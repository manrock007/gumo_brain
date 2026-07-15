import asyncio
import json

from app.fastlane import ESCALATE_MARKER, _consume, stream_answer
from app.feature_prompts import build_fastlane_messages, build_fastlane_system


def _sse(events):
    """Anthropic-style SSE lines from a list of event dicts."""
    async def lines():
        for ev in events:
            yield f"event: {ev.get('type')}"
            yield f"data: {json.dumps(ev)}"
            yield ""
    return lines()


def _delta(text):
    return {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}


class TestConsumeHoldback:
    def test_normal_stream_forwards_deltas(self):
        got = []
        text, meta = asyncio.run(_consume(_sse([
            {"type": "message_start", "message": {"usage": {"input_tokens": 900}}},
            _delta("Option B avoids a "),
            _delta("schema migration entirely."),
            {"type": "message_delta", "usage": {"output_tokens": 40}},
        ]), got.append))
        assert text == "Option B avoids a schema migration entirely."
        assert "".join(got) == text          # everything reached the client
        assert meta["escalated"] is False
        assert meta["input_tokens"] == 900 and meta["output_tokens"] == 40

    def test_marker_swallowed_even_split_across_chunks(self):
        got = []
        text, meta = asyncio.run(_consume(_sse([
            _delta("NEED_CO"), _delta("DE_RUN: must check billing/refunds.py"),
        ]), got.append))
        assert meta["escalated"] is True
        assert got == []                     # the marker never reaches the client
        assert text.startswith(ESCALATE_MARKER)

    def test_short_answer_under_holdback_still_flushes(self):
        got = []
        text, meta = asyncio.run(_consume(_sse([_delta("Yes.")]), got.append))
        assert text == "Yes."
        assert "".join(got) == "Yes."
        assert meta["escalated"] is False

    def test_marker_mid_answer_does_not_escalate(self):
        got = []
        text, meta = asyncio.run(_consume(_sse([
            _delta("It never needs NEED_CODE_RUN because the artifact covers it."),
        ]), got.append))
        assert meta["escalated"] is False
        assert "".join(got) == text


class TestStreamAnswerErrors:
    def test_connection_error_reports_error_status(self, tmp_path):
        from app.config import Settings

        s = Settings(data_dir=str(tmp_path), dashboard_password="test",
                     chat_fast_model="claude-sonnet-5", chat_api_key="k",
                     chat_api_base="http://127.0.0.1:9", chat_fast_timeout_seconds=2)
        status, text, meta = asyncio.run(stream_answer(s, "sys", [
            {"role": "user", "content": "q"}], lambda t: None))
        assert status == "error"
        assert meta["lane"] == "fast"


class TestFastlanePrompts:
    def test_messages_alternate_and_start_with_user(self):
        transcript = [
            {"role": "engine", "text": "tombstone from a lost turn"},  # leading engine drops
            {"role": "human", "text": "why B?"},
            {"role": "human", "text": "and what about cost?"},         # coalesces with prev
            {"role": "engine", "text": "B is simpler; cost is equal."},
        ]
        msgs = build_fastlane_messages(transcript, "final question")
        assert msgs[0]["role"] == "user"
        assert "why B?" in msgs[0]["content"] and "cost?" in msgs[0]["content"]
        assert msgs[1]["role"] == "assistant"
        assert msgs[-1] == {"role": "user", "content": "final question"}
        roles = [m["role"] for m in msgs]
        assert all(a != b for a, b in zip(roles, roles[1:]))  # strict alternation

    def test_first_question_is_single_user_message(self):
        msgs = build_fastlane_messages([], "why B?")
        assert msgs == [{"role": "user", "content": "why B?"}]

    def test_system_carries_bundle_and_escalation_contract(self):
        job = {"issue_id": "feat-x", "title": "Refunds", "analysis": "chose B",
               "question": "1. approve?", "evidence": "diff +120 -3"}
        system = build_fastlane_system(
            job=job, stage=3,
            inline_artifacts={"P3-design.md": "## Design\nuse model B"},
            guidance_entries=[{"stage": 2, "action": "redo", "text": "tighter recon"}],
        )
        assert "chose B" in system
        assert "P3-design.md" in system and "use model B" in system
        assert "tighter recon" in system
        assert "NEED_CODE_RUN" in system
        assert "NO access to the repository" in system
