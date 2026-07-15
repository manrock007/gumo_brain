import asyncio

import pytest

from app.config import Settings
from app.worker import Worker


FINDING_BODY = "**Bug:** something is wrong<br><sub>Severity: HIGH</sub>\n<!-- BUG_PREDICTION -->"
RESOLVED_BODY = "*Resolved in [`abc`](x)*\n\n**Bug:** old\n<!-- BUG_PREDICTION -->"


def _worker(store, tmp_path, **over):
    kw = dict(data_dir=str(tmp_path), dashboard_password="test",
              github_token="tok", shepherd_enabled=True, pr_max_review_rounds=6)
    kw.update(over)
    return Worker(Settings(**kw), store)


def _pr(store, url="https://github.com/manrock007/gumoserver/pull/5",
        job="feat-s1", state="in_review", rounds=1):
    store.insert(job, source="manual", kind="feature")
    store.pr_add(job, url)
    store.pr_set(url, state=state, review_rounds=rounds)
    return store.prs_for(job)[0]


class GH:
    """Scriptable stand-in for the GitHub client."""

    def __init__(self, pr=None, comments=None, reactions=None, reviews=None):
        self.enabled = True
        self.pr = pr
        self.comments = comments if comments is not None else []
        self.reactions = reactions if reactions is not None else []
        self.reviews = reviews if reviews is not None else []
        self.replies = []
        self.posted = []

    async def get_pr(self, repo, number):
        return self.pr

    async def mark_ready(self, repo, number):
        return True

    async def list_comments(self, repo, number):
        return self.comments

    async def get_comment_reactions(self, repo, comment_id):
        return self.reactions

    async def get_review_comments(self, repo, number):
        return self.reviews

    async def reply_to_review_comment(self, repo, number, comment_id, body):
        self.replies.append((comment_id, body))
        return True

    async def comment(self, repo, number, body):
        self.posted.append(body)
        return True


OPEN_PR = {"state": "open", "draft": False, "merged": False,
           "head": {"ref": "brain/feat-s1"}}


class TestShepherdStates:
    def test_merged_detection(self, store, tmp_path):
        w = _worker(store, tmp_path)
        pr = _pr(store)
        w.engine.github = GH(pr={"state": "closed", "merged": True})
        asyncio.run(w._shepherd_pr(pr))
        assert store.prs_for("feat-s1")[0]["state"] == "merged"

    def test_closed_without_merge(self, store, tmp_path):
        w = _worker(store, tmp_path)
        pr = _pr(store)
        w.engine.github = GH(pr={"state": "closed", "merged": False})
        asyncio.run(w._shepherd_pr(pr))
        assert store.prs_for("feat-s1")[0]["state"] == "closed"

    def test_clean_pass_hooray_approves(self, store, tmp_path):
        w = _worker(store, tmp_path)
        pr = _pr(store)
        w.engine.github = GH(
            pr=OPEN_PR,
            comments=[{"id": 1, "body": "@sentry review\n\n---\n_x_"}],
            reactions=[{"content": "hooray"}],
            reviews=[])
        asyncio.run(w._shepherd_pr(pr))
        assert store.prs_for("feat-s1")[0]["state"] == "approved"

    def test_review_in_flight_waits(self, store, tmp_path):
        """No findings, no 🎉 (👀 pending): the shepherd must not re-trigger."""
        w = _worker(store, tmp_path)
        pr = _pr(store)
        gh = GH(pr=OPEN_PR,
                comments=[{"id": 1, "body": "@sentry review"}],
                reactions=[{"content": "eyes"}],
                reviews=[{"id": 9, "body": RESOLVED_BODY}])
        w.engine.github = gh
        asyncio.run(w._shepherd_pr(pr))
        assert gh.posted == []
        assert store.prs_for("feat-s1")[0]["state"] == "in_review"


