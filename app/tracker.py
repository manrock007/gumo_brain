"""Tracker adapter (Epic H1, SCAFFOLD): the issue-tracker seam.

One async interface, drivers behind it. ClickUp is the current (and default)
driver — it IS today's behavior, byte-for-byte (see ``app/clickup.py``). A Jira
driver is stubbed here as an inert no-op so a deployment that flips
``TRACKER_PROVIDER=jira`` degrades to *tracker-off* (dashboard-only), exactly
like an un-configured ClickUp instance — it NEVER raises into control flow
(the §7 best-effort invariant). Mapping notes: ``docs/TRACKER-JIRA.md``.

The seam follows the established pattern (``app/analytics.py`` /
``app/secrets.py``): an ABC with a ``name`` attr + an ``enabled`` flag, concrete
drivers, a ``TRACKER_PROVIDERS`` allow-list tuple, and a fail-closed
``tracker_for(settings)`` factory that returns the DEFAULT (ClickUp) driver
when nothing — or an unknown name — is configured.

NORMALIZATION boundary (the result shapes a non-ClickUp driver MUST satisfy —
these are exactly what ``ClickUpTracker`` returns today):

- Ticket dict (get_task / list_tasks):
  ``{id, name, url, list_id, description, archived?, missing?}``.
- Comment dict (comments — feeds A1 answer attribution; the identity fields
  ``user_id`` and ``username`` are LOAD-BEARING and must not regress):
  ``{id, text, date (epoch seconds), user_id, username}``.
- Custom-field value map (task_fields): ``{lowercased field name: value}``.

Failure/absence sentinels are uniform so an outage and a stub look identical to
callers: ``None`` (get_task / list_tasks / create_task = "unknown"), ``False``
(field/description/assignee writes), ``[]`` (comments), ``{}`` (task_fields),
and silent no-ops for the fire-and-forget setters (set_status / comment /
load_*). Callers already treat ``None`` as "unknown", never "empty".

NOTE on the seam being "on the call path": after this SCAFFOLD the engine and
worker still reach the tracker through the single ``self.clickup`` attribute
(retyped to ``Tracker``), constructed once via ``tracker_for``. The factory is
therefore exercised once at construction, not per call — the ~40 ``self.clickup.*``
sites go through the interface object, not through ``tracker_for`` each time.
"""

import logging
from abc import ABC, abstractmethod

log = logging.getLogger("brain.tracker")

# The registry of valid provider names. Unlike ANALYTICS_PROVIDERS this has NO
# empty-string member: there is no "null tracker" steady state — an unconfigured
# box still runs the ClickUp driver (with enabled=False when the token is
# absent). '' is treated by the factory as "use the default driver".
TRACKER_PROVIDERS = ("clickup", "jira")


class Tracker(ABC):
    """The H1 seam. Drivers are best-effort and NEVER raise into control flow.

    ``enabled`` mirrors ``ClickUp.enabled`` — worker.py reads it to decide
    whether the HITL poller / field mirror runs. A disabled driver behaves as
    dashboard-only.
    """

    name = "base"
    enabled = False

    @abstractmethod
    async def load_statuses(self) -> None: ...

    @abstractmethod
    async def load_fields(self) -> None: ...

    @abstractmethod
    async def set_status(self, task_id: str, state: str) -> None: ...

    @abstractmethod
    async def comment(self, task_id: str, text: str) -> None: ...

    @abstractmethod
    async def comments(self, task_id: str) -> list[dict]: ...

    @abstractmethod
    async def get_task(self, task_id: str) -> dict | None: ...

    @abstractmethod
    async def list_tasks(self, list_id: str | None = None) -> list[dict] | None: ...

    @abstractmethod
    async def create_task(self, name: str, description: str,
                          list_id: str | None = None,
                          parent: str | None = None) -> tuple[str, str] | None: ...

    @abstractmethod
    async def update_description(self, task_id: str, markdown: str) -> bool: ...

    @abstractmethod
    async def set_assignee(self, task_id: str, user_id: str) -> bool: ...

    @abstractmethod
    async def field_set(self, task_id: str, field_name: str, value) -> bool: ...

    @abstractmethod
    async def field_append(self, task_id: str, field_name: str, line: str) -> bool: ...

    @abstractmethod
    async def task_fields(self, task_id: str) -> dict: ...


class JiraTracker(Tracker):
    """SCAFFOLD driver — an inert no-op. Every method returns the SAME
    "unknown/failure" sentinel ClickUp returns on outage, so an instance that
    sets ``TRACKER_PROVIDER=jira`` behaves exactly like tracker-off
    (dashboard-only) rather than crashing. A real driver replaces these bodies
    with Jira REST calls per ``docs/TRACKER-JIRA.md`` and flips ``enabled``.
    """

    name = "jira"
    enabled = False

    def __init__(self, settings):
        self.settings = settings
        log.info("Jira tracker is a scaffold (enabled=False) — tracking degrades "
                 "to dashboard-only. See docs/TRACKER-JIRA.md")

    async def load_statuses(self) -> None:
        return None

    async def load_fields(self) -> None:
        return None

    async def set_status(self, task_id: str, state: str) -> None:
        return None

    async def comment(self, task_id: str, text: str) -> None:
        return None

    async def comments(self, task_id: str) -> list[dict]:
        return []

    async def get_task(self, task_id: str) -> dict | None:
        return None

    async def list_tasks(self, list_id: str | None = None) -> list[dict] | None:
        return None

    async def create_task(self, name: str, description: str,
                          list_id: str | None = None,
                          parent: str | None = None) -> tuple[str, str] | None:
        return None

    async def update_description(self, task_id: str, markdown: str) -> bool:
        return False

    async def set_assignee(self, task_id: str, user_id: str) -> bool:
        return False

    async def field_set(self, task_id: str, field_name: str, value) -> bool:
        return False

    async def field_append(self, task_id: str, field_name: str, line: str) -> bool:
        return False

    async def task_fields(self, task_id: str) -> dict:
        return {}


def tracker_for(settings) -> Tracker:
    """Resolve the tracker driver from ``settings.tracker_provider``. Fail
    closed to the working DEFAULT (ClickUp) for an empty or unknown name — never
    to a silently-broken null tracker that would disable ClickUp on a
    zero-config box."""
    # deferred import avoids a clickup<->tracker module cycle (clickup imports
    # Tracker from here at module load).
    from .clickup import ClickUpTracker

    provider = (getattr(settings, "tracker_provider", "") or "").strip().lower()
    if provider == "jira":
        return JiraTracker(settings)
    if provider not in ("", "clickup"):
        log.warning("unknown TRACKER_PROVIDER=%r — using clickup", provider[:40])
    return ClickUpTracker(settings)
