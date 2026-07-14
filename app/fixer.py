"""Workspace management and the headless Claude Code invocation."""

import asyncio
import json
import logging
import os
import re
from pathlib import Path

from .config import RepoTarget, Settings

log = logging.getLogger("brain.fixer")

PR_URL_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")

BASE_ALLOWED_TOOLS = [
    "Read", "Grep", "Glob", "Edit", "Write",
    "Bash(git:*)", "Bash(gh:*)", "Bash(brain-ticket:*)",
]


class FixResult:
    def __init__(self, status: str, pr_url: str | None = None, detail: str = "",
                 meta: dict | None = None):
        # pr_opened | needs_input | no_fix | error | timeout
        self.status = status
        self.pr_url = pr_url
        self.detail = detail
        self.meta = meta or {}  # cost_usd / num_turns / duration_ms from the CLI envelope


class RawRunResult:
    def __init__(self, status: str, text: str, meta: dict | None = None):
        self.status = status  # ok | error | timeout
        self.text = text
        self.meta = meta or {}


async def _run(cmd: list[str], cwd: str | None = None, timeout: int = 300) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise
    return proc.returncode or 0, out.decode(errors="replace")


async def prepare_workspace(settings: Settings, target: RepoTarget, branch: str,
                            keep_branch: bool = False) -> str:
    """Clone (or refresh) the repo and check out a clean branch off the base.

    keep_branch=True (phase 2) reuses the existing branch if it exists so the
    fix lands on the same branch the analysis referenced.
    """
    name = target.repo.split("/")[-1]
    workspace = str(Path(settings.workspaces_dir) / name)
    Path(settings.workspaces_dir).mkdir(parents=True, exist_ok=True)

    if not Path(workspace, ".git").exists():
        code, out = await _run(
            ["git", "clone", "--filter=blob:none", f"https://github.com/{target.repo}.git", workspace],
            timeout=900,
        )
        if code != 0:
            raise RuntimeError(f"git clone failed: {out[-2000:]}")

    start_point = f"origin/{target.base}"
    cmds = [["git", "fetch", "origin", target.base]]
    if keep_branch:
        # reuse the branch if present locally, else recreate from base
        code, _ = await _run(["git", "rev-parse", "--verify", branch], cwd=workspace, timeout=60)
        cmds += [["git", "checkout", branch] if code == 0 else ["git", "checkout", "-B", branch, start_point]]
    else:
        cmds += [
            ["git", "checkout", "-B", branch, start_point],
            ["git", "reset", "--hard", start_point],
        ]
    # NOTE: no -x — keep ignored files (node_modules) so test setup is cached across runs
    cmds += [["git", "clean", "-fd"]]

    for cmd in cmds:
        code, out = await _run(cmd, cwd=workspace, timeout=300)
        if code != 0:
            raise RuntimeError(f"{' '.join(cmd)} failed: {out[-2000:]}")
    return workspace


class BranchLostError(RuntimeError):
    pass


async def git(workspace: str, *args: str, timeout: int = 300) -> tuple[int, str]:
    """Run a git command in a workspace; returns (exit_code, combined_output)."""
    return await _run(["git", *args], cwd=workspace, timeout=timeout)


async def prepare_feature_workspace(settings: Settings, target: RepoTarget,
                                    branch: str, stage: int) -> str:
    """Feature stages resume from origin/<branch> — never silently rebuild from base.

    Stage 0 creates the branch from origin/<base>. Stage > 0 requires
    origin/<branch> to exist (every stage end pushes it); if it is missing the
    prior artifacts are gone and continuing would build from nothing.
    Raises BranchLostError in that case.
    """
    name = target.repo.split("/")[-1]
    workspace = str(Path(settings.workspaces_dir) / name)
    Path(settings.workspaces_dir).mkdir(parents=True, exist_ok=True)

    if not Path(workspace, ".git").exists():
        code, out = await _run(
            ["git", "clone", "--filter=blob:none", f"https://github.com/{target.repo}.git", workspace],
            timeout=900,
        )
        if code != 0:
            raise RuntimeError(f"git clone failed: {out[-2000:]}")

    code, _ = await git(workspace, "fetch", "origin", target.base)
    if code != 0:
        raise RuntimeError(f"git fetch {target.base} failed")
    branch_code, _ = await git(workspace, "fetch", "origin", branch)

    if branch_code == 0:
        cmds = [["checkout", "-B", branch, f"origin/{branch}"]]
    elif stage == 0:
        cmds = [["checkout", "-B", branch, f"origin/{target.base}"],
                ["reset", "--hard", f"origin/{target.base}"]]
    else:
        raise BranchLostError(
            f"origin/{branch} is missing but the pipeline is at stage P{stage} — "
            "feature branch lost; not rebuilding from base"
        )
    cmds.append(["clean", "-fd"])
    for cmd in cmds:
        code, out = await git(workspace, *cmd)
        if code != 0:
            raise RuntimeError(f"git {' '.join(cmd)} failed: {out[-2000:]}")
    return workspace


