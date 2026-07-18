"""Epic H1 — tracker seam. Factory resolution, ABC conformance, inert stub."""

import asyncio
import inspect

from app.clickup import ClickUp, ClickUpTracker
from app.config import Settings
from app.tracker import TRACKER_PROVIDERS, JiraTracker, Tracker, tracker_for

# the 14 verified methods every driver must satisfy
ABC_METHODS = (
    "load_statuses", "load_fields", "set_status", "comment", "comments",
    "get_task", "list_tasks", "create_task", "update_description",
    "set_assignee", "field_set", "field_append", "task_fields",
)


def _settings(**kw):
    base = dict(clickup_token="", clickup_list_id="")  # clickup_enabled -> False
    base.update(kw)
    return Settings(**base)


class TestFactory:
    def test_default_is_clickup(self):
        t = tracker_for(_settings())
        assert isinstance(t, ClickUpTracker)
        assert t.name == "clickup"

    def test_empty_string_falls_back_to_clickup(self):
        # '' must NOT select a null tracker — it would silently disable ClickUp
        t = tracker_for(_settings(tracker_provider=""))
        assert isinstance(t, ClickUpTracker)

    def test_jira_provider(self):
        t = tracker_for(_settings(tracker_provider="jira"))
        assert isinstance(t, JiraTracker)
        assert t.name == "jira"
        assert t.enabled is False

    def test_unknown_fails_closed_to_clickup(self):
        t = tracker_for(_settings(tracker_provider="asana"))
        assert isinstance(t, ClickUpTracker)

    def test_case_insensitive(self):
        assert isinstance(tracker_for(_settings(tracker_provider="JIRA")), JiraTracker)

    def test_providers_tuple_has_no_null_member(self):
        assert TRACKER_PROVIDERS == ("clickup", "jira")
        assert "" not in TRACKER_PROVIDERS


class TestConformance:
    def test_clickup_implements_every_abc_method(self):
        for m in ABC_METHODS:
            assert hasattr(ClickUpTracker, m), m
            assert inspect.iscoroutinefunction(getattr(ClickUpTracker, m)), m

    def test_alias_is_the_driver(self):
        assert ClickUp is ClickUpTracker

    def test_clickup_is_a_tracker(self):
        assert issubclass(ClickUpTracker, Tracker)
        assert isinstance(tracker_for(_settings()), Tracker)

    def test_no_extra_abstract_methods(self):
        # a fake with exactly the 14 methods (test doubles inject these) must
        # not trip on any NEW required method beyond the verified surface
        assert set(Tracker.__abstractmethods__) == set(ABC_METHODS)


class TestJiraStubInert:
    """Every method is an inert no-op returning the outage sentinel — a
    jira-configured box degrades to dashboard-only, never crashes."""

    def _jira(self):
        return JiraTracker(_settings(tracker_provider="jira"))

    def test_returns_are_outage_sentinels(self):
        j = self._jira()

        async def go():
            assert await j.get_task("1") is None
            assert await j.list_tasks() is None
            assert await j.create_task("n", "d") is None
            assert await j.comments("1") == []
            assert await j.task_fields("1") == {}
            assert await j.update_description("1", "x") is False
            assert await j.set_assignee("1", "9") is False
            assert await j.field_set("1", "Stage", "P2") is False
            assert await j.field_append("1", "Decisions", "x") is False
            # fire-and-forget setters return None and never raise
            assert await j.load_statuses() is None
            assert await j.load_fields() is None
            assert await j.set_status("1", "running") is None
            assert await j.comment("1", "hi") is None

        asyncio.run(go())

    def test_jira_is_a_tracker(self):
        assert isinstance(self._jira(), Tracker)
