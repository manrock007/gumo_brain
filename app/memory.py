"""Product memory: versioned knowledge that warms every run (docs/ENGINE.md §4).

Two scopes (under the engine namespace dir — `.ctrlloop/`, or the legacy
`.gumo/` on pre-rename repos; legacy wins when present, same rule as
fixer.engine_dir):
- Repo scope   — `<ns>/memory/` in each repo (architecture, map, conventions,
  decisions/ + changelog/ as per-entry directories).
- Product scope — `<ns>/product/` in the CANONICAL repo (settings.
  memory_canonical_project, part of the editable project context): what the
  product is, cross-repo contract. Client-repo runs read it base-pinned from
  the canonical workspace; client repos carry no product.md.

Prompts inline capped excerpts + full paths; the files are in the clone, so
Claude reads full versions on demand. The brain-side cache is dashboard-only.
"""

import json
import logging
import time
from pathlib import Path

from .config import ENGINE_DIR, LEGACY_ENGINE_DIRS, Settings
from .fixer import _run, engine_dir, git, git_show_ns

log = logging.getLogger("brain.memory")

# display-string defaults (docs, prompts with no workspace at hand); real
# reads resolve the namespace per clone via memory_dir()/product_dir()
MEMORY_DIR = f"{ENGINE_DIR}/memory"
PRODUCT_DIR = f"{ENGINE_DIR}/product"


def memory_dir(workspace: str) -> str:
    return f"{engine_dir(workspace)}/memory"


def product_dir(workspace: str) -> str:
    return f"{engine_dir(workspace)}/product"

REPO_FILES = ["architecture.md", "map.md", "conventions.md"]
PRODUCT_FILES = ["product.md", "contract.md"]

# per-file inline caps (chars) — pointers over token floods
CAPS = {
    "product.md": 6000,
    "contract.md": 4000,
    "architecture.md": 8000,
    "map.md": 6000,
    "conventions.md": 6000,
    "changelog": 3000,
    "decisions": 2000,
}

# stage -> which memory sections get inlined (docs/ENGINE.md §4 matrix)
STAGE_MATRIX = {
    0: ["product.md", "contract.md", "changelog"],
    1: ["product.md", "contract.md", "changelog"],
    2: ["product.md", "architecture.md", "map.md", "decisions"],
    3: ["product.md", "architecture.md", "map.md", "decisions"],
    4: ["product.md", "architecture.md", "map.md", "decisions", "conventions.md"],
    5: ["conventions.md"],
    6: ["conventions.md"],
    7: ["conventions.md"],
    8: ["conventions.md"],
    9: ["product.md", "architecture.md", "map.md", "changelog", "decisions", "conventions.md"],
}


def _cap(text: str, limit: int, path_note: str) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… (truncated — full file at `{path_note}`)"


def _read(workspace: str, rel: str) -> str:
    p = Path(workspace) / rel
    try:
        return p.read_text() if p.is_file() else ""
    except OSError:
        return ""


def _entries_tail(workspace: str, rel_dir: str, cap: int) -> str:
    """Newest-N concatenation of a per-entry directory (changelog/, decisions/)."""
    d = Path(workspace) / rel_dir
    if not d.is_dir():
        return ""
    files = sorted((p for p in d.glob("*.md")), key=lambda p: p.name, reverse=True)
    out, total, shown = [], 0, 0
    for p in files:
        try:
            t = p.read_text().strip()
        except OSError:
            continue
        if total + len(t) > cap and shown:
            break
        out.append(f"<!-- {p.name} -->\n{t}")
        total += len(t)
        shown += 1
    if not out:
        return ""
    header = f"(showing {shown} most recent of {len(files)} entries)\n\n"
    return header + "\n\n".join(out)


