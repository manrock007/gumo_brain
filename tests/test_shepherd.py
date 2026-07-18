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


def _pr(store, url="https://github.com/acme/demo/pull/5",
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
        target = w.settings.target_for_repo("acme/demo")

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


class TestMergeScanStates:
    """Epic B4 mainline: the shepherd's scan includes approved/stalled/draft
    rows for merge/close detection ONLY — the normal lifecycle parks a PR at
    'approved' ("ready to merge") before the human merges it on GitHub, and
    without re-polling those rows prs.state never becomes 'merged' and the
    outcome watch never spawns. The review loop is never resumed for them."""

    MERGED = {"state": "closed", "merged": True}

    def _terminal_feature(self, store, url="https://github.com/acme/demo/pull/5",
                          state="approved"):
        store.feature_intake("feat-s1", title="F", project="web", stage=9,
                             success_metric="signups")
        store.set_status("feat-s1", "pr_opened")
        store.pr_add("feat-s1", url)
        store.pr_set(url, state=state, review_rounds=1)
        return url

    def test_approved_pr_merge_detected_via_pass_and_watch_spawns(self, store, tmp_path):
        w = _worker(store, tmp_path)
        url = self._terminal_feature(store, state="approved")
        w.engine.github = GH(pr=self.MERGED)
        asyncio.run(w._shepherd_pass())  # the PASS must scan approved rows
        assert store.pr_get(url)["state"] == "merged"
        row = store.get("watch-feat-s1")
        assert row is not None and row["status"] == "watching"

    @pytest.mark.parametrize("state", ["stalled", "draft"])
    def test_stalled_and_draft_pr_merges_detected(self, store, tmp_path, state):
        w = _worker(store, tmp_path)
        url = self._terminal_feature(store, state=state)
        w.engine.github = GH(pr=self.MERGED)
        asyncio.run(w._shepherd_pass())
        assert store.pr_get(url)["state"] == "merged"
        assert store.get("watch-feat-s1") is not None

    def test_open_approved_pr_is_never_redriven(self, store, tmp_path, monkeypatch):
        """An approved-but-open PR with lingering findings: merge scan only —
        no re-trigger, no fix run, state stays approved (post-approval pushes
        re-kick via engine.record_prs, not here)."""
        w = _worker(store, tmp_path)
        url = self._terminal_feature(store, state="approved")
        gh = GH(pr=OPEN_PR, comments=[],
                reviews=[{"id": 42, "body": FINDING_BODY}])
        w.engine.github = gh

        async def boom(*a, **k):
            raise AssertionError("merge-scan states must never reach the fix loop")

        monkeypatch.setattr(w, "_shepherd_fix", boom)
        asyncio.run(w._shepherd_pass())
        assert gh.posted == [] and gh.replies == []
        assert store.pr_get(url)["state"] == "approved"

    def test_open_draft_pr_is_not_marked_ready(self, store, tmp_path):
        """pr_auto_ready=false keeps PRs 'draft' by operator choice — the
        merge scan must not un-draft or trigger reviews on them."""
        w = _worker(store, tmp_path)
        url = self._terminal_feature(store, state="draft")
        gh = GH(pr={"state": "open", "draft": True, "merged": False,
                    "head": {"ref": "brain/feat-s1"}})
        w.engine.github = gh
        asyncio.run(w._shepherd_pass())
        assert gh.posted == []
        assert store.pr_get(url)["state"] == "draft"


class TestWatchSpawn:
    """Epic B4: a merged PR on a TERMINAL (pr_opened) feature with a metric
    spawns the outcome watch — idempotently, guarded, never mid-pipeline."""

    MERGED = {"state": "closed", "merged": True}

    @staticmethod
    def _pr_row(store, url="https://github.com/acme/demo/pull/5", job="feat-s1"):
        # NOT the module _pr helper: that re-inserts the job row, which resets
        # the feature's status and would defeat the terminal-status guard
        store.pr_add(job, url)
        store.pr_set(url, state="in_review", review_rounds=1)
        return store.pr_get(url)

    def _feature(self, store, job="feat-s1", status="pr_opened", **fields):
        store.feature_intake(job, title="Feature S1", project="web", stage=9,
                             workspace_id=7, clickup_task_id="cu1",
                             founder_dri="111", dev_dri="222", owner="222",
                             **fields)
        store.set_status(job, status)
        return store.get(job)

    def test_merged_pr_spawns_watch_with_copied_fields(self, store, tmp_path):
        w = _worker(store, tmp_path)
        self._feature(store, success_metric="signups", metric_target=">= 100",
                      metric_event="signup_done", metric_window_days=7)
        pr = self._pr_row(store)
        w.engine.github = GH(pr=self.MERGED)
        asyncio.run(w._shepherd_pr(pr))
        row = store.get("watch-feat-s1")
        assert row is not None
        assert row["kind"] == "watch" and row["status"] == "watching"
        assert row["success_metric"] == "signups"
        assert row["metric_target"] == ">= 100"
        assert row["metric_event"] == "signup_done"
        assert row["metric_window_days"] == 7
        assert row["related_jobs"] == "feat-s1"
        assert row["workspace_id"] == 7
        assert row["owner"] == "111"  # founder-owned Iterate gate
        # both DRIs ride along so roles.gate_owner enforces the gate
        assert row["founder_dri"] == "111" and row["dev_dri"] == "222"
        assert row["clickup_task_id"] == "cu1"
        assert abs((row["watch_deadline"] - row["watch_started_at"]) - 7 * 86400) < 5

    def test_window_defaults_from_settings_when_unset(self, store, tmp_path):
        w = _worker(store, tmp_path, metric_window_days_default=21)
        self._feature(store, success_metric="signups")
        pr = self._pr_row(store)
        w.engine.github = GH(pr=self.MERGED)
        asyncio.run(w._shepherd_pr(pr))
        assert store.get("watch-feat-s1")["metric_window_days"] == 21

    def test_second_merged_sibling_pr_is_idempotent(self, store, tmp_path):
        w = _worker(store, tmp_path)
        self._feature(store, success_metric="signups")
        pr1 = self._pr_row(store)
        pr2 = self._pr_row(store, url="https://github.com/acme/demo/pull/6")
        w.engine.github = GH(pr=self.MERGED)
        asyncio.run(w._shepherd_pr(pr1))
        first = store.get("watch-feat-s1")
        asyncio.run(w._shepherd_pr(pr2))
        assert store.get("watch-feat-s1")["created_at"] == first["created_at"]

    def test_no_metric_skips_with_one_note_per_feature(self, store, tmp_path):
        w = _worker(store, tmp_path)
        posted = []

        class CU:
            enabled = True

            async def comment(self, task_id, text):
                posted.append(text)

            async def set_status(self, task_id, state):
                pass

        w.clickup = CU()
        self._feature(store)  # no metric, no event
        pr1 = self._pr_row(store)
        pr2 = self._pr_row(store, url="https://github.com/acme/demo/pull/6")
        w.engine.github = GH(pr=self.MERGED)
        asyncio.run(w._shepherd_pr(pr1))
        asyncio.run(w._shepherd_pr(pr2))  # second merged PR: no second note
        assert store.get("watch-feat-s1") is None
        skips = [t for t in posted if "outcome watch skipped" in t]
        assert len(skips) == 1

    def test_mid_pipeline_merge_never_spawns(self, store, tmp_path):
        """Blocker 2: a PR merged while the feature is still live must not put
        a second gate on the shared ticket — the P9-approval path covers it."""
        w = _worker(store, tmp_path)
        self._feature(store, status="awaiting_input", success_metric="signups")
        pr = self._pr_row(store)
        w.engine.github = GH(pr=self.MERGED)
        asyncio.run(w._shepherd_pr(pr))
        assert store.get("watch-feat-s1") is None

    def test_watch_disabled_never_spawns(self, store, tmp_path):
        w = _worker(store, tmp_path, watch_enabled=False)
        self._feature(store, success_metric="signups")
        pr = self._pr_row(store)
        w.engine.github = GH(pr=self.MERGED)
        asyncio.run(w._shepherd_pr(pr))
        assert store.get("watch-feat-s1") is None

    def test_p9_approval_spawns_for_an_already_merged_pr(self, store, tmp_path):
        """The early-merge mirror hook: PR merged before P9 approval — the
        approval makes the feature terminal and spawns the watch then."""
        w = _worker(store, tmp_path)
        job = self._feature(store, status="awaiting_input",
                            success_metric="signups")
        # solo mode for the answer: enforcement is not what this test is about
        store.set_fields("feat-s1", founder_dri="", dev_dri="",
                         pr_url="https://github.com/acme/demo/pull/5")
        store.pr_add("feat-s1", "https://github.com/acme/demo/pull/5")
        store.pr_set("https://github.com/acme/demo/pull/5", state="merged")
        status = asyncio.run(w.answer_job("feat-s1", "proceed", "", via="dashboard"))
        assert status == "pr_opened"
        row = store.get("watch-feat-s1")
        assert row is not None and row["status"] == "watching"

    def test_p9_approval_without_merged_pr_does_not_spawn(self, store, tmp_path):
        w = _worker(store, tmp_path)
        self._feature(store, status="awaiting_input", success_metric="signups")
        store.set_fields("feat-s1", founder_dri="", dev_dri="",
                         pr_url="https://github.com/acme/demo/pull/5")
        store.pr_add("feat-s1", "https://github.com/acme/demo/pull/5")
        store.pr_set("https://github.com/acme/demo/pull/5", state="in_review")
        asyncio.run(w.answer_job("feat-s1", "proceed", "", via="dashboard"))
        assert store.get("watch-feat-s1") is None  # the shepherd spawns on merge
