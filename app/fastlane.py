"""Fast-lane gate chat (docs/CONVERSATIONS.md §5): a direct streaming Messages
API call primed with the gate bundle — no subprocess, no repo checkout, no
locks. First tokens in ~1-2s. It answers from what the engine already wrote
down (gate summary, artifacts cached in the DB, guidance, the conversation);
when the question genuinely needs the repository it escalates itself by
opening its reply with the NEED_CODE_RUN marker, which the engine turns into
the existing tool-run slow lane.

Disabled unless settings.chat_fast_model is set AND a key is available
(CHAT_API_KEY, falling back to ANTHROPIC_API_KEY). The key is used ONLY here —
CLI runs keep their own auth untouched.
"""

import json
import logging
import time

import httpx

log = logging.getLogger("brain.fastlane")

ESCALATE_MARKER = "NEED_CODE_RUN"
# Hold back the head of the stream until we know it isn't the escalation
# marker — the marker must never reach the client as answer text.
HOLDBACK_CHARS = len(ESCALATE_MARKER) + 8

API_VERSION = "2023-06-01"


class _Holdback:
    """Buffers the first HOLDBACK_CHARS of the stream, then decides once:
    escalation (swallow everything) or normal (flush and pass through)."""

    def __init__(self, on_delta):
        self.on_delta = on_delta
        self.buffer = ""
        self.text = ""
        self.decided = False
        self.escalated = False

    def feed(self, chunk: str):
        self.text += chunk
        if self.escalated:
            return
        if self.decided:
            self.on_delta(chunk)
            return
        self.buffer += chunk
        if len(self.buffer.lstrip()) >= HOLDBACK_CHARS:
            self._decide()

    def close(self):
        if not self.decided and not self.escalated:
            self._decide()

    def _decide(self):
        self.decided = True
        if self.buffer.lstrip().startswith(ESCALATE_MARKER):
            self.escalated = True
        elif self.buffer:
            self.on_delta(self.buffer)
        self.buffer = ""


async def _consume(lines, on_delta) -> tuple[str, dict]:
    """Parse an Anthropic SSE line stream; forward text through the holdback.
    Returns (full_text, usage_meta). Factored out so tests can drive it with
    scripted lines — no network involved."""
    hold = _Holdback(on_delta)
    usage: dict = {}
    async for line in lines:
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload:
            continue
        try:
            ev = json.loads(payload)
        except json.JSONDecodeError:
            continue
        etype = ev.get("type")
        if etype == "content_block_delta":
            delta = ev.get("delta") or {}
            if delta.get("type") == "text_delta":
                hold.feed(delta.get("text") or "")
        elif etype == "message_start":
            u = (ev.get("message") or {}).get("usage") or {}
            if u.get("input_tokens") is not None:
                usage["input_tokens"] = u["input_tokens"]
        elif etype == "message_delta":
            u = ev.get("usage") or {}
            if u.get("output_tokens") is not None:
                usage["output_tokens"] = u["output_tokens"]
        elif etype == "error":
            err = (ev.get("error") or {}).get("message") or "stream error"
            raise RuntimeError(err[:300])
    hold.close()
    return hold.text, {**usage, "escalated": hold.escalated}


async def stream_answer(settings, system: str, messages: list[dict],
                        on_delta) -> tuple[str, str, dict]:
    """One fast-lane turn. Returns (status, text, meta):
      status: 'ok' (text is the answer, already streamed via on_delta)
            | 'escalate' (text is the marker line; nothing was streamed)
            | 'error' (text is the reason; nothing was streamed)
    meta carries duration_ms and lane='fast' for the gate_chat row."""
    url = settings.chat_api_base.rstrip("/") + "/v1/messages"
    headers = {
        "x-api-key": settings.effective_chat_api_key,
        "anthropic-version": API_VERSION,
        "content-type": "application/json",
    }
    body = {
        "model": settings.chat_fast_model,
        "max_tokens": settings.chat_fast_max_tokens,
        "system": system,
        "messages": messages,
        "stream": True,
    }
    started = time.monotonic()
    meta: dict = {"lane": "fast"}
    try:
        async with httpx.AsyncClient(timeout=settings.chat_fast_timeout_seconds) as client:
            async with client.stream("POST", url, headers=headers, json=body) as resp:
                if resp.status_code != 200:
                    detail = (await resp.aread()).decode(errors="replace")[:300]
                    log.warning("fast lane HTTP %s: %s", resp.status_code, detail)
                    return "error", f"HTTP {resp.status_code}: {detail}", meta
                text, usage = await _consume(resp.aiter_lines(), on_delta)
    except (httpx.HTTPError, RuntimeError, OSError) as e:
        log.warning("fast lane failed: %s", str(e)[:300])
        return "error", str(e)[:300], meta
    meta["duration_ms"] = (time.monotonic() - started) * 1000
    meta["num_turns"] = 1
    if usage.pop("escalated", False):
        return "escalate", text, meta
    if not text.strip():
        return "error", "empty fast-lane reply", meta
    return "ok", text, meta
