"""Epic H2 — VCS seam. Factory resolution, ABC conformance, clone/token
extraction, inert stub."""

import asyncio
import inspect

import httpx

from app.config import Settings
from app.github import GitHub, GitHubVCS
from app.vcs import VCS_PROVIDERS, GitLabVCS, VCS, vcs_for

ABC_METHODS = (
    "clone_url", "mint_git_token", "get_pr", "create_pr", "mark_ready",
    "comment", "reply_to_review_comment", "list_comments",
    "get_review_comments", "get_comment_reactions",
)


def _settings(**kw):
    base = dict(github_token="")
    base.update(kw)
    return Settings(**base)


class TestFactory:
    def test_default_is_github(self):
        v = vcs_for(_settings())
        assert isinstance(v, GitHubVCS)
        assert v.name == "github"

    def test_empty_string_falls_back_to_github(self):
        assert isinstance(vcs_for(_settings(vcs_provider="")), GitHubVCS)

    def test_gitlab_provider(self):
        v = vcs_for(_settings(vcs_provider="gitlab"))
        assert isinstance(v, GitLabVCS)
        assert v.name == "gitlab"
        assert v.enabled is False

    def test_unknown_fails_closed_to_github(self):
        assert isinstance(vcs_for(_settings(vcs_provider="bitbucket")), GitHubVCS)

    def test_transport_seam_passes_through(self):
        t = httpx.MockTransport(lambda req: httpx.Response(200, json={}))
        v = vcs_for(_settings(github_token="x"), transport=t)
        assert v._transport is t

    def test_providers_tuple_has_no_null_member(self):
        assert VCS_PROVIDERS == ("github", "gitlab")
        assert "" not in VCS_PROVIDERS


class TestConformance:
    def test_github_implements_every_abc_method(self):
        for m in ABC_METHODS:
            assert hasattr(GitHubVCS, m), m

    def test_alias_is_the_driver(self):
        assert GitHub is GitHubVCS

    def test_github_is_a_vcs(self):
        assert issubclass(GitHubVCS, VCS)
        assert isinstance(vcs_for(_settings()), VCS)

    def test_no_extra_abstract_methods(self):
        assert set(VCS.__abstractmethods__) == set(ABC_METHODS)

    def test_pr_methods_are_coroutines(self):
        for m in ABC_METHODS:
            if m == "clone_url":
                assert not inspect.iscoroutinefunction(getattr(GitHubVCS, m))
            else:
                assert inspect.iscoroutinefunction(getattr(GitHubVCS, m)), m


class TestCloneUrl:
    def test_github_clone_url(self):
        assert vcs_for(_settings()).clone_url("acme/demo") == \
            "https://github.com/acme/demo.git"

    def test_gitlab_clone_url_wellformed(self):
        # even the inert stub returns a usable HTTPS URL so a checkout never wedges
        assert vcs_for(_settings(vcs_provider="gitlab")).clone_url("acme/demo") == \
            "https://gitlab.com/acme/demo.git"


class TestMintGitToken:
    def test_app_off_returns_none(self):
        v = vcs_for(_settings(github_token="pat"))
        assert asyncio.run(v.mint_git_token("acme/demo")) is None

    def test_app_on_returns_only_app_kind(self, monkeypatch):
        import app.github as gh_mod

        async def fake_eff(settings, repo, transport=None):
            return ("inst-token", "app")

        monkeypatch.setattr(gh_mod, "GitHub", GitHubVCS)  # keep alias intact
        s = _settings(github_token="pat", github_app_id="1", github_app_private_key="k")
        v = GitHubVCS(s)
        monkeypatch.setattr("app.githubapp.effective_git_token", fake_eff)
        assert asyncio.run(v.mint_git_token("acme/demo")) == "inst-token"

    def test_app_on_pat_kind_returns_none(self, monkeypatch):
        async def fake_eff(settings, repo, transport=None):
            return ("pat-token", "pat")

        s = _settings(github_token="pat", github_app_id="1", github_app_private_key="k")
        v = GitHubVCS(s)
        monkeypatch.setattr("app.githubapp.effective_git_token", fake_eff)
        assert asyncio.run(v.mint_git_token("acme/demo")) is None

    def test_fixer_shim_delegates(self, monkeypatch):
        # the back-compat fixer.mint_git_token routes through the VCS driver
        from app import fixer
        s = _settings(github_token="pat")
        assert asyncio.run(fixer.mint_git_token(s, "acme/demo")) is None


class TestGitLabStubInert:
    def _gl(self):
        return GitLabVCS(_settings(vcs_provider="gitlab"))

    def test_pr_ops_are_inert(self):
        v = self._gl()

        async def go():
            assert await v.get_pr("r", 1) is None
            assert await v.create_pr("r", "h", "b", "t", "body") is None
            assert await v.mark_ready("r", 1) is False
            assert await v.comment("r", 1, "x") is False
            assert await v.reply_to_review_comment("r", 1, 2, "x") is False
            assert await v.list_comments("r", 1) is None
            assert await v.get_review_comments("r", 1) is None
            assert await v.get_comment_reactions("r", 2) is None
            assert await v.mint_git_token("r") is None

        asyncio.run(go())

    def test_gitlab_is_a_vcs(self):
        assert isinstance(self._gl(), VCS)
