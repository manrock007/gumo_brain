"""Workspace crash hygiene (dogfood-found on live): a run killed mid-write
(deploy restart) leaves dirty TRACKED files in the shared clone; without a
reset-first, every later `git checkout -B` refuses with 'Your local changes
would be overwritten' and the job errors forever. Real-git tests: origin is
a local bare repo; the workspace clone pre-exists so no network is touched."""

import asyncio
import subprocess
from pathlib import Path

import pytest

from app.config import Settings
from app.fixer import prepare_feature_workspace, prepare_workspace


def _sh(cwd, *args):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                        "HOME": str(cwd), "PATH": "/usr/bin:/bin:/usr/local/bin"})


@pytest.fixture()
def repo_env(tmp_path):
    """A bare 'origin' with a master branch + a feature branch, and a
    pre-existing workspace clone of it named after the repo."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _sh(origin, "git", "init", "--bare", "--initial-branch=master", ".")

    seed = tmp_path / "seed"
    seed.mkdir()
    _sh(seed, "git", "init", "--initial-branch=master", ".")
    (seed / "app.py").write_text("v1\n")
    _sh(seed, "git", "add", ".")
    _sh(seed, "git", "commit", "-m", "base")
    _sh(seed, "git", "remote", "add", "origin", str(origin))
    _sh(seed, "git", "push", "origin", "master")
    _sh(seed, "git", "checkout", "-b", "brain/feat-x")
    (seed / "feature.py").write_text("wip\n")
    _sh(seed, "git", "add", ".")
    _sh(seed, "git", "commit", "-m", "feature work")
    _sh(seed, "git", "push", "origin", "brain/feat-x")

    root = tmp_path / "workspaces"
    root.mkdir()
    workspace = root / "server"  # repo name 'o/server' -> dir 'server'
    subprocess.run(["git", "clone", str(origin), str(workspace)],
                   check=True, capture_output=True)

    settings = Settings(data_dir=str(tmp_path), dashboard_password="test",
                        repo_map='{"p": {"repo": "o/server", "base": "master"}}')
    return settings, settings.repo_for_project("p"), str(root), workspace


class TestCrashHygiene:
    def test_feature_prep_survives_dirty_tracked_files(self, repo_env):
        settings, target, root, workspace = repo_env
        # simulate the killed run: branch checked out, tracked file modified
        _sh(workspace, "git", "fetch", "origin", "brain/feat-x")
        _sh(workspace, "git", "checkout", "-B", "brain/feat-x", "origin/brain/feat-x")
        (workspace / "feature.py").write_text("half-written by the dead run\n")

        ws = asyncio.run(prepare_feature_workspace(
            settings, target, "brain/feat-x", stage=6, workspace_root=root))
        assert (Path(ws) / "feature.py").read_text() == "wip\n"  # origin wins

    def test_v1_prep_survives_dirty_tracked_files(self, repo_env):
        settings, target, root, workspace = repo_env
        (workspace / "app.py").write_text("half-written by the dead run\n")

        ws = asyncio.run(prepare_workspace(
            settings, target, "brain/fix-y", workspace_root=root))
        assert (Path(ws) / "app.py").read_text() == "v1\n"

    def test_v1_keep_branch_survives_dirty_tracked_files(self, repo_env):
        settings, target, root, workspace = repo_env
        _sh(workspace, "git", "fetch", "origin", "brain/feat-x")
        _sh(workspace, "git", "checkout", "-B", "brain/feat-x", "origin/brain/feat-x")
        (workspace / "feature.py").write_text("dirty\n")

        ws = asyncio.run(prepare_workspace(
            settings, target, "brain/feat-x", keep_branch=True, workspace_root=root))
        assert (Path(ws) / "feature.py").read_text() == "wip\n"
