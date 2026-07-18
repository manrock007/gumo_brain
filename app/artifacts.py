"""Shared-artifact sync: git is the source of truth, ClickUp subtasks are the
human editing surface, this module is the sync layer.

Invariants (docs/ENGINE.md §3):
- Human edits always survive; the engine's own writes never clobber them.
- Edit detection is CONTENT-based and tolerant of ClickUp's markdown
  regeneration (escapes, list-marker normalization, blank-line collapse):
  `synced_hash` is the semantic hash of the last READBACK (ClickUp's fixpoint),
  and only a *semantic* difference vs the git file counts as a human edit.
- Fail closed: empty/404/short pulls never become synced state and never
  overwrite git.
"""

import hashlib
import logging
import re
from pathlib import Path

from .clickup import ClickUp
from .config import GATE_PREFIX
from .db import JobStore
from .fixer import engine_dir, git

log = logging.getLogger("brain.artifacts")

_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!>~|])")
_BULLET_RE = re.compile(r"^(\s*)[*+]\s", re.MULTILINE)
_ORDERED_RE = re.compile(r"^(\s*)\d+[.)]\s", re.MULTILINE)
_BLANKS_RE = re.compile(r"\n{3,}")


def normalize(text: str) -> str:
    """Byte-level normalization applied to everything we hash or write."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return text.strip() + "\n" if text.strip() else ""


def semantic_normalize(text: str) -> str:
    """Collapse ClickUp's markdown round-trip mangling so only human edits differ.

    ClickUp re-generates markdown from its rich-text model: it backslash-escapes
    punctuation, rewrites bullet/number markers, and collapses blank runs. Two
    texts that differ only in those ways are the SAME artifact.
    """
    text = normalize(text)
    text = _ESCAPE_RE.sub(r"\1", text)
    text = _BULLET_RE.sub(r"\1- ", text)
    text = _ORDERED_RE.sub(r"\g<1>1. ", text)
    text = _BLANKS_RE.sub("\n\n", text)
    # table-row whitespace: "| a  | b |" == "| a | b |"
    text = "\n".join(
        re.sub(r"\s*\|\s*", " | ", line).strip() if line.lstrip().startswith("|") else line
        for line in text.split("\n")
    )
    return text


def semantic_hash(text: str) -> str:
    return hashlib.sha256(semantic_normalize(text).encode()).hexdigest()


def features_dir(workspace: str) -> str:
    """Relative features root inside a clone, namespace-resolved (legacy wins)."""
    return f"{engine_dir(workspace)}/features"


def feature_dir(workspace: str, job_id: str) -> str:
    return f"{features_dir(workspace)}/{job_id}"


def artifact_path(workspace: str, job_id: str, artifact: str) -> Path:
    return Path(workspace) / feature_dir(workspace, job_id) / artifact


def list_artifacts(workspace: str, job_id: str) -> list[str]:
    d = Path(workspace) / feature_dir(workspace, job_id)
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.glob("P*-*.md"))


class ArtifactSync:
    """Per-job sync between the feature branch and ClickUp subtasks."""

    def __init__(self, store: JobStore, clickup: ClickUp, mirror_max_chars: int = 50000):
        self.store = store
        self.clickup = clickup
        self.mirror_max_chars = mirror_max_chars

    # ---------- git side ----------

    async def commit_file(self, workspace: str, job_id: str, artifact: str,
                          content: str, message: str) -> bool:
        path = artifact_path(workspace, job_id, artifact)
        path.parent.mkdir(parents=True, exist_ok=True)
        new = normalize(content)
        old = path.read_text() if path.exists() else None
        if old is not None and normalize(old) == new:
            return False  # idempotent under replay: identical content is a no-op
        path.write_text(new)
        # keep the fast-lane bundle cache current with every artifact write
        self.store.artifact_content_set(job_id, artifact, new)
        await git(workspace, "add", str(path))
        code, out = await git(workspace, "commit", "-m", message)
        if code != 0 and "nothing to commit" not in out:
            log.error("artifact commit failed: %s", out[-500:])
        return True

    # ---------- pull: ClickUp -> git (before every stage run) ----------

    async def pull(self, workspace: str, job: dict, branch: str | None = None) -> list[str]:
        """Fold human edits from ClickUp into the branch. Returns edited artifact
        names. Never lets a missing/empty/short mirror overwrite git.

        Durability: when `branch` is given, a human-edit commit is pushed to
        origin BEFORE synced_hash is advanced — otherwise a crash between the
        local commit and the branch push would lose the edit while the hash
        claims it is synced, and the next pull would see 'unchanged'. Without a
        branch (tests / local-only), the hash advances immediately."""
        job_id = job["issue_id"]
        edited: list[str] = []
        if not self.clickup.enabled:
            return edited
        for state in self.store.artifacts_for(job_id):
            artifact, subtask_id = state["artifact"], state["subtask_id"]
            if not subtask_id or "truncated" in (state["flags"] or ""):
                continue  # truncated mirrors are pointers, not editing surfaces
            task = await self.clickup.get_task(subtask_id)
            if task is None:
                continue  # unknown (API failure) — fail closed, change nothing
            path = artifact_path(workspace, job_id, artifact)
            git_text = path.read_text() if path.exists() else ""
            if task.get("missing") or task.get("archived"):
                await self._recreate_mirror(workspace, job, artifact, git_text)
                continue
            fetched = task.get("description") or ""
            if not fetched.strip():
                continue  # empty pull NEVER wins; the gate flow asks the human
            fetched_hash = semantic_hash(fetched)
            if fetched_hash == state["synced_hash"]:
                continue  # unchanged since last sync
            if semantic_normalize(fetched) == semantic_normalize(git_text):
                # byte-only drift (ClickUp re-mangling): refresh hash, no commit
                self.store.artifact_set(job_id, artifact, synced_hash=fetched_hash)
                continue
            committed = await self.commit_file(
                workspace, job_id, artifact, fetched,
                f"artifact: human edit {artifact.removesuffix('.md')} (via ClickUp)",
            )
            if committed and branch is not None:
                code, _ = await git(workspace, "push", "--force-with-lease", "-u", "origin", branch)
                if code != 0:
                    # not durable — leave synced_hash unadvanced so the next pull
                    # re-detects and re-commits this same edit (idempotent)
                    log.error("job %s: push after pulling %s failed; will retry", job_id, artifact)
                    continue
            self.store.artifact_set(job_id, artifact, synced_hash=fetched_hash)
            if committed:
                edited.append(artifact)
                log.info("job %s: human edit pulled for %s", job_id, artifact)
        return edited

    # ---------- push: git -> ClickUp (after every stage run) ----------

    async def push(self, workspace: str, job: dict) -> list[str]:
        """Mirror current branch artifacts to ClickUp. Compare-and-set: a human
        edit made DURING the run wins — it is committed on top and the mirror
        is left untouched. Returns artifacts where that happened (for the gate
        warning)."""
        job_id = job["issue_id"]
        conflicted: list[str] = []
        contents: dict[str, str] = {}
        # refresh the fast-lane bundle cache for EVERY branch artifact first —
        # code stages write artifacts directly in the workspace (no commit_file),
        # and this must happen even when ClickUp mirroring is disabled
        for artifact in list_artifacts(workspace, job_id):
            contents[artifact] = normalize(artifact_path(workspace, job_id, artifact).read_text())
            self.store.artifact_content_set(job_id, artifact, contents[artifact])
        if not self.clickup.enabled or not job.get("clickup_task_id"):
            return conflicted
        for artifact, content in contents.items():
            state = self.store.artifact_get(job_id, artifact)
            if state is None or not state["subtask_id"]:
                await self._create_mirror(workspace, job, artifact, content)
                continue
            subtask_id = state["subtask_id"]
            truncated = "truncated" in (state["flags"] or "")
            task = await self.clickup.get_task(subtask_id)
            if task is None:
                continue  # API failure — leave mirror + hash as-is, retry next sync
            if task.get("missing") or task.get("archived"):
                await self._recreate_mirror(workspace, job, artifact, content)
                continue
            if truncated:
                # the mirror is an intentional pointer, not an editing surface;
                # never treat the banner as a human edit — just refresh the pointer
                await self._put_and_fix(workspace, job_id, subtask_id, artifact, content)
                continue
            current = task.get("description") or ""
            if (current.strip() and state["synced_hash"]
                    and semantic_hash(current) != state["synced_hash"]
                    and semantic_normalize(current) != semantic_normalize(content)):
                # human edited while the stage ran: their version wins
                await self.commit_file(
                    workspace, job_id, artifact, current,
                    f"artifact: human edit {artifact.removesuffix('.md')} (during stage run — wins)",
                )
                self.store.artifact_set(job_id, artifact, synced_hash=semantic_hash(current))
                conflicted.append(artifact)
                continue
            await self._put_and_fix(workspace, job_id, subtask_id, artifact, content)
        return conflicted

    # ---------- mirror plumbing ----------

    def _mirror_body(self, content: str, workspace_path: str) -> tuple[str, bool]:
        if len(content) <= self.mirror_max_chars:
            return content, False
        head = content[: self.mirror_max_chars]
        return (
            f"**TRUNCATED MIRROR — full document lives in git at `{workspace_path}`. "
            f"Edit via gate comments, not here.**\n\n{head}",
            True,
        )

    async def _create_mirror(self, workspace: str, job: dict, artifact: str, content: str):
        # workspace resolves the REAL namespace for the truncated-mirror pointer
        # text — the human's only pointer to the git file; a wrong dir on a
        # legacy repo would mislead the editing human
        job_id = job["issue_id"]
        body, truncated = self._mirror_body(content, f"{feature_dir(workspace, job_id)}/{artifact}")
        created = await self.clickup.create_task(
            name=artifact.removesuffix(".md").replace("-", " "),
            description=body,
            list_id=job.get("cu_list_id") or None,
            parent=job.get("clickup_task_id"),
        )
        if created is None:
            if job.get("mirror_ok"):
                self.store.set_fields(job_id, mirror_ok=0)
                await self.clickup.comment(
                    job.get("clickup_task_id") or "",
                    f"{GATE_PREFIX} Artifact mirroring to subtasks is unavailable for this "
                    "ticket — read artifacts in git and answer gates on the dashboard or here.",
                )
            return
        subtask_id, _ = created
        readback = await self.clickup.get_task(subtask_id)
        synced = semantic_hash(readback.get("description") or body) if readback and not readback.get("missing") else ""
        self.store.artifact_set(
            job_id, artifact, subtask_id=subtask_id, synced_hash=synced,
            flags="truncated" if truncated else "",
        )
        if not job.get("mirror_ok"):
            self.store.set_fields(job_id, mirror_ok=1)

    async def _recreate_mirror(self, workspace: str, job: dict, artifact: str, content: str):
        job_id = job["issue_id"]
        log.warning("job %s: mirror subtask for %s lost — recreating from git", job_id, artifact)
        self.store.artifact_set(job_id, artifact, subtask_id="", synced_hash="", flags="mirror_lost")
        await self._create_mirror(workspace, job, artifact, content)
        await self.clickup.comment(
            job.get("clickup_task_id") or "",
            f"{GATE_PREFIX} The `{artifact}` subtask disappeared; I recreated it from git "
            "(the source of truth). Any edits made to the old subtask were not received.",
        )

    async def _put_and_fix(self, workspace: str, job_id: str, subtask_id: str,
                           artifact: str, content: str):
        """PUT then hash the READBACK — ClickUp's regenerated markdown is the
        fixpoint that future pulls are compared against."""
        body, truncated = self._mirror_body(content, f"{feature_dir(workspace, job_id)}/{artifact}")
        ok = await self.clickup.update_description(subtask_id, body)
        if not ok:
            return  # keep old hash; retry at next sync point
        readback = await self.clickup.get_task(subtask_id)
        if readback is None or readback.get("missing"):
            return
        got = readback.get("description") or ""
        # post-push readback dramatically shorter than sent => ClickUp size cliff
        if not truncated and len(got) < min(len(body), self.mirror_max_chars) * 0.5 and len(body) > 2000:
            truncated = True
            log.warning("job %s: %s readback truncated by ClickUp — switching to pointer mirror", job_id, artifact)
            pointer = (f"**TRUNCATED MIRROR — full document lives in git at "
                       f"`{feature_dir(workspace, job_id)}/{artifact}`. Edit via gate comments, not here.**")
            await self.clickup.update_description(subtask_id, pointer)
            # hash the POINTER readback, not the stale full-content one, else the
            # next push() reads the banner as a 'human edit' and commits it to git
            pb = await self.clickup.get_task(subtask_id)
            got = pb.get("description") or pointer if pb and not pb.get("missing") else pointer
        self.store.artifact_set(
            job_id, artifact,
            synced_hash=semantic_hash(got),
            flags="truncated" if truncated else "",
        )
