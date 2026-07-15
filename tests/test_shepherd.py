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

    def test_failed_reactions_call_retries_not_no_reactions(self, store, tmp_path, monkeypatch):
        """Seer PR#11 round 3: a failed reactions call (None) must not read as
        'no reactions' — the pass retries later instead of misjudging approval."""
        w = _worker(store, tmp_path)
        pr = _pr(store, rounds=1)
        gh = GH(pr=OPEN_PR,
                comments=[{"id": 1, "body": "@sentry review"}],
                reviews=[{"id": 42, "body": FINDING_BODY}])

        async def fail_reactions(repo, comment_id):
            return None

        gh.get_comment_reactions = fail_reactions
        w.engine.github = gh

        async def boom(*a, **k):
            raise AssertionError("must not proceed to findings on an unknown reaction state")

        monkeypatch.setattr(w, "_shepherd_fix", boom)
        asyncio.run(w._shepherd_pr(pr))
        assert gh.posted == []
        assert store.prs_for("feat-s1")[0]["review_rounds"] == 1

    def test_missing_kickoff_trigger_is_recovered(self, store, tmp_path):
        """Seer PR#11 round 4: ready + no trigger comment + no findings means a
        review was NEVER requested (the kickoff's comment failed) — waiting
        would deadlock; the shepherd must post the trigger itself."""
        w = _worker(store, tmp_path)
        pr = _pr(store, state="ready", rounds=0)
        gh = GH(pr=OPEN_PR, comments=[], reviews=[])
        w.engine.github = gh
        asyncio.run(w._shepherd_pr(pr))
        assert gh.posted == ["@sentry review"]
        row = store.prs_for("feat-s1")[0]
        assert row["state"] == "in_review" and row["review_rounds"] == 1

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

    def test_replied_findings_wait_without_burning_rounds(self, store, tmp_path, monkeypatch):
        """Seer PR#11 round 1: findings we already replied to (fix or rebut) got
        their trigger in that pass — later passes must WAIT for the bot's
        re-judgment, not re-trigger every 3 minutes until the round cap."""
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
        assert gh.replies == [] and gh.posted == []
        row = store.prs_for("feat-s1")[0]
        assert row["review_rounds"] == 2 and "awaiting" in row["detail"]

    def test_missing_verdict_never_claims_fixed(self, store, tmp_path, monkeypatch):
        """Seer PR#11 round 1: a finding the run did not report must NOT get a
        false 'Fixed' reply — left unreplied, the next pass re-attempts it.
        A round with zero verdicts posts no trigger and burns no round."""
        w = _worker(store, tmp_path)
        pr = _pr(store, rounds=1)
        gh = GH(pr=OPEN_PR, comments=[],
                reviews=[{"id": 42, "body": FINDING_BODY}])
        w.engine.github = gh

        async def no_verdicts(*a, **k):
            return {}

        monkeypatch.setattr(w, "_shepherd_fix", no_verdicts)
        asyncio.run(w._shepherd_pr(pr))
        assert gh.replies == [] and gh.posted == []
        row = store.prs_for("feat-s1")[0]
        assert row["review_rounds"] == 1 and "no verdicts" in row["detail"]

    def test_partial_verdicts_reply_only_whats_reported(self, store, tmp_path, monkeypatch):
        w = _worker(store, tmp_path)
        pr = _pr(store, rounds=1)
        gh = GH(pr=OPEN_PR, comments=[], reviews=[
            {"id": 42, "body": FINDING_BODY}, {"id": 44, "body": FINDING_BODY}])
        w.engine.github = gh

        async def one_verdict(*a, **k):
            return {42: ("FIXED", "guarded")}

        monkeypatch.setattr(w, "_shepherd_fix", one_verdict)
        asyncio.run(w._shepherd_pr(pr))
        assert gh.replies == [(42, "Fixed — guarded")]  # 44 stays unreplied for next pass
        assert gh.posted == ["@sentry review"]
        assert store.prs_for("feat-s1")[0]["review_rounds"] == 2

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

    def test_fix_run_does_not_wait_on_the_main_repo_lock(self, store, tmp_path, monkeypatch):
        """Seer PR#11 round 5: a fix run can hold a lock for a full claude
        timeout — on the MAIN repo lock that starves pipeline stages for the
        duration. The shepherd uses its own clone + lock; a fix must complete
        while the main lock is held."""
        import app.worker as worker_mod
        from app.fixer import RawRunResult

        w = _worker(store, tmp_path)
        pr = _pr(store)
        roots = {}

        async def fake_ws(settings, target, branch, stage, workspace_root=None):
            roots["root"] = workspace_root
            return str(tmp_path)

        async def fake_run(*a, **k):
            return RawRunResult("ok", "FINDING 42: FIXED — done", {})

        monkeypatch.setattr(worker_mod, "prepare_feature_workspace", fake_ws)
        monkeypatch.setattr(worker_mod, "run_claude_raw", fake_run)
        target = w.settings.target_for_repo("manrock007/gumoserver")

        async def run():
            async with w.locks.for_repo(target.repo):  # a busy pipeline job
                return await asyncio.wait_for(
                    w._shepherd_fix(pr, "brain/feat-s1", [{"id": 42, "body": "b"}]),
                    timeout=5)

        out = asyncio.run(run())
        assert out == {42: ("FIXED", "done")}
        assert roots["root"].endswith("/shepherd")  # its own clone, not the main one

    def test_unknown_repo_skips(self, store, tmp_path):
        w = _worker(store, tmp_path)
        store.insert("feat-u1", source="manual", kind="feature")
        store.pr_add("feat-u1", "https://github.com/stranger/elsewhere/pull/1")
        pr = store.prs_for("feat-u1")[0]
        out = asyncio.run(w._shepherd_fix(pr, "some-branch", [{"id": 1, "body": "b"}]))
        assert out is None
        assert "no repo target" in store.prs_for("feat-u1")[0]["detail"]


class TestGitHubPagination:
    """Seer PR#11 round 2: page 1 alone holds the OLDEST 100 items — the latest
    '@sentry review' trigger (whose 🎉 signals the clean pass) and findings past
    100 would be silently missed without Link-header pagination."""

    def _gh(self, tmp_path, handler):
        import httpx
        from app.github import GitHub

        s = Settings(data_dir=str(tmp_path), github_token="tok")
        return GitHub(s, transport=httpx.MockTransport(handler))

    def test_follows_link_next_across_pages(self, tmp_path):
        import httpx

        def handler(request):
            if "page=2" in str(request.url):
                return httpx.Response(200, json=[{"id": 2}])
            return httpx.Response(
                200, json=[{"id": 1}],
                headers={"Link": f'<{str(request.url)}&page=2>; rel="next"'})

        gh = self._gh(tmp_path, handler)
        out = asyncio.run(gh.list_comments("o/r", 5))
        assert [c["id"] for c in out] == [1, 2]

    def test_mid_pagination_failure_returns_none(self, tmp_path):
        import httpx

        def handler(request):
            if "page=2" in str(request.url):
                return httpx.Response(502)
            return httpx.Response(
                200, json=[{"id": 1}],
                headers={"Link": f'<{str(request.url)}&page=2>; rel="next"'})

        gh = self._gh(tmp_path, handler)
        # a partial list would silently hide the newest items — must be None
        assert asyncio.run(gh.get_review_comments("o/r", 5)) is None


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
