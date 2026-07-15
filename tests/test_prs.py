import asyncio

import pytest

from app.config import Settings
from app.engine import all_pr_urls
from app.worker import Worker


class TestPrStore:
    def test_add_idempotent_and_parsed(self, store):
        assert store.pr_add("feat-1", "https://github.com/o/r/pull/7") is True
        assert store.pr_add("feat-1", "https://github.com/o/r/pull/7") is False
        assert store.pr_add("feat-1", "https://github.com/o/r/pull/8/") is True
        prs = store.prs_for("feat-1")
        assert [(p["repo"], p["number"], p["state"]) for p in prs] == [
            ("o/r", 7, "draft"), ("o/r", 8, "draft")]

    def test_set_and_state_query(self, store):
        store.pr_add("feat-2", "https://github.com/o/r/pull/9")
        store.pr_set("https://github.com/o/r/pull/9", state="in_review", review_rounds=2)
        rows = store.prs_in_state(("in_review", "changes_requested"))
        assert rows and rows[0]["review_rounds"] == 2


class TestCaptureAll:
    def test_all_pr_urls_dedup_ordered(self):
        text = """P5 done.
PR_URL: https://github.com/o/r/pull/21
some text
PR_URL: https://github.com/o/r/pull/22
STAGE_DONE: built group 2
PR_URL: https://github.com/o/r/pull/21
"""
        assert all_pr_urls(text) == [
            "https://github.com/o/r/pull/21", "https://github.com/o/r/pull/22"]

    def test_fixresult_captures_every_explicit_pr(self, tmp_path):
        from app.config import RepoTarget
        from app.fixer import run_claude

        # printf '%s' passes the JSON through verbatim (dash echo mangles the
        # embedded literal backslash-n escapes)
        script = tmp_path / "fake-claude"
        script.write_text(
            "#!/bin/sh\n"
            "printf '%s' '{\"result\": \"done\\nPR_URL: https://github.com/o/r/pull/3"
            "\\nPR_URL: https://github.com/o/r/pull/4\"}'\n")
        script.chmod(0o755)
        s = Settings(data_dir=str(tmp_path), claude_binary=str(script))
        res = asyncio.run(run_claude(s, RepoTarget(repo="o/r", base="main"),
                                     str(tmp_path), "fix"))
        assert res.status == "pr_opened"
        assert res.pr_urls == ["https://github.com/o/r/pull/3",
                               "https://github.com/o/r/pull/4"]

    def test_prose_mentioned_pr_is_not_captured(self, tmp_path):
        """Seer PR#10 round 1: a PR merely MENTIONED in prose (context, a related
        PR) must never enter the lifecycle capture — only explicit `PR_URL:`
        lines count, or the kickoff would un-draft/review external PRs."""
        from app.config import RepoTarget
        from app.fixer import run_claude

        script = tmp_path / "fake-claude"
        script.write_text(
            "#!/bin/sh\n"
            "echo '{\"result\": \"similar to https://github.com/other/repo/pull/99, "
            "no new PR needed\"}'\n")
        script.chmod(0o755)
        s = Settings(data_dir=str(tmp_path), claude_binary=str(script))
        res = asyncio.run(run_claude(s, RepoTarget(repo="o/r", base="main"),
                                     str(tmp_path), "fix"))
        assert res.pr_urls == []  # nothing for record_prs to kick off


