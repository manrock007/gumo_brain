"""AgentRuntime adapter (Epic H3, SCAFFOLD): the agent-invocation seam.

Abstracts WHICH agent implementation runs a prompt. The current (and default)
driver is the CLI subprocess — ``claude -p`` spawned via the runner — which IS
today's behavior byte-for-byte: the hard-won envelope-parsing / session-lost /
reaper / interrupt contract lives in ``app/fixer.py`` and CLIRuntime delegates
to it unchanged. An Agent-SDK driver is stubbed as the documented migration
target; it raises ``NotConfigured`` at run time so a mis-set
``AGENT_RUNTIME=agent-sdk`` fails loudly (a run genuinely cannot proceed without
a runtime) — but the DEFAULT is ``cli``, so that path is never hit on a
zero-config install.

Seam mechanics (avoiding the fixer<->runtime cycle, per the H3 blockers):

- ``RawRunResult`` stays DEFINED in fixer and is imported here — it has no
  runtime deps, which keeps this module importable without re-entering fixer.
- The public ``fixer.run_claude_raw`` / ``fixer.run_claude_stream`` are thin
  shims that call ``runtime_for(settings).run/.run_stream``. CLIRuntime's
  methods delegate to the PRIVATE real bodies ``fixer._run_claude_raw_impl`` /
  ``_run_claude_stream_impl`` via a FUNCTION-LEVEL import. The dispatch chain is
  therefore ``shim -> runtime_for().run -> _impl`` — a single hop, never a
  second hop back through the public shim (which would be infinite recursion).

Interplay with Epic F3 (sandboxed runs, FLAG): this seam picks the agent
PROTOCOL (CLI subprocess vs in-process SDK). F3's runner picks the ISOLATION
(local exec vs disposable container) and wraps the subprocess spawn INSIDE the
CLI body. The two seams are orthogonal — F3 composes container-of-CLI; it does
not merge with H3.
"""

import logging
from abc import ABC, abstractmethod

from .fixer import RawRunResult
from .secrets import NotConfigured

log = logging.getLogger("brain.runtime")

# No empty-string member: a run always needs a runtime. '' → the default (cli).
AGENT_RUNTIMES = ("cli", "agent-sdk")


class AgentRuntime(ABC):
    """The H3 seam. Covers run / stream / resume / interrupt (BUILD-PLAN H3):
    resume = ``resume_session``; fork = ``fork_session``; stream = ``on_event``;
    interrupt = ``interrupt_event``. The result contract is ``RawRunResult`` —
    identical across drivers."""

    name = "base"

    @abstractmethod
    async def run(self, settings, workspace, prompt, allowed_tools, timeout, *,
                  resume_session=None, disallowed_tools=None, session_id=None,
                  fork_session=False, config_dir=None,
                  git_token=None) -> RawRunResult: ...

    @abstractmethod
    async def run_stream(self, settings, workspace, prompt, allowed_tools, timeout, *,
                         resume_session=None, disallowed_tools=None, session_id=None,
                         fork_session=False, config_dir=None, on_event=None,
                         interrupt_event=None, git_token=None) -> RawRunResult: ...

    def session_transcript_exists(self, settings, session_id, config_dir=None) -> bool:
        """Resume-availability query. File-backed CLI sessions override this; a
        driver whose sessions aren't file-backed answers from its own store."""
        from .fixer import session_transcript_exists
        return session_transcript_exists(settings, session_id, config_dir=config_dir)


class CLIRuntime(AgentRuntime):
    """DEFAULT driver — today's ``claude -p`` subprocess. run/run_stream delegate
    to the real bodies in fixer (function-level import breaks the cycle). This
    IS the "CLI driver = current run_claude_*" requirement."""

    name = "cli"

    async def run(self, settings, workspace, prompt, allowed_tools, timeout, *,
                  resume_session=None, disallowed_tools=None, session_id=None,
                  fork_session=False, config_dir=None, git_token=None) -> RawRunResult:
        from .fixer import _run_claude_raw_impl
        return await _run_claude_raw_impl(
            settings, workspace, prompt, allowed_tools, timeout,
            resume_session=resume_session, disallowed_tools=disallowed_tools,
            session_id=session_id, fork_session=fork_session, config_dir=config_dir,
            git_token=git_token)

    async def run_stream(self, settings, workspace, prompt, allowed_tools, timeout, *,
                         resume_session=None, disallowed_tools=None, session_id=None,
                         fork_session=False, config_dir=None, on_event=None,
                         interrupt_event=None, git_token=None) -> RawRunResult:
        from .fixer import _run_claude_stream_impl
        return await _run_claude_stream_impl(
            settings, workspace, prompt, allowed_tools, timeout,
            resume_session=resume_session, disallowed_tools=disallowed_tools,
            session_id=session_id, fork_session=fork_session, config_dir=config_dir,
            on_event=on_event, interrupt_event=interrupt_event, git_token=git_token)


class AgentSDKRuntime(AgentRuntime):
    """SCAFFOLD — the documented migration target. Instead of spawning
    ``claude -p`` subprocesses it would drive in-process Agent-SDK session
    objects: ``resume`` → SDK session resume, ``interrupt`` → SDK cancel, the G2
    env allow-list still applies to the SDK's tool sandbox. Raising here (rather
    than a silent no-op) is deliberate — a run cannot proceed without a real
    runtime, so a mis-set AGENT_RUNTIME must fail loud, not fake success."""

    name = "agent-sdk"

    async def run(self, settings, workspace, prompt, allowed_tools, timeout, *,
                  resume_session=None, disallowed_tools=None, session_id=None,
                  fork_session=False, config_dir=None, git_token=None) -> RawRunResult:
        raise NotConfigured("the Agent-SDK runtime is a scaffold — set AGENT_RUNTIME=cli")

    async def run_stream(self, settings, workspace, prompt, allowed_tools, timeout, *,
                         resume_session=None, disallowed_tools=None, session_id=None,
                         fork_session=False, config_dir=None, on_event=None,
                         interrupt_event=None, git_token=None) -> RawRunResult:
        raise NotConfigured("the Agent-SDK runtime is a scaffold — set AGENT_RUNTIME=cli")


def runtime_for(settings) -> AgentRuntime:
    """Resolve the agent-runtime driver from ``settings.agent_runtime``. Fail
    closed to the working DEFAULT (CLI) for an empty or unknown name."""
    name = (getattr(settings, "agent_runtime", "") or "").strip().lower()
    if name == "agent-sdk":
        return AgentSDKRuntime()
    if name not in ("", "cli"):
        log.warning("unknown AGENT_RUNTIME=%r — using cli", name[:40])
    return CLIRuntime()
