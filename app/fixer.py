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


class FixResult:
    def __init__(self, status: str, pr_url: str | None = None, detail: str = ""):
        self.status = status  # pr_opened | no_fix | error | timeout
        self.pr_url = pr_url
        self.detail = detail


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


async def prepare_workspace(settings: Settings, target: RepoTarget, branch: str) -> str:
    """Clone (or refresh) the repo and check out a clean branch off the base."""
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

    for cmd in (
        ["git", "fetch", "origin", target.base],
        ["git", "checkout", "-B", branch, f"origin/{target.base}"],
        ["git", "reset", "--hard", f"origin/{target.base}"],
        ["git", "clean", "-fd"],
    ):
        code, out = await _run(cmd, cwd=workspace, timeout=300)
        if code != 0:
            raise RuntimeError(f"{' '.join(cmd)} failed: {out[-2000:]}")
    return workspace


async def run_claude(settings: Settings, workspace: str, prompt: str) -> FixResult:
    cmd = [
        settings.claude_binary,
        "-p", prompt,
        "--output-format", "json",
        "--allowedTools", "Read,Grep,Glob,Edit,Write,Bash(git:*),Bash(gh:*)",
    ]
    if settings.claude_model:
        cmd += ["--model", settings.claude_model]

    env = os.environ.copy()
    env["GH_TOKEN"] = settings.github_token

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=workspace,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(), timeout=settings.claude_timeout_seconds
        )
    except asyncio.TimeoutError:
        proc.kill()
        log.error("claude run timed out after %ss", settings.claude_timeout_seconds)
        return FixResult("timeout", detail=f"timed out after {settings.claude_timeout_seconds}s")

    stdout = out.decode(errors="replace")
    if proc.returncode != 0:
        log.error("claude exited %s: %s", proc.returncode, err.decode(errors="replace")[-2000:])
        return FixResult("error", detail=f"claude exited {proc.returncode}: {stdout[-2000:]}")

    try:
        result_text = json.loads(stdout).get("result", "")
    except (json.JSONDecodeError, AttributeError):
        result_text = stdout

    pr_match = PR_URL_RE.search(result_text)
    if pr_match:
        return FixResult("pr_opened", pr_url=pr_match.group(0), detail=result_text[-3000:])
    return FixResult("no_fix", detail=result_text[-3000:])
