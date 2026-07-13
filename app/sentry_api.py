"""Sentry webhook verification and REST API helpers (EU region)."""

import hashlib
import hmac
import logging

import httpx

from .config import Settings

log = logging.getLogger("brain.sentry")


def verify_signature(body: bytes, signature: str | None, client_secret: str) -> bool:
    if not signature or not client_secret:
        return False
    expected = hmac.new(client_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


class SentryClient:
    def __init__(self, settings: Settings):
        self._base = settings.sentry_api_base.rstrip("/")
        self._org = settings.sentry_org
        self._headers = {"Authorization": f"Bearer {settings.sentry_auth_token}"}

    async def issue(self, issue_id: str) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{self._base}/organizations/{self._org}/issues/{issue_id}/",
                headers=self._headers,
            )
            r.raise_for_status()
            return r.json()

    async def latest_event(self, issue_id: str) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{self._base}/organizations/{self._org}/issues/{issue_id}/events/latest/",
                headers=self._headers,
            )
            r.raise_for_status()
            return r.json()

    async def top_unresolved_issues(self, limit: int = 25) -> list[dict]:
        """Most-frequent unresolved issues over the last 14 days (sweep source)."""
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{self._base}/organizations/{self._org}/issues/",
                headers=self._headers,
                params={"query": "is:unresolved", "sort": "freq",
                        "statsPeriod": "14d", "limit": limit},
            )
            r.raise_for_status()
            return r.json()

    async def resolve_short_id(self, short_id: str) -> str | None:
        """Resolve a short id like GUMO-1A to a numeric issue id."""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(
                    f"{self._base}/organizations/{self._org}/shortids/{short_id}/",
                    headers=self._headers,
                )
                r.raise_for_status()
                data = r.json()
                return str(data.get("groupId") or data.get("group", {}).get("id"))
        except Exception:
            log.warning("could not resolve short id %s", short_id)
            return None

    async def post_comment(self, issue_id: str, text: str) -> None:
        """Leave a note on the issue. Best-effort — never fails the job."""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    f"{self._base}/organizations/{self._org}/issues/{issue_id}/comments/",
                    headers=self._headers,
                    json={"text": text},
                )
                r.raise_for_status()
        except Exception:
            log.exception("failed to post Sentry comment on issue %s", issue_id)


def format_stacktrace(event: dict, max_frames: int = 30) -> str:
    """Flatten the exception entries of a Sentry event into readable text."""
    lines: list[str] = []
    for entry in event.get("entries", []):
        if entry.get("type") != "exception":
            continue
        for value in entry.get("data", {}).get("values", []):
            lines.append(f"{value.get('type', 'Error')}: {value.get('value', '')}")
            frames = (value.get("stacktrace") or {}).get("frames") or []
            # Sentry orders frames oldest->newest; the crash site is last
            for frame in frames[-max_frames:]:
                where = frame.get("filename") or frame.get("module") or "?"
                lines.append(
                    f"  at {frame.get('function', '?')} ({where}:{frame.get('lineNo', '?')})"
                )
                context = frame.get("context") or []
                for ctx_line_no, ctx_src in context:
                    marker = ">>" if ctx_line_no == frame.get("lineNo") else "  "
                    lines.append(f"    {marker} {ctx_line_no}: {ctx_src}")
    if not lines:
        # Fall back to the message entry for non-exception issues
        for entry in event.get("entries", []):
            if entry.get("type") == "message":
                lines.append(entry.get("data", {}).get("formatted", ""))
    return "\n".join(lines) or "(no stacktrace available)"


def extract_issue_ref(resource: str, payload: dict) -> tuple[str, str] | None:
    """Return (issue_id, action) from a webhook payload, or None if not applicable."""
    action = payload.get("action", "")
    data = payload.get("data", {})
    if resource == "event_alert":
        event = data.get("event", {})
        issue_id = event.get("issue_id") or event.get("groupID")
        return (str(issue_id), action) if issue_id else None
    if resource == "issue":
        issue = data.get("issue", {})
        issue_id = issue.get("id")
        return (str(issue_id), action) if issue_id else None
    return None
