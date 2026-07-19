"""Engine namespace (Epic 0.2): `.ctrlloop/` with a legacy `.gumo/` fallback.
ONE precedence rule governs every resolution helper — legacy wins when
present — so a pre-rename repo is never split-brained across two trees."""

import asyncio
import subprocess
from pathlib import Path

import pytest

from app.artifacts import feature_dir, features_dir, list_artifacts
from app.config import ENGINE_DIR, LEGACY_ENGINE_DIRS, Settings
from app.fixer import engine_dir, git_show_ns


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   cwd=cwd, check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path):
    ws = tmp_path / "repo"
    ws.mkdir()
    _git(ws, "init", "-q", "-b", "main")
    return ws


def _commit_all(ws, msg="c"):
    _git(ws, "add", "-A")
    _git(ws, "commit", "-q", "-m", msg)


def _fake_origin(ws, base="main"):
    """Make origin/<base> resolvable without a network remote."""
    _git(ws, "update-ref", f"refs/remotes/origin/{base}", "HEAD")


class TestEngineDir:
    def test_fresh_repo_uses_engine_dir(self, repo):
        assert engine_dir(str(repo)) == ENGINE_DIR == ".ctrlloop"

    def test_legacy_tree_wins(self, repo):
        (repo / ".gumo").mkdir()
        assert engine_dir(str(repo)) == ".gumo"

    def test_both_trees_legacy_wins(self, repo):
        (repo / ".gumo").mkdir()
        (repo / ".ctrlloop").mkdir()
        assert engine_dir(str(repo)) == ".gumo"
        assert LEGACY_ENGINE_DIRS == (".gumo",)


class TestArtifactHelpers:
    def test_feature_dir_follows_namespace(self, repo):
        assert feature_dir(str(repo), "feat-1") == ".ctrlloop/features/feat-1"
        (repo / ".gumo").mkdir()
        assert features_dir(str(repo)) == ".gumo/features"
        assert feature_dir(str(repo), "feat-1") == ".gumo/features/feat-1"

    def test_list_artifacts_follows_namespace(self, repo):
        d = repo / ".gumo" / "features" / "feat-1"
        d.mkdir(parents=True)
        (d / "P0-intake.md").write_text("x")
        (d / "P1-prd.md").write_text("x")
        assert list_artifacts(str(repo), "feat-1") == ["P0-intake.md", "P1-prd.md"]
        # a new-namespace repo resolves the other tree
        d2 = repo.parent / "repo2" / ".ctrlloop" / "features" / "feat-2"
        d2.mkdir(parents=True)
        (d2 / "P3-design.md").write_text("x")
        assert list_artifacts(str(repo.parent / "repo2"), "feat-2") == ["P3-design.md"]


class TestGitShowNs:
    def test_reads_legacy_then_current(self, repo):
        (repo / ".ctrlloop" / "memory").mkdir(parents=True)
        (repo / ".ctrlloop" / "memory" / "map.md").write_text("new-tree map")
        _commit_all(repo)
        code, out = asyncio.run(git_show_ns(str(repo), "HEAD", "memory/map.md"))
        assert code == 0 and "new-tree map" in out

    def test_legacy_wins_when_ref_has_both(self, repo):
        (repo / ".gumo" / "memory").mkdir(parents=True)
        (repo / ".gumo" / "memory" / "map.md").write_text("legacy map")
        (repo / ".ctrlloop" / "memory").mkdir(parents=True)
        (repo / ".ctrlloop" / "memory" / "map.md").write_text("new map")
        _commit_all(repo)
        code, out = asyncio.run(git_show_ns(str(repo), "HEAD", "memory/map.md"))
        assert code == 0 and "legacy map" in out

    def test_missing_everywhere_fails(self, repo):
        (repo / "x.txt").write_text("x")
        _commit_all(repo)
        code, _ = asyncio.run(git_show_ns(str(repo), "HEAD", "memory/map.md"))
        assert code != 0


