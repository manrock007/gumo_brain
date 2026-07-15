import asyncio
import os
import time
from pathlib import Path

import pytest

from app.config import Settings
from app.fixer import run_claude_raw, run_claude_stream


def _fake_claude(tmp_path, script_body: str) -> str:
    script = tmp_path / "fake-claude"
    script.write_text(f"#!/bin/sh\n{script_body}\n")
    script.chmod(0o755)
    return str(script)


def _settings(tmp_path, binary: str) -> Settings:
    return Settings(data_dir=str(tmp_path), claude_binary=binary,
                    session_persistence=True)


class TestSessionIdFallback:
    def test_fork_with_nonjson_output_reports_no_session(self, tmp_path):
        """Seer round 6: a fork whose envelope can't be parsed must NOT fall back
        to the original session id — the next chat turn would resume (and
        pollute) the stage session instead of the lost fork."""
        s = _settings(tmp_path, _fake_claude(tmp_path, "echo 'not json'; exit 0"))
        raw = asyncio.run(run_claude_raw(
            s, str(tmp_path), "q", ["Read"], 30,
            resume_session="stage-sess-1", fork_session=True,
        ))
        assert raw.status == "ok"
        assert raw.meta.get("session_id") is None

    def test_plain_resume_with_nonjson_keeps_same_session(self, tmp_path):
        s = _settings(tmp_path, _fake_claude(tmp_path, "echo 'not json'; exit 0"))
        raw = asyncio.run(run_claude_raw(
            s, str(tmp_path), "q", ["Read"], 30, resume_session="sess-2",
        ))
        assert raw.meta.get("session_id") == "sess-2"  # a resume continues its own id

    def test_fresh_run_keeps_engine_owned_id(self, tmp_path):
        s = _settings(tmp_path, _fake_claude(tmp_path, "echo 'not json'; exit 0"))
        raw = asyncio.run(run_claude_raw(
            s, str(tmp_path), "q", ["Read"], 30, session_id="engine-uuid",
        ))
        assert raw.meta.get("session_id") == "engine-uuid"

    def test_resume_empty_stdout_is_session_lost(self, tmp_path):
        s = _settings(tmp_path, _fake_claude(
            tmp_path, "echo 'No conversation found' >&2; exit 0"))
        raw = asyncio.run(run_claude_raw(
            s, str(tmp_path), "q", ["Read"], 30, resume_session="gone",
        ))
        assert raw.status == "session_lost"

    def test_envelope_session_id_wins(self, tmp_path):
        s = _settings(tmp_path, _fake_claude(
            tmp_path,
            'echo \'{"result": "hi", "session_id": "forked-9", "total_cost_usd": 0.01}\''))
        raw = asyncio.run(run_claude_raw(
            s, str(tmp_path), "q", ["Read"], 30,
            resume_session="stage-sess-1", fork_session=True,
        ))
        assert raw.status == "ok"
        assert raw.meta["session_id"] == "forked-9"
        assert raw.text == "hi"


STREAM_SCRIPT = """
echo '{"type":"system","subtype":"init","session_id":"s-9"}'
echo '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read","input":{"file_path":"app/x.py"}}]}}'
echo '{"type":"assistant","message":{"content":[{"type":"text","text":"the answer"}]}}'
echo '{"type":"result","result":"the answer","session_id":"s-9","total_cost_usd":0.02,"num_turns":3,"duration_ms":1200}'
"""


class TestRunClaudeStream:
    """docs/CONVERSATIONS.md §5: same return contract as run_claude_raw, plus
    live on_event progress."""

    def test_events_and_result_envelope(self, tmp_path):
        s = _settings(tmp_path, _fake_claude(tmp_path, STREAM_SCRIPT))
        events = []
        raw = asyncio.run(run_claude_stream(
            s, str(tmp_path), "q", ["Read"], 30,
            on_event=lambda e, d: events.append((e, d)),
        ))
        assert raw.status == "ok"
        assert raw.text == "the answer"
        assert raw.meta["session_id"] == "s-9"
        assert raw.meta["cost_usd"] == 0.02
        assert ("status", "Read app/x.py") in events
        assert ("delta", "the answer") in events

    def test_resume_empty_output_is_session_lost(self, tmp_path):
        s = _settings(tmp_path, _fake_claude(
            tmp_path, "echo 'No conversation found' >&2; exit 0"))
        raw = asyncio.run(run_claude_stream(
            s, str(tmp_path), "q", ["Read"], 30, resume_session="gone",
        ))
        assert raw.status == "session_lost"

    def test_no_result_envelope_is_error(self, tmp_path):
        s = _settings(tmp_path, _fake_claude(
            tmp_path, "echo '{\"type\":\"system\",\"subtype\":\"init\"}'"))
        raw = asyncio.run(run_claude_stream(s, str(tmp_path), "q", ["Read"], 30))
        assert raw.status == "error"

    def test_fork_without_envelope_reports_no_session(self, tmp_path):
        # same Seer-round-6 rule as the raw runner: a fork with no envelope
        # must NOT fall back to the original session id
        s = _settings(tmp_path, _fake_claude(
            tmp_path, "echo '{\"type\":\"system\",\"subtype\":\"init\"}'"))
        raw = asyncio.run(run_claude_stream(
            s, str(tmp_path), "q", ["Read"], 30,
            resume_session="stage-sess-1", fork_session=True,
        ))
        assert raw.meta.get("session_id") is None

    def test_nonzero_exit_is_error_with_envelope_meta(self, tmp_path):
        s = _settings(tmp_path, _fake_claude(tmp_path, STREAM_SCRIPT + "\nexit 3"))
        raw = asyncio.run(run_claude_stream(s, str(tmp_path), "q", ["Read"], 30))
        assert raw.status == "error"
        assert raw.meta["session_id"] == "s-9"


class TestJanitorBothStores:
    def _mk_transcript(self, config_dir: str, sid: str, age_days: float):
        d = Path(config_dir) / "projects" / "-some-workspace"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{sid}.jsonl"
        f.write_text("{}")
        old = time.time() - age_days * 86400
        os.utime(f, (old, old))
        return f

    def test_prunes_stage_and_chat_stores_with_keepset(self, worker, tmp_path):
        """Seer round 6: artifact-primed chats write to claude_chat_config_dir —
        the janitor must sweep both stores, honoring the keep-set."""
        s = worker.settings
        stale_stage = self._mk_transcript(s.claude_config_dir, "stale-stage", 30)
        stale_chat = self._mk_transcript(s.claude_chat_config_dir, "stale-chat", 30)
        fresh_chat = self._mk_transcript(s.claude_chat_config_dir, "fresh-chat", 1)
        kept_stage = self._mk_transcript(s.claude_config_dir, "kept-live", 30)

        worker.intake_feature("feat-j1", title="F", project="web", request="r")
        worker.store.set_fields("feat-j1", resume_session_id="kept-live", resume_stage=3)
        worker.store.set_status("feat-j1", "awaiting_input")

        worker._prune_sessions()

        assert not stale_stage.exists()
        assert not stale_chat.exists()
        assert fresh_chat.exists()      # inside TTL
        assert kept_stage.exists()      # referenced by a live job's pending resume