class TestLifecycleKickoff:
    def _worker(self, store, tmp_path, token="tok", auto=True):
        s = Settings(data_dir=str(tmp_path), dashboard_password="test",
                     github_token=token, pr_auto_ready=auto)
        return Worker(s, store)

    def test_new_pr_marked_ready_and_review_requested(self, store, tmp_path, monkeypatch):
        w = self._worker(store, tmp_path)
        calls = []

        async def ok_ready(repo, number):
            calls.append(("ready", repo, number))
            return True

        async def ok_comment(repo, number, body):
            calls.append(("comment", repo, number, body))
            return True

        monkeypatch.setattr(w.engine.github, "mark_ready", ok_ready)
        monkeypatch.setattr(w.engine.github, "comment", ok_comment)
        asyncio.run(w.engine.record_prs("feat-k1", ["https://github.com/o/r/pull/5"]))
        assert ("ready", "o/r", 5) in calls
        assert ("comment", "o/r", 5, "@sentry review") in calls
        pr = store.prs_for("feat-k1")[0]
        assert pr["state"] == "in_review" and pr["review_rounds"] == 1

    def test_known_pr_skips_kickoff(self, store, tmp_path, monkeypatch):
        w = self._worker(store, tmp_path)
        store.pr_add("feat-k2", "https://github.com/o/r/pull/6")

        async def boom(*a, **k):
            raise AssertionError("kickoff must not run for a known PR")

        monkeypatch.setattr(w.engine.github, "mark_ready", boom)
        asyncio.run(w.engine.record_prs("feat-k2", ["https://github.com/o/r/pull/6"]))

    def test_approved_pr_rekicked_when_head_moved(self, store, tmp_path, monkeypatch):
        """Dogfood-found gap: build group 2 pushes MORE commits onto a PR the
        bot already approved. 'approved' is outside the shepherd's scan states,
        so record_prs must flip it back to in_review with a fresh trigger."""
        w = self._worker(store, tmp_path)
        url = "https://github.com/o/r/pull/59"
        store.pr_add("feat-k6", url)
        store.pr_set(url, state="approved", review_rounds=2, approved_head="aaa111")

        calls = []

        async def get_pr(repo, number):
            return {"head": {"sha": "bbb222"}}  # moved past the approved head

        async def ok_comment(repo, number, body):
            calls.append(body)
            return True

        monkeypatch.setattr(w.engine.github, "get_pr", get_pr)
        monkeypatch.setattr(w.engine.github, "comment", ok_comment)
        asyncio.run(w.engine.record_prs("feat-k6", [url]))
        assert calls == ["@sentry review"]
        pr = store.pr_get(url)
        assert pr["state"] == "in_review" and pr["review_rounds"] == 3
        assert "new commits after approval" in pr["detail"]

    def test_approved_pr_not_rekicked_on_same_head(self, store, tmp_path, monkeypatch):
        """A run that merely re-prints the PR_URL line (no push) must not burn
        a review round — the head compare keeps re-kicks to real pushes."""
        w = self._worker(store, tmp_path)
        url = "https://github.com/o/r/pull/60"
        store.pr_add("feat-k7", url)
        store.pr_set(url, state="approved", review_rounds=2, approved_head="aaa111")

        async def get_pr(repo, number):
            return {"head": {"sha": "aaa111"}}  # unchanged since the clean pass

        async def boom_comment(*a, **k):
            raise AssertionError("must not re-trigger on an unchanged head")

        monkeypatch.setattr(w.engine.github, "get_pr", get_pr)
        monkeypatch.setattr(w.engine.github, "comment", boom_comment)
        asyncio.run(w.engine.record_prs("feat-k7", [url]))
        pr = store.pr_get(url)
        assert pr["state"] == "approved" and pr["review_rounds"] == 2

    def test_approved_rekick_unknown_head_waits(self, store, tmp_path, monkeypatch):
        """get_pr None = unknown — never re-kick blind; the next run mentioning
        this PR retries the compare."""
        w = self._worker(store, tmp_path)
        url = "https://github.com/o/r/pull/61"
        store.pr_add("feat-k8", url)
        store.pr_set(url, state="approved", approved_head="aaa111")

        async def get_pr(repo, number):
            return None

        async def boom_comment(*a, **k):
            raise AssertionError("must not trigger while the head is unknown")

        monkeypatch.setattr(w.engine.github, "get_pr", get_pr)
        monkeypatch.setattr(w.engine.github, "comment", boom_comment)
        asyncio.run(w.engine.record_prs("feat-k8", [url]))
        assert store.pr_get(url)["state"] == "approved"

    def test_kickoff_disabled_records_only(self, store, tmp_path, monkeypatch):
        w = self._worker(store, tmp_path, auto=False)

        async def boom(*a, **k):
            raise AssertionError("kickoff disabled")

        monkeypatch.setattr(w.engine.github, "mark_ready", boom)
        asyncio.run(w.engine.record_prs("feat-k3", ["https://github.com/o/r/pull/7"]))
        assert store.prs_for("feat-k3")[0]["state"] == "draft"

    def test_github_failure_never_raises(self, store, tmp_path, monkeypatch):
        w = self._worker(store, tmp_path)

        async def boom(*a, **k):
            raise RuntimeError("github down")

        monkeypatch.setattr(w.engine.github, "mark_ready", boom)
        asyncio.run(w.engine.record_prs("feat-k4", ["https://github.com/o/r/pull/8"]))
        assert store.prs_for("feat-k4")[0]["state"] == "draft"  # recorded, kickoff failed

    def test_mark_ready_failure_still_requests_review(self, store, tmp_path, monkeypatch):
        """A PR that cannot be un-drafted still gets the review trigger — the
        shepherd retries readiness later; findings can start flowing meanwhile."""
        w = self._worker(store, tmp_path)

        async def no_ready(repo, number):
            return False

        async def ok_comment(repo, number, body):
            return True

        monkeypatch.setattr(w.engine.github, "mark_ready", no_ready)
        monkeypatch.setattr(w.engine.github, "comment", ok_comment)
        asyncio.run(w.engine.record_prs("feat-k5", ["https://github.com/o/r/pull/9"]))
        assert store.prs_for("feat-k5")[0]["state"] == "in_review"
