"""Slack read ingestion (Epic D3 — FLAG, off by default).

A read-only poller captures decision-shaped messages (`!decision` prefix, or
the configured reaction emoji) from per-workspace channel allowlists as
decision-registry CANDIDATES — parked for human confirmation, NEVER
auto-committed: candidates are excluded from the FTS index, from every
prompt block, and from the default registry view until a human confirms.

SlackReader mirrors the Mixpanel-driver posture: never raises, returns typed
result dicts, httpx transport test seam. The bot token is a secret: it is
never interpolated into any detail/log line and never appears in any API
response (env-only config).

Slack API notes (documented limits):
- conversations.history returns messages by ORIGINAL ts, so a reaction added
  to a message older than the re-scan overlap window is not seen. The worker
  re-scans a bounded overlap (RESCAN_OVERLAP_SECONDS) behind the watermark;
  the (source, ref) dedupe absorbs the re-reads.
- Thread replies are NOT returned by conversations.history unless broadcast
  to the channel — out of scope for v1.
- Scopes needed: channels:history (public channels; add groups:history for
  private ones), reactions:read. chat.getPermalink needs no write scope.
"""

import logging

import httpx

from .config import Settings

log = logging.getLogger("brain.slack")

# re-scan window behind the watermark so late reactions on recent messages
# are still captured (bounded; dedupe absorbs the re-reads)
RESCAN_OVERLAP_SECONDS = 7 * 86400
# default pagination bound per channel per pass (SLACK_INGEST_MAX_PAGES
# overrides) — runaway-history guard. A bound-hit pass processes what it
# fetched but HOLDS the watermark: pages are newest-first, so the unfetched
# remainder is the OLDER segment and advancing would skip it forever.
MAX_PAGES_PER_PASS = 10
PAGE_LIMIT = 100

TITLE_CAP = 120
TEXT_CAP = 2000


class SlackReader:
    """Read-only Slack Web API wrapper. Never raises; every failure is a
    status='error' result with a bounded detail (auth headers stripped —
    the token never reaches a detail or log line)."""

    def __init__(self, settings: Settings,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.api_base = (settings.slack_api_base or "https://slack.com/api").rstrip("/")
        self._token = settings.slack_bot_token or ""
        self._transport = transport  # test seam — None means real HTTP

    async def _get(self, method: str, params: dict) -> dict:
        try:
            async with httpx.AsyncClient(
                    timeout=30, transport=self._transport,
                    headers={"Authorization": f"Bearer {self._token}"}) as client:
                r = await client.get(f"{self.api_base}/{method}", params=params)
                if r.status_code != 200:
                    return {"ok": False,
                            "error": f"HTTP {r.status_code}: {r.text[:200]}"}
                return r.json()
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}

    async def history(self, channel: str, oldest_ts: str,
                      cursor: str = "") -> dict:
        """One page of channel history strictly after oldest_ts.
        Returns {"status": "ok"|"error", "messages": [...],
                 "has_more": bool, "next_cursor": str, "detail": str}."""
        params = {"channel": channel, "oldest": str(oldest_ts),
                  "limit": PAGE_LIMIT, "inclusive": "false"}
        if cursor:
            params["cursor"] = cursor
        data = await self._get("conversations.history", params)
        if not data.get("ok"):
            return {"status": "error", "messages": [], "has_more": False,
                    "next_cursor": "",
                    "detail": str(data.get("error") or "unknown")[:200]}
        return {"status": "ok",
                "messages": data.get("messages") or [],
                "has_more": bool(data.get("has_more")),
                "next_cursor": str(((data.get("response_metadata") or {})
                                    .get("next_cursor") or "")),
                "detail": ""}

    async def permalink(self, channel: str, ts: str) -> str:
        """Best-effort message permalink; '' on any failure."""
        data = await self._get("chat.getPermalink",
                               {"channel": channel, "message_ts": str(ts)})
        if not data.get("ok"):
            return ""
        return str(data.get("permalink") or "")


def is_decision_shaped(msg: dict, emoji_name: str) -> bool:
    """A candidate is a human message whose text starts with `!decision`
    (after whitespace) OR that carries the configured reaction. Bot/system
    messages (subtype present, or bot_id) never qualify."""
    if not isinstance(msg, dict):
        return False
    if msg.get("subtype") or msg.get("bot_id"):
        return False
    text = str(msg.get("text") or "")
    if text.lstrip().lower().startswith("!decision"):
        return True
    emoji = (emoji_name or "").strip().strip(":")
    if emoji:
        for r in msg.get("reactions") or []:
            if str((r or {}).get("name") or "") == emoji:
                return True
    return False


def candidate_fields(msg: dict) -> dict:
    """Extract the candidate's title/text/author. The text is stored VERBATIM
    as untrusted input — it reaches prompts and the FTS index only after a
    human confirms the candidate (and Epic D4 never indexes candidates)."""
    text = str(msg.get("text") or "").strip()
    first = text.splitlines()[0] if text else ""
    if first.lstrip().lower().startswith("!decision"):
        first = first.lstrip()[len("!decision"):].strip(" :—-")
    author = str(msg.get("user") or "")
    return {
        "title": first[:TITLE_CAP],
        "text": text[:TEXT_CAP],
        "decided_by": f"slack:{author}" if author else "slack:unknown",
    }