async def run_claude_raw(settings: Settings, workspace: str, prompt: str,
                         allowed_tools: list[str], timeout: int,
                         resume_session: str | None = None,
                         disallowed_tools: list[str] | None = None) -> RawRunResult:
    """Low-level headless run. Returns the CLI's result text verbatim plus the
    telemetry envelope (session/cost/turns/duration) — callers own the parsing.

    resume_session continues an existing session (same working directory) with
    full context — increment 2 (docs/CONVERSATIONS.md); requires
    settings.session_persistence. disallowed_tools is a hard DENY list —
    --allowedTools alone is additive to settings-file grants, so read-only
    chat runs must explicitly deny the write tools."""
    cmd = [settings.claude_binary, "-p"]
    if resume_session:
        cmd += ["-r", resume_session]
    cmd += [
        prompt,
        "--output-format", "json",
        "--allowedTools", ",".join(allowed_tools),
    ]
    if disallowed_tools:
        cmd += ["--disallowedTools", ",".join(disallowed_tools)]
    if settings.claude_model:
        cmd += ["--model", settings.claude_model]

    env = os.environ.copy()
    env["GH_TOKEN"] = settings.github_token
    env["CLICKUP_TOKEN"] = settings.clickup_token  # used by the brain-ticket CLI
    # never inherit an ambient session identity from the service's own env
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    if settings.session_persistence:
        # sessions on the data volume so resume survives restarts (increment 2;
        # needs the §1 bootstrap contract deployed — flag-gated until then)
        env["CLAUDE_CONFIG_DIR"] = settings.claude_config_dir

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=workspace,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        log.error("claude run timed out after %ss", timeout)
        return RawRunResult("timeout", f"timed out after {timeout}s")
    except asyncio.CancelledError:
        # graceful shutdown must never orphan a live claude that later pushes
        proc.kill()
        raise

    stdout = out.decode(errors="replace")
    if proc.returncode != 0:
        log.error("claude exited %s: %s", proc.returncode, err.decode(errors="replace")[-2000:])
        return RawRunResult("error", f"claude exited {proc.returncode}: {stdout[-2000:]}")

    meta: dict = {}
    try:
        envelope = json.loads(stdout)
        result_text = envelope.get("result", "")
        meta = {
            "cost_usd": envelope.get("total_cost_usd"),
            "num_turns": envelope.get("num_turns"),
            "duration_ms": envelope.get("duration_ms"),
            "session_id": envelope.get("session_id"),
        }
    except (json.JSONDecodeError, AttributeError):
        result_text = stdout
    return RawRunResult("ok", result_text, meta)


async def run_claude(settings: Settings, target: RepoTarget, workspace: str, prompt: str) -> FixResult:
    """v1 contract used by sentry/task jobs: PR URL sniffing + NEEDS_INPUT/NO_FIX."""
    raw = await run_claude_raw(
        settings, workspace, prompt,
        allowed_tools=BASE_ALLOWED_TOOLS + target.allow,
        timeout=settings.claude_timeout_seconds,
    )
    if raw.status == "timeout":
        return FixResult("timeout", detail=raw.text, meta=raw.meta)
    if raw.status == "error":
        return FixResult("error", detail=raw.text, meta=raw.meta)

    result_text = raw.text
    pr_match = PR_URL_RE.search(result_text)
    if pr_match:
        return FixResult("pr_opened", pr_url=pr_match.group(0), detail=result_text[-3000:], meta=raw.meta)
    if "NEEDS_INPUT:" in result_text:
        return FixResult("needs_input", detail=result_text.split("NEEDS_INPUT:", 1)[1].strip()[:8000], meta=raw.meta)
    return FixResult("no_fix", detail=result_text[-3000:], meta=raw.meta)
