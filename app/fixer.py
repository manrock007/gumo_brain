"""Workspace management and the headless Claude Code invocation."""

import asyncio
import contextlib
import json
import logging
import os
import re
from pathlib import Path

from .config import RepoTarget, Settings

log = logging.getLogger("brain.fixer")

PR_URL_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
# STRICT capture: only a standalone `PR_URL: <url>` line counts as "this run
# OPENED that PR" — a URL mentioned in prose (context, a related PR) must never
# trigger the lifecycle kickoff. Shared with the feature pipeline (engine.py).
PR_LINE_RE = re.compile(
    r"^[\s`*>-]*PR_URL:\s*`?(https://github\.com/[\w./-]+/pull/\d+)`?[\s`]*$", re.MULTILINE
)

# Upstream hiccups that deserve ONE automatic retry before parking as an error
# needing a human /redo. Deliberately narrow: assertion failures, tool errors
# and anything the run itself concluded must stay manual.
TRANSIENT_ERROR_RE = re.compile(
    r"(?i)(api error|server error|internal server|bad gateway|service unavailable|"
    r"gateway timeout|rate.?limit|overloaded|connection (?:reset|refused|error|closed)|"
    r"temporarily unavailable|socket hang.?up)")

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
        self.pr_urls: list[str] = [pr_url] if pr_url else []  # every PR the run mentioned
        self.detail = detail
        self.meta = meta or {}  # cost_usd / num_turns / duration_ms from the CLI envelope


class RawRunResult:
    def __init__(self, status: str, text: str, meta: dict | None = None):
        self.status = status  # ok | error | timeout | session_lost
        self.text = text
        self.meta = meta or {}


def ensure_session_store(settings: Settings):
    """Bootstrap the relocated CLI config dir (docs/CONVERSATIONS.md §4): an empty
    CLAUDE_CONFIG_DIR is a logged-out CLI unless auth arrives via env
    (CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY pass through os.environ). Seed
    credentials/onboarding state from the legacy ~/.claude location when present
    so OAuth-file deployments survive the move. Called at startup when
    session_persistence is on; loud on failure, never fatal."""
    import shutil

    for target_dir in (settings.claude_config_dir, settings.claude_chat_config_dir):
        try:
            Path(target_dir).mkdir(parents=True, exist_ok=True)
            legacy = Path.home() / ".claude"
            for name in (".credentials.json", ".claude.json"):
                src = legacy / name
                dst = Path(target_dir) / name
                if src.is_file() and not dst.exists():
                    shutil.copy2(src, dst)
            legacy_json = Path.home() / ".claude.json"  # onboarding state sits beside ~/.claude
            dst_json = Path(target_dir) / ".claude.json"
            if legacy_json.is_file() and not dst_json.exists():
                shutil.copy2(legacy_json, dst_json)
        except OSError:
            log.exception("session store bootstrap failed for %s — session "
                          "persistence will degrade to fresh runs", target_dir)


def session_transcript_exists(settings: Settings, session_id: str,
                              config_dir: str | None = None) -> bool:
    """A resume of a missing session exits 0 with EMPTY stdout (verified on the
    installed CLI) — never trust exit signals. Transcripts live under
    <config dir>/projects/<cwd-slug>/<session>.jsonl; glob across project
    slugs so slug-scheme drift can't fake a loss. config_dir picks WHICH store
    to search — the default stage store, or the dedicated chat store for runs
    that were invoked with config_dir=claude_chat_config_dir."""
    if not session_id or not settings.session_persistence:
        return False
    root = Path(config_dir or settings.claude_config_dir) / "projects"
    if not root.is_dir():
        return False
    return any(root.glob(f"*/{session_id}.jsonl"))


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
                            keep_branch: bool = False,
                            workspace_root: str | None = None) -> str:
    """Clone (or refresh) the repo and check out a clean branch off the base.

    keep_branch=True (phase 2) reuses the existing branch if it exists so the
    fix lands on the same branch the analysis referenced.
    workspace_root overrides the clone location — read-only chat runs use their
    own clone so they never contend with the job holding the main workspace.
    """
    root = workspace_root or settings.workspaces_dir
    name = target.repo.split("/")[-1]
    workspace = str(Path(root) / name)
    Path(root).mkdir(parents=True, exist_ok=True)

    if not Path(workspace, ".git").exists():
        code, out = await _run(
            ["git", "clone", "--filter=blob:none", f"https://github.com/{target.repo}.git", workspace],
            timeout=900,
        )
        if code != 0:
            raise RuntimeError(f"git clone failed: {out[-2000:]}")

    start_point = f"origin/{target.base}"
    # discard leftovers FIRST: a run killed mid-write (deploy restart) leaves
    # dirty TRACKED files that make every later `git checkout` refuse — origin
    # is always the record, local edits from a dead run are worthless
    cmds = [["git", "reset", "--hard"],
            ["git", "fetch", "origin", target.base]]
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
                                    branch: str, stage: int,
                                    workspace_root: str | None = None) -> str:
    """Feature stages resume from origin/<branch> — never silently rebuild from base.

    Stage 0 creates the branch from origin/<base>. Stage > 0 requires
    origin/<branch> to exist (every stage end pushes it); if it is missing the
    prior artifacts are gone and continuing would build from nothing.
    Raises BranchLostError in that case.
    workspace_root overrides the clone location — shepherd fix runs use their
    own clone so they never contend with the job holding the main workspace.
    """
    root = workspace_root or settings.workspaces_dir
    name = target.repo.split("/")[-1]
    workspace = str(Path(root) / name)
    Path(root).mkdir(parents=True, exist_ok=True)

    if not Path(workspace, ".git").exists():
        code, out = await _run(
            ["git", "clone", "--filter=blob:none", f"https://github.com/{target.repo}.git", workspace],
            timeout=900,
        )
        if code != 0:
            raise RuntimeError(f"git clone failed: {out[-2000:]}")

    # discard leftovers FIRST (see prepare_workspace): a stage run killed
    # mid-write leaves dirty tracked files that abort the checkout below
    code, out = await git(workspace, "reset", "--hard")
    if code != 0:
        raise RuntimeError(f"git reset --hard failed: {out[-2000:]}")
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