class TestShepherdFindings:
    def test_finding_fixed_replied_and_retriggered(self, store, tmp_path, monkeypatch):
        w = _worker(store, tmp_path)
        pr = _pr(store, rounds=2)
        gh = GH(pr=OPEN_PR, comments=[], reviews=[
            {"id": 42, "body": FINDING_BODY, "path": "app/x.py", "line": 10}])
        w.engine.github = gh

        async def fake_fix(pr_row, branch, findings):
            assert branch == "brain/feat-s1"
            assert [f["id"] for f in findings] == [42]
            return {42: ("FIXED", "guarded the None case")}

        monkeypatch.setattr(w, "_shepherd_fix", fake_fix)
        asyncio.run(w._shepherd_pr(pr))
        assert gh.replies == [(42, "Fixed — guarded the None case")]
        assert gh.posted == ["@sentry review"]
        row = store.prs_for("feat-s1")[0]
        assert row["state"] == "in_review" and row["review_rounds"] == 3

    def test_rebutted_finding_not_refixed_but_retriggered(self, store, tmp_path, monkeypatch):
        """A finding we already replied to must not be fixed again — but the
        bot only re-judges on an explicit trigger, so one is posted."""
        w = _worker(store, tmp_path)
        pr = _pr(store, rounds=2)
        gh = GH(pr=OPEN_PR, comments=[], reviews=[
            {"id": 42, "body": FINDING_BODY, "path": "app/x.py"},
            {"id": 43, "in_reply_to_id": 42, "body": "Not a real issue — see docs"}])
        w.engine.github = gh

        async def boom(*a, **k):
            raise AssertionError("must not re-fix a replied finding")

        monkeypatch.setattr(w, "_shepherd_fix", boom)
        asyncio.run(w._shepherd_pr(pr))
        assert gh.replies == []
        assert gh.posted == ["@sentry review"]

    def test_max_rounds_stalls_for_a_human(self, store, tmp_path, monkeypatch):
        w = _worker(store, tmp_path, pr_max_review_rounds=2)
        pr = _pr(store, rounds=2)
        gh = GH(pr=OPEN_PR, comments=[],
                reviews=[{"id": 42, "body": FINDING_BODY}])
        w.engine.github = gh

        async def boom(*a, **k):
            raise AssertionError("no fix past the round cap")

        monkeypatch.setattr(w, "_shepherd_fix", boom)
        asyncio.run(w._shepherd_pr(pr))
        row = store.prs_for("feat-s1")[0]
        assert row["state"] == "stalled"
        assert "human" in row["detail"]
        assert gh.posted == []

    def test_failed_fix_run_retries_later(self, store, tmp_path, monkeypatch):
        w = _worker(store, tmp_path)
        pr = _pr(store, rounds=1)
        gh = GH(pr=OPEN_PR, comments=[],
                reviews=[{"id": 42, "body": FINDING_BODY}])
        w.engine.github = gh

        async def failed(*a, **k):
            return None

        monkeypatch.setattr(w, "_shepherd_fix", failed)
        asyncio.run(w._shepherd_pr(pr))
        assert gh.posted == []  # no trigger on a failed round
        row = store.prs_for("feat-s1")[0]
        assert row["review_rounds"] == 1 and "retry" in row["detail"]


class TestShepherdFixRun:
    def test_verdicts_parsed_from_output(self, store, tmp_path, monkeypatch):
        import app.worker as worker_mod
        from app.fixer import RawRunResult

        w = _worker(store, tmp_path)
        pr = _pr(store)

        async def fake_ws(*a, **k):
            return str(tmp_path)

        async def fake_run(*a, **k):
            return RawRunResult("ok", "done\nFINDING 42: FIXED — guarded None\n"
                                      "FINDING 43: REBUT — stale commit", {})

        monkeypatch.setattr(worker_mod, "prepare_feature_workspace", fake_ws)
        monkeypatch.setattr(worker_mod, "run_claude_raw", fake_run)
        out = asyncio.run(w._shepherd_fix(pr, "brain/feat-s1",
                                          [{"id": 42, "body": "b", "path": "x"},
                                           {"id": 43, "body": "b", "path": "y"}]))
        assert out == {42: ("FIXED", "guarded None"), 43: ("REBUT", "stale commit")}

    def test_unknown_repo_skips(self, store, tmp_path):
        w = _worker(store, tmp_path)
        store.insert("feat-u1", source="manual", kind="feature")
        store.pr_add("feat-u1", "https://github.com/stranger/elsewhere/pull/1")
        pr = store.prs_for("feat-u1")[0]
        out = asyncio.run(w._shepherd_fix(pr, "some-branch", [{"id": 1, "body": "b"}]))
        assert out is None
        assert "no repo target" in store.prs_for("feat-u1")[0]["detail"]


class TestShepherdPass:
    def test_disabled_is_a_noop(self, store, tmp_path, monkeypatch):
        w = _worker(store, tmp_path, shepherd_enabled=False)
        _pr(store)

        async def boom(*a, **k):
            raise AssertionError("disabled shepherd must not touch PRs")

        monkeypatch.setattr(w, "_shepherd_pr", boom)
        asyncio.run(w._shepherd_pass())

    def test_pass_updates_last_checked_even_on_error(self, store, tmp_path, monkeypatch):
        w = _worker(store, tmp_path)
        pr = _pr(store)

        async def boom(*a, **k):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(w, "_shepherd_pr", boom)
        asyncio.run(w._shepherd_pass())
        row = store.prs_for("feat-s1")[0]
        assert row["last_checked"] is not None
        assert "error" in row["detail"]