class MemoryReader:
    def __init__(self, settings: Settings, locks=None):
        self.settings = settings
        self.locks = locks  # RepoLocks; None for read-only cache use (dashboard)
        # Phase 2: slug -> canonical slug via its workspace (injected at startup);
        # None falls back to the instance-wide memory_canonical_project
        self.canonical_resolver = None

    async def product_scope(self, project: str, workspace: str) -> dict[str, str]:
        """Product-scope files, base-pinned. For the canonical repo, read them
        from its own clone; for client repos, read from the canonical
        workspace's origin/<base> (fetched fresh) — under the canonical repo's
        lock, since that workspace belongs to other jobs too."""
        canonical = (self.canonical_resolver(project) if self.canonical_resolver
                     else self.settings.memory_canonical_project)
        if project == canonical:
            return {f: _read(workspace, f"{product_dir(workspace)}/{f}") for f in PRODUCT_FILES}
        target = self.settings.repo_for_project(canonical)
        if target is None:
            return {}
        if self.locks is not None:
            async with self.locks.for_repo(target.repo):
                return await self._read_canonical(target)
        return await self._read_canonical(target)

    async def _read_canonical(self, target) -> dict[str, str]:
        ws = Path(self.settings.workspaces_dir) / target.repo.split("/")[-1]
        if not (ws / ".git").exists():
            code, out = await _run(
                ["git", "clone", "--filter=blob:none",
                 f"https://github.com/{target.repo}.git", str(ws)],
                timeout=900,
            )
            if code != 0:
                log.warning("canonical clone failed; product scope unavailable: %s", out[-300:])
                return {}
        await git(str(ws), "fetch", "origin", target.base)
        files = {}
        for f in PRODUCT_FILES:
            code, out = await git_show_ns(str(ws), f"origin/{target.base}", f"product/{f}")
            files[f] = out if code == 0 else ""
        return files

    async def context_for_stage(self, stage: int, project: str, workspace: str) -> str:
        """Assemble the memory block for a stage prompt (capped excerpts + paths)."""
        wanted = STAGE_MATRIX.get(stage, ["product.md"])
        product = await self.product_scope(project, workspace)
        md = memory_dir(workspace)
        blocks: list[str] = []
        degraded = True
        for section in wanted:
            if section in PRODUCT_FILES:
                text = product.get(section, "")
                note = f"{PRODUCT_DIR}/{section} (canonical repo)"
            elif section == "changelog":
                text = _entries_tail(workspace, f"{md}/changelog", CAPS["changelog"])
                note = f"{md}/changelog/"
            elif section == "decisions":
                text = _entries_tail(workspace, f"{md}/decisions", CAPS["decisions"])
                note = f"{md}/decisions/"
            else:
                text = _read(workspace, f"{md}/{section}")
                note = f"{md}/{section}"
            if not text.strip():
                continue
            degraded = False
            blocks.append(f"### {section}\n\n{_cap(text, CAPS.get(section, 4000), note)}")
        if degraded:
            return ""  # caller declares degraded mode in the prompt
        return "\n\n".join(blocks)

    async def freshness(self, workspace: str, base: str) -> int | None:
        """Commits on origin/<base> since the last commit touching the engine
        namespace — the memory-staleness metric shown on the dashboard. BOTH
        pathspecs are passed (git tolerates absent ones), so whichever tree
        the repo carries counts."""
        code, last = await git(workspace, "rev-list", "-1", f"origin/{base}",
                               "--", ENGINE_DIR, *LEGACY_ENGINE_DIRS)
        if code != 0 or not last.strip():
            return None
        code, count = await git(workspace, "rev-list", "--count",
                                f"{last.strip()}..origin/{base}")
        return int(count.strip()) if code == 0 and count.strip().isdigit() else None

    async def refresh_cache(self, project: str, workspace: str, base: str):
        """Copy origin/<base> memory into data_dir/memory/<project>/ for the
        dashboard. Base-pinned: never branch or bootstrap-draft content."""
        cache = Path(self.settings.data_dir) / "memory" / project
        cache.mkdir(parents=True, exist_ok=True)
        code, sha = await git(workspace, "rev-parse", f"origin/{base}")
        listed: dict[str, int] = {}
        for scope, names in (("memory", REPO_FILES), ("product", PRODUCT_FILES)):
            for name in names:
                fcode, out = await git_show_ns(workspace, f"origin/{base}", f"{scope}/{name}")
                if fcode == 0 and out.strip():
                    (cache / name).write_text(out)
                    listed[name] = len(out)
        for entry_dir in ("changelog", "decisions"):
            n = 0
            # same precedence as every other read: legacy wins when present
            for ns in (*LEGACY_ENGINE_DIRS, ENGINE_DIR):
                lcode, out = await git(workspace, "ls-tree", "--name-only",
                                       f"origin/{base}", f"{ns}/memory/{entry_dir}/")
                if lcode == 0:
                    n = len([l for l in out.splitlines() if l.strip().endswith(".md")])
                    if n:
                        break
            listed[entry_dir] = n
        fresh = await self.freshness(workspace, base)
        (cache / "meta.json").write_text(json.dumps({
            "commit_sha": sha.strip() if code == 0 else "",
            "fetched_at": time.time(),
            "files": listed,
            "staleness_commits": fresh,
        }))

    def cached(self, project: str) -> dict:
        cache = Path(self.settings.data_dir) / "memory" / project
        if not cache.is_dir():
            return {"exists": False}
        meta = {}
        try:
            meta = json.loads((cache / "meta.json").read_text())
        except (OSError, json.JSONDecodeError):
            pass
        files = {p.name: p.read_text() for p in cache.glob("*.md")}
        return {"exists": bool(files), "meta": meta, "files": files}
