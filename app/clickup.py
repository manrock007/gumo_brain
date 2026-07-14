"""ClickUp integration — one task per issue; the conveyor belt for HITL input.

All calls are best-effort: a ClickUp outage degrades tracking, never fixing.
"""

import logging

import httpx

from .config import Settings

log = logging.getLogger("brain.clickup")

API = "https://api.clickup.com/api/v2"

# internal state -> candidate ClickUp status names (first match on the list wins)
STATUS_CANDIDATES = {
    "running": ["in progress", "in review", "active"],
    "awaiting_input": ["needs input", "blocked", "review", "in review", "in progress"],
    "pr_opened": ["accepted", "complete", "done", "closed"],
    "no_fix": ["rejected", "complete", "done", "closed"],
    "skipped": ["rejected", "complete", "done", "closed"],
    "error": ["to do", "open"],
    "timeout": ["to do", "open"],
}


class ClickUp:
    def __init__(self, settings: Settings):
        self.enabled = settings.clickup_enabled
        self._list_id = settings.clickup_list_id
        self._headers = {"Authorization": settings.clickup_token}
        self._statuses: list[str] = []

    async def load_statuses(self):
        if not self.enabled:
            return
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(f"{API}/list/{self._list_id}", headers=self._headers)
                r.raise_for_status()
                self._statuses = [s["status"].lower() for s in r.json().get("statuses", [])]
                log.info("ClickUp list statuses: %s", self._statuses)
        except Exception:
            log.exception("could not load ClickUp list statuses; status sync disabled")

    async def get_task(self, task_id: str) -> dict | None:
        """Fetch an existing task (any list) — used to adopt user-submitted tickets."""
        if not self.enabled or not task_id:
            return None
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(
                    f"{API}/task/{task_id}",
                    headers=self._headers,
                    params={"include_markdown_description": "true"},
                )
                r.raise_for_status()
                d = r.json()
                return {
                    "id": str(d["id"]),
                    "name": d.get("name", ""),
                    "url": d.get("url", ""),
                    "description": (
                        d.get("markdown_description")
                        or d.get("description")
                        or d.get("text_content")
                        or ""
                    ),
                }
        except Exception:
            log.exception("ClickUp get_task failed for %s", task_id)
            return None

    async def create_task(self, name: str, description: str) -> tuple[str, str] | None:
        if not self.enabled:
            return None
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    f"{API}/list/{self._list_id}/task",
                    headers=self._headers,
                    json={"name": name[:200], "markdown_description": description},
                )
                r.raise_for_status()
                data = r.json()
                return data["id"], data.get("url", "")
        except Exception:
            log.exception("ClickUp create_task failed")
            return None

    async def comment(self, task_id: str, text: str):
        if not self.enabled or not task_id:
            return
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    f"{API}/task/{task_id}/comment",
                    headers=self._headers,
                    json={"comment_text": text[:9000]},
                )
                r.raise_for_status()
        except Exception:
            log.exception("ClickUp comment failed for task %s", task_id)

    async def comments(self, task_id: str) -> list[dict]:
        """Newest-last list of {id, text} comments."""
        if not self.enabled or not task_id:
            return []
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(f"{API}/task/{task_id}/comment", headers=self._headers)
                r.raise_for_status()
                items = r.json().get("comments", [])
                items.reverse()  # API returns newest first
                return [{"id": str(c["id"]), "text": c.get("comment_text", "")} for c in items]
        except Exception:
            log.exception("ClickUp comments fetch failed for task %s", task_id)
            return []

    async def set_status(self, task_id: str, state: str):
        if not self.enabled or not task_id or not self._statuses:
            return
        wanted = next(
            (c for c in STATUS_CANDIDATES.get(state, []) if c in self._statuses), None
        )
        if wanted is None:
            return
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.put(
                    f"{API}/task/{task_id}",
                    headers=self._headers,
                    json={"status": wanted},
                )
                r.raise_for_status()
        except Exception:
            log.exception("ClickUp set_status(%s) failed for task %s", state, task_id)