def _claude_cmd_env(settings: Settings, prompt: str, allowed_tools: list[str],
                    resume_session: str | None, disallowed_tools: list[str] | None,
                    session_id: str | None, fork_session: bool,
                    config_dir: str | None, output_format: str = "json"):
    """Shared command/env assembly for the raw and streaming runners — one
    place owns the flag order and the env hygiene."""
    # prompt is positional, directly after -p; option flags follow it
    cmd = [
        settings.claude_binary,
        "-p", prompt,
        "--output-format", output_format,
        "--allowedTools", ",".join(allowed_tools),
    ]
    if output_format == "stream-json":
        cmd += ["--verbose"]  # the CLI requires it with -p + stream-json
    if resume_session:
        cmd += ["-r", resume_session]
        if fork_session:
            cmd += ["--fork-session"]
    elif session_id:
        cmd += ["--session-id", session_id]
    if disallowed_tools:
        cmd += ["--disallowedTools", ",".join(disallowed_tools)]
    if settings.claude_model:
        cmd += ["--model", settings.claude_model]

    env = os.environ.copy()
    env["GH_TOKEN"] = settings.github_token
    env["CLICKUP_TOKEN"] = settings.clickup_token  # used by the brain-ticket CLI
    # never inherit an ambient session identity from the service's own env
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    elif settings.session_persistence:
        # sessions on the data volume so resume survives restarts
        env["CLAUDE_CONFIG_DIR"] = settings.claude_config_dir
    return cmd, env


async def run_claude_raw(settings: Settings, workspace: str, prompt: str,
                         allowed_tools: list[str], timeout: int,
                         resume_session: str | None = None,
                         disallowed_tools: list[str] | None = None,
                         session_id: str | None = None,
                         fork_session: bool = False,
                         config_dir: str | None = None) -> RawRunResult:
    """Low-level headless run. Returns the CLI's result text verbatim plus the
    telemetry envelope (session/cost/turns/duration) — callers own the parsing.

    - resume_session continues an existing session (same working directory) with
      full context; fork_session resumes into a NEW session id.
    - session_id pre-assigns the id on fresh runs — the ENGINE owns identity, so
      timeout/error runs still have a resumable id on record (the envelope is
      only a cross-check).
    - disallowed_tools is a hard DENY list — --allowedTools alone is additive to
      settings-file grants, so read-only chat runs must deny the write tools.
    - config_dir overrides the CLI config dir for this run (artifact-primed
      chats use their own so concurrent invocations never race the session
      store's state files)."""
    cmd, env = _claude_cmd_env(settings, prompt, allowed_tools, resume_session,
                               disallowed_tools, session_id, fork_session, config_dir)

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
        await proc.wait()  # reap — kill alone leaves a zombie
        log.error("claude run timed out after %ss", timeout)
        return RawRunResult("timeout", f"timed out after {timeout}s",
                            {"session_id": session_id})
    except asyncio.CancelledError:
        # graceful shutdown must never orphan a live claude that later pushes
        proc.kill()
        with contextlib.suppress(asyncio.CancelledError):
            await proc.wait()
        raise

    stdout = out.decode(errors="replace")
    stderr = err.decode(errors="replace")

    # a resume of a missing/pruned session exits 0 with EMPTY stdout and the error
    # on stderr only (verified) — never let that flow into stage parsing
    if resume_session and proc.returncode == 0 and not stdout.strip():
        log.warning("resume of session %s found nothing: %s", resume_session, stderr[-300:])
        return RawRunResult("session_lost", stderr[-500:], {"session_id": resume_session})

    # session-id fallback when the envelope can't be parsed: a fresh run's id is
    # engine-owned (still correct); a plain resume continues the same id; but a
    # FORK's new id lives only in the envelope — falling back to the original
    # would make the next chat turn resume (and pollute) the stage session, so
    # a fork with no envelope reports no session at all.
    fallback_sid = session_id or (None if fork_session else resume_session)
    meta: dict = {"session_id": fallback_sid}
    result_text = stdout
    try:
        envelope = json.loads(stdout)
        result_text = envelope.get("result", "")
        meta = {
            "cost_usd": envelope.get("total_cost_usd"),
            "num_turns": envelope.get("num_turns"),
            "duration_ms": envelope.get("duration_ms"),
            "session_id": envelope.get("session_id") or fallback_sid,
        }
    except (json.JSONDecodeError, AttributeError):
        pass  # envelope parsing is best-effort even on nonzero exits

    if proc.returncode != 0:
        log.error("claude exited %s: %s", proc.returncode, stderr[-2000:])
        return RawRunResult("error", f"claude exited {proc.returncode}: {result_text[-2000:]}", meta)
    return RawRunResult("ok", result_text, meta)


