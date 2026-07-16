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
        # custom-field defs by lowercased name: {name: {id, type, options{name: option_id}}}
        # — the gumo-speed conveyor contract (Stage board, PR fields, Decisions)
        self._fields: dict[str, dict] = {}

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
        await self.load_fields()

    async def load_fields(self):
        """Custom-field definitions for the list — resolved once at startup so
        field_set can address fields by NAME (the workflow contract names them:
        Stage, Backend PR, Web PR, App PR, Decisions, Dashboard, …)."""
        if not self.enabled:
            return
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(f"{API}/list/{self._list_id}/field", headers=self._headers)
                r.raise_for_status()
                self._fields = {}
                for f in r.json().get("fields", []):
                    options = {(o.get("name") or "").lower(): o.get("id")
                               for o in (f.get("type_config") or {}).get("options", [])}
                    self._fields[(f.get("name") or "").lower()] = {
                        "id": f["id"], "type": f.get("type"), "options": options,
                    }
                log.info("ClickUp custom fields: %s", sorted(self._fields))
        except Exception:
            log.exception("could not load ClickUp custom fields; field sync disabled")

    async def field_set(self, task_id: str, field_name: str, value) -> bool:
        """Set a custom field by NAME (dropdowns resolve option names to ids).
        Best-effort like everything here; unknown field/option = quiet no-op —
        the workspace schema is the human's, never a hard dependency."""
        if not self.enabled or not task_id:
            return False
        f = self._fields.get((field_name or "").lower())
        if not f:
            return False
        if f["type"] == "drop_down":
            value = f["options"].get((str(value) or "").lower())
            if value is None:
                return False
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    f"{API}/task/{task_id}/field/{f['id']}",
                    headers=self._headers, json={"value": value},
                )
                r.raise_for_status()
                return True
        except Exception:
            log.exception("ClickUp field_set %s failed for task %s", field_name, task_id)
            return False

    async def field_append(self, task_id: str, field_name: str, line: str) -> bool:
        """Append a line to a text field (read-then-write — the workflow contract
        says shared fields accumulate across stages, never overwrite)."""
        if not self.enabled or not task_id:
            return False
        f = self._fields.get((field_name or "").lower())
        if not f:
            return False
        current = ""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(f"{API}/task/{task_id}", headers=self._headers)
                r.raise_for_status()
                for cf in r.json().get("custom_fields", []):
                    if cf.get("id") == f["id"]:
                        current = str(cf.get("value") or "")
                        break
        except Exception:
            log.exception("ClickUp field_append read failed for task %s", task_id)
            return False
        combined = (current.rstrip() + "\n" if current.strip() else "") + line.strip()
        return await self.field_set(task_id, field_name, combined[:9000])

    async def get_task(self, task_id: str) -> dict | None:
        """Fetch an existing task (any list) — used to adopt user-submitted tickets
        and to read back artifact-mirror subtasks. Returns None on any failure;
        callers must treat None as 'unknown', never as 'empty'."""
        if not self.enabled or not task_id:
            return None
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(
                    f"{API}/task/{task_id}",
                    headers=self._headers,
                    params={"include_markdown_description": "true"},
                )
                if r.status_code == 404:
                    return {"missing": True, "id": str(task_id)}
                r.raise_for_status()
                d = r.json()
                return {
                    "id": str(d["id"]),
                    "name": d.get("name", ""),
                    "url": d.get("url", ""),
                    "list_id": str((d.get("list") or {}).get("id") or ""),
                    "archived": bool(d.get("archived")),
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

    async def list_tasks(self, list_id: str | None = None) -> list[dict] | None:
        """Open (non-closed) top-level tasks in a list — the intake scan reads
        this. Paginates a few pages so ordering quirks can't hide a fresh
        ticket behind engine-created ones. None on failure; callers treat None
        as 'unknown', never as 'empty'."""
        if not self.enabled:
            return None
        out: list[dict] = []
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                for page in range(3):  # 300 open tasks — far beyond the working set
                    r = await client.get(
                        f"{API}/list/{list_id or self._list_id}/task",
                        headers=self._headers,
                        params={"page": page, "order_by": "created"},
                    )
                    r.raise_for_status()
                    tasks = r.json().get("tasks", [])
                    for d in tasks:
                        out.append({
                            "id": str(d["id"]),
                            "name": d.get("name", ""),
                            "url": d.get("url", ""),
                            "list_id": str((d.get("list") or {}).get("id") or ""),
                        })
                    if len(tasks) < 100:  # short page = last page
                        return out
            return out
        except Exception:
            log.exception("ClickUp list_tasks failed")
            return None

    async def create_task(self, name: str, description: str,
                          list_id: str | None = None,
                          parent: str | None = None) -> tuple[str, str] | None:
        """Create a task — or a subtask when `parent` is given. Subtasks MUST be
        created in the parent's home list (pass list_id), not the autofix list."""
        if not self.enabled:
            return None
        payload: dict = {"name": name[:200], "markdown_description": description}
        if parent:
            payload["parent"] = parent
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    f"{API}/list/{list_id or self._list_id}/task",
                    headers=self._headers,
                    json=payload,
                )
                r.raise_for_status()
                data = r.json()
                return data["id"], data.get("url", "")
        except Exception:
            log.exception("ClickUp create_task failed (list=%s parent=%s)", list_id, parent)
            return None

    async def update_description(self, task_id: str, markdown: str) -> bool:
        if not self.enabled or not task_id:
            return False
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.put(
                    f"{API}/task/{task_id}",
                    headers=self._headers,
                    json={"markdown_description": markdown},
                )
                r.raise_for_status()
                return True
        except Exception:
            log.exception("ClickUp update_description failed for %s", task_id)
            return False

    async def set_assignee(self, task_id: str, user_id: str) -> bool:
        """Assign a ClickUp member (numeric user id) — triggers native notifications."""
        if not self.enabled or not task_id or not str(user_id).isdigit():
            return False
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.put(
                    f"{API}/task/{task_id}",
                    headers=self._headers,
                    json={"assignees": {"add": [int(user_id)]}},
                )
                r.raise_for_status()
                return True
        except Exception:
            log.exception("ClickUp set_assignee failed for %s", task_id)
            return False

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
        """Newest-last list of {id, text, date} comments (date: epoch seconds)."""
        if not self.enabled or not task_id:
            return []
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(f"{API}/task/{task_id}/comment", headers=self._headers)
                r.raise_for_status()
                items = r.json().get("comments", [])
                items.reverse()  # API returns newest first
                out = []
                for c in items:
                    try:
                        date = float(c.get("date", 0)) / 1000.0  # API returns ms
                    except (TypeError, ValueError):
                        date = 0.0
                    out.append({"id": str(c["id"]), "text": c.get("comment_text", ""), "date": date})
                return out
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
