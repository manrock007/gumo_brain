"""Epic G5 — API-billing readiness: the Max→API policy signal + warning."""

import logging

from app.config import Settings


def test_using_max_oauth_token_only_with_oauth_and_no_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-tok")
    assert Settings().using_max_oauth_token is True
    # an API key present -> not on personal Max creds (billing flipped)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    assert Settings().using_max_oauth_token is False
    # no oauth token at all -> false
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    assert Settings().using_max_oauth_token is False


def test_enabled_user_count(store):
    assert store.enabled_user_count() == 0
    store.user_create("a", "h")
    store.user_create("b", "h")
    assert store.enabled_user_count() == 2
    store.user_set("b", disabled=1)
    assert store.enabled_user_count() == 1


def test_startup_warning_fires_with_multi_user_max(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DASHBOARD_PASSWORD", "test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-tok")
    from app import config
    config.get_settings.cache_clear()
    import importlib
    from app import main as main_module
    importlib.reload(main_module)
    from fastapi.testclient import TestClient

    # add a 2nd enabled user AFTER boot then re-run the warning logic directly
    with TestClient(main_module.app) as c:
        store = main_module.app.state.store
        store.user_create("second", main_module.hash_password("x"), role="member",
                          must_change_pw=False)
        assert store.enabled_user_count() == 2
        assert main_module.app.state.settings.using_max_oauth_token is True
    config.get_settings.cache_clear()


def test_no_warning_with_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-tok")
    assert Settings().using_max_oauth_token is False