def _tool_status(name: str, tool_input: dict) -> str:
    """One human-readable line per tool call for the chat stream."""
    for key in ("file_path", "path", "pattern", "command", "query", "url"):
        val = tool_input.get(key)
        if val:
            return f"{name} {str(val)[:120]}"
    return name


async def run_claude_stream(settings: Settings, workspace: str, prompt: str,
                            allowed_tools: list[str], timeout: int,
                            resume_session: str | None = None,
                            disallowed_tools: list[str] | None = None,
                            session_id: str | None = None,
                            fork_session: bool = False,
                            config_dir: str | None = None,
                            on_event=None,
                            interrupt_event=None) -> RawRunResult:
    """run_claude_raw with live progress (docs/CONVERSATIONS.md §5): the CLI
    runs in stream-json mode and each event is surfaced through on_event as it
    happens — ("status", "Read app/x.py") per tool call, ("delta", text) per
    assistant text block. The RETURN contract is identical to run_claude_raw
    (same statuses, same meta, same session-id fallback rules); on_event is
    best-effort UX and never affects the result.

    interrupt_event (optional asyncio.Event) enables mid-run human steering: when
    it is set, the CLI is killed and the run returns status "interrupted" with the
    (engine-owned, still resumable) session id — the caller resumes that session
    with the steer note folded in. The partial work already streamed stays on the
    branch; killing the CLI never orphans it (the session transcript persists)."""
    on_event = on_event or (lambda event, data: None)
    cmd, env = _claude_cmd_env(settings, prompt, allowed_tools, resume_session,
                               disallowed_tools, session_id, fork_session,
                               config_dir, output_format="stream-json")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=workspace,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=2 ** 20,  # stream-json lines can carry whole documents; 64KB default is too small
    )

    result_env: dict | None = None
    saw_event = False
    text_parts: list = []  # assistant text, for the no-envelope fallback (see below)

    async def _pump():
        nonlocal result_env, saw_event
        async for raw_line in proc.stdout:
            line = raw_line.decode(errors="replace").strip()
            if not line:
                continue
            saw_event = True
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = ev.get("type")
            if etype == "assistant":
                for block in ((ev.get("message") or {}).get("content") or []):
                    if block.get("type") == "tool_use":
                        on_event("status", _tool_status(block.get("name") or "tool",
                                                        block.get("input") or {}))
                    elif block.get("type") == "text" and (block.get("text") or "").strip():
                        on_event("delta", block["text"])
                        text_parts.append(block["text"])
            elif etype == "result":
                result_env = ev

    stderr_task = asyncio.create_task(proc.stderr.read())
    pump_task = asyncio.create_task(_pump())
    steer_task = asyncio.create_task(interrupt_event.wait()) if interrupt_event else None
    waiters = {pump_task} | ({steer_task} if steer_task else set())

    async def _reap():  # kill + drain both readers + the child; leaves no zombie
        pump_task.cancel()
        # the pump may already have finished with an exception (e.g. an over-long
        # line); consume it here so cleanup never re-raises what we're handling
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await pump_task
        proc.kill()
        stderr_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stderr_task
        with contextlib.suppress(asyncio.CancelledError):
            await proc.wait()

    try:
        done, _ = await asyncio.wait(waiters, timeout=timeout,
                                     return_when=asyncio.FIRST_COMPLETED)
        if not done:
            raise asyncio.TimeoutError  # nothing finished within the budget
        if interrupt_event is not None and interrupt_event.is_set():
            # human steered — honor it even when the pump finished in the SAME event
            # loop wakeup (both tasks in `done`); deciding on the event's own state
            # rather than task-set membership keeps the steer note from being
            # silently orphaned when a run happens to complete as the steer arrives.
            await _reap()
            log.info("claude stream run interrupted by steer (session %s)", session_id)
            return RawRunResult("interrupted", "interrupted by human steer",
                                {"session_id": session_id or resume_session})
        try:
            pump_task.result()  # pump finished — surface any pump exception
        except Exception:
            # a pump-level failure (e.g. an over-long line -> ValueError from
            # readline) must still reap the child, or it leaks as a zombie
            await _reap()
            log.exception("claude stream pump failed")
            return RawRunResult("error", "stream pump failed",
                                {"session_id": session_id or resume_session})
        try:
            # stdout is closed; a healthy CLI exits promptly — never wait forever
            await asyncio.wait_for(proc.wait(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
    except asyncio.TimeoutError:
        await _reap()
        log.error("claude stream run timed out after %ss", timeout)
        return RawRunResult("timeout", f"timed out after {timeout}s",
                            {"session_id": session_id})
    except asyncio.CancelledError:
        # graceful shutdown must never orphan a live claude that later pushes
        await _reap()
        raise
    finally:
        if steer_task is not None and not steer_task.done():
            steer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await steer_task
    try:
        stderr = (await asyncio.wait_for(stderr_task, timeout=10)).decode(errors="replace")
    except (asyncio.TimeoutError, asyncio.CancelledError):
        stderr = ""

    # same contract as run_claude_raw: a resume of a missing/pruned session
    # exits 0 having produced NOTHING — never let that flow into parsing
    if resume_session and proc.returncode == 0 and not saw_event:
        log.warning("resume of session %s found nothing: %s", resume_session, stderr[-300:])
        return RawRunResult("session_lost", stderr[-500:], {"session_id": resume_session})

    fallback_sid = session_id or (None if fork_session else resume_session)
    meta: dict = {"session_id": fallback_sid}
    result_text = ""
    if result_env is not None:
        result_text = result_env.get("result") or ""
        meta = {
            "cost_usd": result_env.get("total_cost_usd"),
            "num_turns": result_env.get("num_turns"),
            "duration_ms": result_env.get("duration_ms"),
            "session_id": result_env.get("session_id") or fallback_sid,
        }

    if proc.returncode != 0:
        log.error("claude (stream) exited %s: %s", proc.returncode, stderr[-2000:])
        return RawRunResult("error",
                            f"claude exited {proc.returncode}: {(result_text or stderr)[-2000:]}",
                            meta)
    if result_env is None:
        # exit 0 but the CLI never emitted a result envelope. If it produced
        # assistant text, mirror run_claude_raw's fallback (return the text as ok)
        # rather than failing a run that actually did work; a truly empty stream
        # (no text at all) stays an error, same as an unparsable raw run.
        if text_parts:
            return RawRunResult("ok", "".join(text_parts), meta)
        return RawRunResult("error", "stream ended without a result envelope", meta)
    return RawRunResult("ok", result_text, meta)


async def run_claude(settings: Settings, target: RepoTarget, workspace: str, prompt: str,
                     on_event=None) -> FixResult:
    """v1 contract used by sentry/task jobs: PR URL sniffing + NEEDS_INPUT/NO_FIX.
    With on_event, the run streams live progress (tool calls / text) exactly like
    stage runs — same return contract either way."""
    if on_event is not None:
        raw = await run_claude_stream(
            settings, workspace, prompt,
            allowed_tools=BASE_ALLOWED_TOOLS + target.allow,
            timeout=settings.claude_timeout_seconds,
            on_event=on_event,
        )
    else:
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
        res = FixResult("pr_opened", pr_url=pr_match.group(0), detail=result_text[-3000:], meta=raw.meta)
        # lifecycle capture is STRICT: only explicit `PR_URL:` lines are PRs this
        # run opened — a URL mentioned in prose must not get the kickoff. (The
        # pr_opened STATUS keeps the broad match: long-standing v1 behavior.)
        res.pr_urls = list(dict.fromkeys(PR_LINE_RE.findall(result_text)))
        return res
    if "NEEDS_INPUT:" in result_text:
        return FixResult("needs_input", detail=result_text.split("NEEDS_INPUT:", 1)[1].strip()[:8000], meta=raw.meta)
    return FixResult("no_fix", detail=result_text[-3000:], meta=raw.meta)
