"""Epic G2 — secrets provider seam + allow-listed subprocess env."""

import os

import pytest

from app.config import Settings
from app.secrets import (EnvProvider, FileProvider, VaultProvider, NotConfigured,
                         build_subprocess_env, detect_auth_backend, read_secret,
                         resolve)
from app.fixer import _claude_cmd_env


def test_env_provider(monkeypatch):
    monkeypatch.setenv("SOME_KEY", "v1")
    p = EnvProvider()
    assert p.get("SOME_KEY") == "v1"
    assert p.get("MISSING_KEY_XYZ") is None


def test_file_provider(tmp_path, monkeypatch):
    (tmp_path / "MY_SECRET").write_text("hunter2\n")
    p = FileProvider(str(tmp_path))
    assert p.get("MY_SECRET") == "hunter2"
    # missing file falls back to env
    monkeypatch.setenv("ENV_ONLY", "fromenv")
    assert p.get("ENV_ONLY") == "fromenv"
    with pytest.raises(NotConfigured):
        p.get_required("NOPE_NOT_THERE")


def test_resolve_unknown_falls_back_to_env():
    s = Settings(secrets_provider="banana")
    assert isinstance(resolve(s), EnvProvider)


def test_resolve_file_and_vault(tmp_path):
    assert isinstance(resolve(Settings(secrets_provider="file", secrets_dir=str(tmp_path))),
                      FileProvider)
    assert isinstance(resolve(Settings(secrets_provider="vault")), VaultProvider)
    # file with no dir configured degrades to env
    assert isinstance(resolve(Settings(secrets_provider="file")), EnvProvider).__class__


def test_read_secret_at_path(tmp_path):
    f = tmp_path / "key.pem"
    f.write_text("PEMDATA")
    assert read_secret(Settings(), f"@{f}") == "PEMDATA"
    assert read_secret(Settings(), "@/no/such/file") is None
    assert read_secret(Settings(), "raw-value") == "raw-value"


def test_allowlist_excludes_secrets_includes_plumbing(monkeypatch):
    # plant a secret and a plumbing var
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "s3cr3t")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "oidc-s3cr3t")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-tok")
    env = build_subprocess_env(Settings(), extra={"GH_TOKEN": "ghtok", "CLICKUP_TOKEN": "cu"})
    assert "SENTRY_AUTH_TOKEN" not in env
    assert "OIDC_CLIENT_SECRET" not in env
    assert "DASHBOARD_PASSWORD" not in env
    assert env["PATH"] == "/usr/bin"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-tok"
    assert env["GH_TOKEN"] == "ghtok"
    assert env["CLICKUP_TOKEN"] == "cu"


def test_extra_allowlist_passthrough_but_never_denylisted(monkeypatch):
    monkeypatch.setenv("MY_EXTRA_VAR", "ok")
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "nope")
    s = Settings(subprocess_env_allowlist="MY_EXTRA_VAR,SENTRY_AUTH_TOKEN")
    env = build_subprocess_env(s)
    assert env["MY_EXTRA_VAR"] == "ok"
    # a hard-deny secret can never be re-added through the escape hatch
    assert "SENTRY_AUTH_TOKEN" not in env


def test_bedrock_git_families_pass_through(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "akid")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "bot")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    env = build_subprocess_env(Settings())
    assert env["AWS_ACCESS_KEY_ID"] == "akid"
    assert env["GIT_AUTHOR_NAME"] == "bot"
    assert detect_auth_backend(env) == "bedrock"


def test_session_id_never_inherited(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "leak")
    env = build_subprocess_env(Settings())
    assert "CLAUDE_CODE_SESSION_ID" not in env


def _env_from_builder(builder_kwargs, monkeypatch):
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "s3cr3t")
    monkeypatch.setenv("PATH", "/usr/bin")
    cmd, env = _claude_cmd_env(Settings(github_token="pat"), "prompt", ["Read"],
                              None, None, None, False, None, **builder_kwargs)
    return cmd, env


def test_cmd_env_raw_and_stream_exclude_secret(monkeypatch):
    # json (raw) builder
    _, env = _env_from_builder({"output_format": "json"}, monkeypatch)
    assert "SENTRY_AUTH_TOKEN" not in env
    assert env["GH_TOKEN"] == "pat"        # PAT fallback when no git_token
    assert env["PATH"] == "/usr/bin"
    # stream builder
    cmd, env = _env_from_builder({"output_format": "stream-json"}, monkeypatch)
    assert "SENTRY_AUTH_TOKEN" not in env
    assert "--verbose" in cmd


def test_cmd_env_uses_run_specific_git_token(monkeypatch):
    _, env = _env_from_builder({"git_token": "minted-install-tok"}, monkeypatch)
    assert env["GH_TOKEN"] == "minted-install-tok"