class TestMemoryReads:
    def test_freshness_counts_legacy_tree(self, repo, tmp_path):
        from app.memory import MemoryReader

        (repo / ".gumo" / "memory").mkdir(parents=True)
        (repo / ".gumo" / "memory" / "map.md").write_text("m")
        _commit_all(repo, "memory commit")
        (repo / "code.py").write_text("pass")
        _commit_all(repo, "code commit 1")
        (repo / "code.py").write_text("pass  # 2")
        _commit_all(repo, "code commit 2")
        _fake_origin(repo)
        reader = MemoryReader(Settings(data_dir=str(tmp_path)))
        assert asyncio.run(reader.freshness(str(repo), "main")) == 2

    def test_freshness_counts_current_tree(self, repo, tmp_path):
        from app.memory import MemoryReader

        (repo / ".ctrlloop" / "memory").mkdir(parents=True)
        (repo / ".ctrlloop" / "memory" / "map.md").write_text("m")
        _commit_all(repo, "memory commit")
        (repo / "code.py").write_text("pass")
        _commit_all(repo, "code commit")
        _fake_origin(repo)
        reader = MemoryReader(Settings(data_dir=str(tmp_path)))
        assert asyncio.run(reader.freshness(str(repo), "main")) == 1

    def test_refresh_cache_reads_legacy_tree(self, repo, tmp_path):
        from app.memory import MemoryReader

        (repo / ".gumo" / "memory" / "changelog").mkdir(parents=True)
        (repo / ".gumo" / "memory" / "map.md").write_text("the legacy map")
        (repo / ".gumo" / "memory" / "changelog" / "2026-01-01-x.md").write_text("e")
        _commit_all(repo)
        _fake_origin(repo)
        reader = MemoryReader(Settings(data_dir=str(tmp_path)))
        asyncio.run(reader.refresh_cache("demo", str(repo), "main"))
        cached = reader.cached("demo")
        assert cached["exists"] and "the legacy map" in cached["files"]["map.md"]
        assert cached["meta"]["files"]["changelog"] == 1


class TestPromptNamespace:
    def _prompt(self, stage, ns):
        from app.config import RepoTarget
        from app.feature_prompts import build_stage_prompt

        return build_stage_prompt(
            target=RepoTarget(repo="acme/api", base="main"),
            branch="ctrlloop/feat-feat-1",
            job={"issue_id": "feat-1", "title": "T", "project": "api", "request": "r"},
            stage=stage, memory_context="", artifact_names=["P0-intake.md"],
            inline_artifacts={}, guidance_entries=[], ns=ns,
        )

    @pytest.mark.parametrize("ns", [".ctrlloop", ".gumo"])
    def test_stage_prompts_render_the_resolved_namespace(self, ns):
        other = ".gumo" if ns == ".ctrlloop" else ".ctrlloop"
        for stage in (0, 5, 8, 9):
            prompt = self._prompt(stage, ns)
            assert f"{ns}/" in prompt, (stage, ns)
            assert f"{other}/" not in prompt, (stage, ns)

    def test_default_namespace_is_current(self):
        assert ".ctrlloop/" in self._prompt(9, ENGINE_DIR)

    @pytest.mark.parametrize("ns", [".ctrlloop", ".gumo"])
    def test_bootstrap_prompt_renders_namespace(self, ns):
        from app.config import RepoTarget
        from app.feature_prompts import build_bootstrap_prompt

        for run in (1, 2):
            prompt = build_bootstrap_prompt(
                target=RepoTarget(repo="acme/api", base="main"), branch="b",
                project="api", is_canonical=True, run=run, ns=ns)
            assert f"{ns}/memory/" in prompt


class TestBranchPrefixValidation:
    def test_valid_prefixes_accepted(self, tmp_path):
        for good in ("ctrlloop", "brain", "team-1", "a.b"):
            assert Settings(data_dir=str(tmp_path), branch_prefix=good).branch_prefix == good

    @pytest.mark.parametrize("bad", ["", "a/b", ".hidden", "x.lock", "sp ace"])
    def test_git_invalid_prefixes_rejected(self, tmp_path, bad):
        with pytest.raises(Exception):
            Settings(data_dir=str(tmp_path), branch_prefix=bad)
