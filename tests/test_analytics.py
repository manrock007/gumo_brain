"""Analytics adapter (Epic B3): the Null driver contract, the Mixpanel driver
against httpx.MockTransport (no network), and the fail-closed factory."""

import asyncio
import base64
import json

import httpx

from app.analytics import (
    ANALYTICS_PROVIDERS,
    MixpanelAnalytics,
    NullAnalytics,
    provider_for,
)
from app.config import Settings


MP_CONFIG = {"project_id": "123", "service_account": "sa.user", "secret": "s3cret"}


def _mixpanel(handler, config=None):
    return MixpanelAnalytics(config or dict(MP_CONFIG),
                             transport=httpx.MockTransport(handler))


class TestNullAnalytics:
    def test_contract(self):
        res = asyncio.run(NullAnalytics().query_metric("signups", 14))
        assert res["status"] == "unavailable"
        assert res["series"] == [] and res["total"] is None
        assert "no analytics provider" in res["detail"]


class TestMixpanelDriver:
    def test_series_parse_auth_and_dates(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization", "")
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, json={
                "data": {"values": {"signup_done": {
                    "2026-07-01": 3, "2026-07-02": 5}}}})

        mp = _mixpanel(handler)
        res = asyncio.run(mp.query_metric("signups", 7, event="signup_done",
                                          end=1751500000))  # 2025-07-02Z
        assert res["status"] == "ok"
        assert res["total"] == 8.0
        assert [p["value"] for p in res["series"]] == [3.0, 5.0]
        # basic auth = service_account:secret
        expected = base64.b64encode(b"sa.user:s3cret").decode()
        assert seen["auth"] == f"Basic {expected}"
        # the event param is the JSON-encoded ARRAY string (amendment 12)
        assert seen["params"]["event"] == json.dumps(["signup_done"])
        assert seen["params"]["type"] == "general"
        assert seen["params"]["unit"] == "day"
        # 7-day window ending at `end`
        assert seen["params"]["to_date"] == "2025-07-02"
        assert seen["params"]["from_date"] == "2025-06-26"

    def test_metric_name_falls_back_when_no_event(self):
        def handler(request):
            ev = json.loads(dict(request.url.params)["event"])[0]
            return httpx.Response(200, json={"data": {"values": {ev: {"2026-07-01": 2}}}})

        res = asyncio.run(_mixpanel(handler).query_metric("weekly signups", 3))
        assert res["status"] == "ok" and res["total"] == 2.0

    def test_401_is_error_not_unavailable(self):
        def handler(request):
            return httpx.Response(401, text="invalid credentials")

        res = asyncio.run(_mixpanel(handler).query_metric("m", 7, event="e"))
        assert res["status"] == "error"
        assert "401" in res["detail"]

    def test_malformed_json_is_error(self):
        def handler(request):
            return httpx.Response(200, text="not json at all")

        res = asyncio.run(_mixpanel(handler).query_metric("m", 7, event="e"))
        assert res["status"] == "error"

    def test_unexpected_shape_is_error(self):
        def handler(request):
            return httpx.Response(200, json={"data": {"values": {"e": "boom"}}})

        res = asyncio.run(_mixpanel(handler).query_metric("m", 7, event="e"))
        assert res["status"] == "error"

    def test_incomplete_config_is_error_without_http(self):
        def handler(request):  # must never be hit
            raise AssertionError("no HTTP with incomplete config")

        mp = _mixpanel(handler, config={"project_id": "123"})
        res = asyncio.run(mp.query_metric("m", 7, event="e"))
        assert res["status"] == "error" and "config incomplete" in res["detail"]

    def test_default_api_base_is_never_eu(self):
        assert MixpanelAnalytics({}).api_base == "https://mixpanel.com/api"
        assert MixpanelAnalytics({"api_base": "https://eu.mixpanel.com/api/"}
                                 ).api_base == "https://eu.mixpanel.com/api"


class TestProviderFactory:
    def _settings(self, tmp_path, **over):
        return Settings(data_dir=str(tmp_path), dashboard_password="test", **over)

    def test_registry_names(self):
        assert ANALYTICS_PROVIDERS == ("", "mixpanel")

    def test_no_config_yields_null(self, tmp_path):
        assert provider_for(self._settings(tmp_path), None).name == "null"

    def test_workspace_wins_over_instance_env(self, tmp_path):
        s = self._settings(tmp_path, analytics_provider="",
                           analytics_config="{}")
        ws = {"analytics_provider": "mixpanel",
              "analytics_config": json.dumps(MP_CONFIG)}
        p = provider_for(s, ws)
        assert p.name == "mixpanel" and p.project_id == "123"

    def test_instance_env_fallback(self, tmp_path):
        s = self._settings(tmp_path, analytics_provider="mixpanel",
                           analytics_config=json.dumps(MP_CONFIG))
        # a workspace row WITHOUT its own provider falls through to the instance
        p = provider_for(s, {"analytics_provider": "", "analytics_config": "{}"})
        assert p.name == "mixpanel"

    def test_unknown_provider_fails_closed_to_null(self, tmp_path):
        ws = {"analytics_provider": "amplitude", "analytics_config": "{}"}
        assert provider_for(self._settings(tmp_path), ws).name == "null"

    def test_malformed_config_fails_closed_to_null(self, tmp_path, caplog):
        ws = {"analytics_provider": "mixpanel",
              "analytics_config": '{"secret": "sup3r-secret'}  # broken JSON
        assert provider_for(self._settings(tmp_path), ws).name == "null"
        # the warning must never leak the config string (it holds the secret)
        assert "sup3r-secret" not in caplog.text
