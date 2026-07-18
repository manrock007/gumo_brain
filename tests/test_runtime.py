"""Epic H3 — agent-runtime seam. Factory resolution, ABC conformance,
scaffold-raises, and shim-dispatch delegation."""

import asyncio

import pytest

from app.config import Settings
from app.fixer import RawRunResult
from app.runtime import (
    AGENT_RUNTIMES,
    AgentRuntime,
    AgentSDKRuntime,
    CLIRuntime,
    runtime_for,
)
from app.secrets import NotConfigured


def _settings(**kw):
    return Settings(github_token="", **kw)


class TestFactory:
    def test_default_is_cli(self):
        assert isinstance(runtime_for(_settings()), CLIRuntime)

    def test_empty_string_falls_back_to_cli(self):
        assert isinstance(runtime_for(_settings(agent_runtime="")), CLIRuntime)

    def test_agent_sdk(self):
        r = runtime_for(_settings(agent_runtime="agent-sdk"))
        assert isinstance(r, AgentSDKRuntime)
        assert r.name == "agent-sdk"

    def test_unknown_fails_closed_to_cli(self):
        assert isinstance(runtime_for(_settings(agent_runtime="langgraph")), CLIRuntime)

    def test_providers_tuple(self):
        assert AGENT_RUNTIMES == ("cli", "agent-sdk")
        assert "" not in AGENT_RUNTIMES


class TestConformance:
    def test_cli_is_a_runtime(self):
        assert issubclass(CLIRuntime, AgentRuntime)

    def test_abstract_methods(self):
        assert set(AgentRuntime.__abstractmethods__) == {"run", "run_stream"}


class TestSDKScaffold:
    def test_run_raises_not_configured(self):
        r = AgentSDKRuntime()
        with pytest.raises(NotConfigured):
            asyncio.run(r.run(_settings(), "/ws", "p", ["Read"], 30))

    def test_run_stream_raises_not_configured(self):
        r = AgentSDKRuntime()
        with pytest.raises(NotConfigured):
            asyncio.run(r.run_stream(_settings(), "/ws", "p", ["Read"], 30))


class TestShimDispatch:
    """The fixer public shims dispatch through the resolved runtime; patching
    runtime_for proves the seam is on the path without a real subprocess."""

    def test_run_claude_raw_goes_through_runtime(self, monkeypatch):
        from app import fixer

        seen = {}

        class FakeRuntime(AgentRuntime):
            name = "fake"

            async def run(self, settings, workspace, prompt, allowed_tools, timeout,
                          **kw):
                seen["raw"] = (workspace, prompt, kw.get("git_token"))
                return RawRunResult("ok", "dispatched-raw", {})

            async def run_stream(self, *a, **k):
                raise AssertionError("wrong method")

        monkeypatch.setattr("app.runtime.runtime_for", lambda s: FakeRuntime())
        res = asyncio.run(fixer.run_claude_raw(
            _settings(), "/ws", "p", ["Read"], 30, git_token="tok"))
        assert res.text == "dispatched-raw"
        assert seen["raw"] == ("/ws", "p", "tok")

    def test_run_claude_stream_goes_through_runtime(self, monkeypatch):
        from app import fixer

        seen = {}

        class FakeRuntime(AgentRuntime):
            name = "fake"

            async def run(self, *a, **k):
                raise AssertionError("wrong method")

            async def run_stream(self, settings, workspace, prompt, allowed_tools,
                                 timeout, **kw):
                seen["stream"] = (workspace, kw.get("interrupt_event"))
                return RawRunResult("ok", "dispatched-stream", {})

        monkeypatch.setattr("app.runtime.runtime_for", lambda s: FakeRuntime())
        res = asyncio.run(fixer.run_claude_stream(
            _settings(), "/ws", "p", ["Read"], 30, interrupt_event=None))
        assert res.text == "dispatched-stream"
        assert seen["stream"][0] == "/ws"

    def test_cli_runtime_delegates_to_impl(self, monkeypatch):
        """CLIRuntime.run must call the PRIVATE impl (single hop), never the
        public shim — proving no infinite recursion."""
        from app import fixer

        called = {}

        async def fake_impl(settings, workspace, prompt, allowed_tools, timeout, **kw):
            called["impl"] = True
            return RawRunResult("ok", "impl-body", {})

        monkeypatch.setattr(fixer, "_run_claude_raw_impl", fake_impl)
        res = asyncio.run(CLIRuntime().run(_settings(), "/ws", "p", ["Read"], 30))
        assert called.get("impl") is True
        assert res.text == "impl-body"


class TestTranscriptCapability:
    def test_cli_runtime_exposes_transcript_query(self, monkeypatch):
        from app import fixer
        monkeypatch.setattr(fixer, "session_transcript_exists",
                            lambda s, sid, config_dir=None: True)
        assert CLIRuntime().session_transcript_exists(_settings(), "sess") is True
