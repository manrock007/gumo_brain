"""Analytics adapter (Epic B3 / seam H4): how the outcome loop reads a metric.

One async interface, drivers behind it. Result contract (a plain dict):

    {"status": "ok" | "unavailable" | "error",
     "series": [{"date": "YYYY-MM-DD", "value": float}],
     "total": float | None,          # window-to-date aggregate
     "detail": str}

- "ok"          — the series/total are real measurements.
- "unavailable" — no provider is configured (the null driver). Verdicts built
                  on this stay "unmeasured" — never a guess.
- "error"       — a configured provider failed (auth, network, parse). The
                  detail is surfaced on the watch job so a broken credential
                  is visible, but the verdict math treats it like no data.

Providers NEVER raise to callers; every failure is a status="error" result.
The factory fails closed: unknown provider names and malformed config both
degrade to NullAnalytics with a logged warning (the config string may carry a
secret, so only the error TYPE is ever logged).
"""

import datetime
import json
import logging

import httpx

from .config import Settings

log = logging.getLogger("brain.analytics")

# The registry of valid provider names — workspaces._clean_fields validates
# against THIS tuple, so the write-side allowlist and the factory can't drift
# when the next driver lands. '' = none (null driver).
ANALYTICS_PROVIDERS = ("", "mixpanel")


def _result(status: str, series: list | None = None, total: float | None = None,
            detail: str = "") -> dict:
    return {"status": status, "series": series or [], "total": total,
            "detail": detail[:500]}


class AnalyticsProvider:
    """The seam (H4). Drivers implement query_metric and never raise."""

    name = "base"

    async def query_metric(self, metric: str, window_days: int, *,
                           event: str = "", end: float | None = None) -> dict:
        raise NotImplementedError


class NullAnalytics(AnalyticsProvider):
    """The driver for instances without analytics: always 'unavailable', so
    outcomes render as 'unmeasured' instead of pretending to measure."""

    name = "null"

    async def query_metric(self, metric: str, window_days: int, *,
                           event: str = "", end: float | None = None) -> dict:
        return _result("unavailable", detail="no analytics provider configured")


class MixpanelAnalytics(AnalyticsProvider):
    """First real driver: Mixpanel's query API (service-account basic auth).

    Config keys: project_id, service_account, secret, api_base (default
    https://mixpanel.com/api; EU projects set https://eu.mixpanel.com/api in
    their config — the EU value is never a code default)."""

    name = "mixpanel"
    DEFAULT_API_BASE = "https://mixpanel.com/api"

    def __init__(self, config: dict, transport: httpx.AsyncBaseTransport | None = None):
        config = config or {}
        self.project_id = str(config.get("project_id") or "").strip()
        self.service_account = str(config.get("service_account") or "").strip()
        self.secret = str(config.get("secret") or "").strip()
        self.api_base = (str(config.get("api_base") or "").strip()
                         or self.DEFAULT_API_BASE).rstrip("/")
        self._transport = transport  # test seam — None means real HTTP

    async def query_metric(self, metric: str, window_days: int, *,
                           event: str = "", end: float | None = None) -> dict:
        ev = (event or metric or "").strip()
        if not ev:
            return _result("error", detail="no metric event to query")
        if not (self.project_id and self.service_account and self.secret):
            return _result("error", detail="mixpanel config incomplete "
                                           "(project_id/service_account/secret)")
        import time as _time
        end_ts = end if end is not None else _time.time()
        window_days = max(1, int(window_days or 1))
        to_date = datetime.datetime.fromtimestamp(end_ts, tz=datetime.timezone.utc).date()
        from_date = to_date - datetime.timedelta(days=window_days - 1)
        params = {
            "project_id": self.project_id,
            # the API wants the JSON-encoded array STRING, not a repeated param
            "event": json.dumps([ev]),
            "type": "general",
            "unit": "day",
            "from_date": from_date.isoformat(),
            "to_date": to_date.isoformat(),
        }
        try:
            async with httpx.AsyncClient(timeout=30, transport=self._transport,
                                         auth=(self.service_account, self.secret)) as client:
                r = await client.get(f"{self.api_base}/query/events", params=params)
                if r.status_code != 200:
                    return _result("error",
                                   detail=f"mixpanel HTTP {r.status_code}: {r.text[:200]}")
                data = r.json()
        except Exception as e:
            return _result("error", detail=f"mixpanel query failed: {type(e).__name__}: "
                                           f"{str(e)[:200]}")
        try:
            values = ((data.get("data") or {}).get("values") or {}).get(ev) or {}
            series = [{"date": d, "value": float(v)} for d, v in sorted(values.items())]
            total = float(sum(p["value"] for p in series))
        except (TypeError, ValueError, AttributeError) as e:
            return _result("error", detail=f"mixpanel response unparseable: "
                                           f"{type(e).__name__}")
        return _result("ok", series=series, total=total,
                       detail=f"{len(series)} day(s), event '{ev}'")


def provider_for(settings: Settings, ws: dict | None,
                 transport: httpx.AsyncBaseTransport | None = None) -> AnalyticsProvider:
    """Resolve the provider for a workspace: workspace row wins, instance env
    is the fallback, everything else is the null driver. Fail closed: unknown
    provider names and malformed config JSON degrade to NullAnalytics (verdicts
    become 'unmeasured' — never a guess). The config string may hold a secret,
    so failures log the error TYPE only, never the value."""
    provider = ""
    raw = "{}"
    if ws is not None and str(ws.get("analytics_provider") or "").strip():
        provider = str(ws["analytics_provider"]).strip()
        raw = ws.get("analytics_config") or "{}"
    elif (settings.analytics_provider or "").strip():
        provider = settings.analytics_provider.strip()
        raw = settings.analytics_config or "{}"
    if not provider:
        return NullAnalytics()
    try:
        config = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(config, dict):
            raise ValueError("analytics_config is not an object")
    except (ValueError, TypeError) as e:
        log.warning("malformed analytics_config (%s) — analytics disabled for this scope",
                    type(e).__name__)
        return NullAnalytics()
    if provider == "mixpanel":
        return MixpanelAnalytics(config, transport=transport)
    log.warning("unknown analytics provider '%s' — analytics disabled for this scope",
                provider[:40])
    return NullAnalytics()
