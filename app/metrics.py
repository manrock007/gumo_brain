"""Prometheus /metrics exposition (Epic F4).

Dependency-free: no prometheus-client (keeps the zero-config ethos). Metrics are
computed at scrape time from the store's read-only aggregations and rendered as
text exposition. Scrapes are cached with a small TTL so a scrape storm can't DoS
the DB.
"""

import time

from . import budgets

BUILD_VERSION = "epic-f"
_CACHE: dict = {"at": 0.0, "text": ""}
_TTL_SECONDS = 5.0


def _esc(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _label(pairs: dict) -> str:
    if not pairs:
        return ""
    inner = ",".join(f'{k}="{_esc(v)}"' for k, v in pairs.items())
    return "{" + inner + "}"


def _line(out: list, name: str, value, labels: dict | None = None):
    out.append(f"{name}{_label(labels or {})} {value}")


def render(store, worker, settings, workspaces=None) -> str:
    """Build the text exposition. Never raises — a broken aggregate is skipped
    rather than failing the whole scrape."""
    snap = store.metrics_snapshot()
    out: list[str] = []

    out.append("# HELP ctrlloop_build_info Build metadata.")
    out.append("# TYPE ctrlloop_build_info gauge")
    _line(out, "ctrlloop_build_info", 1,
          {"version": BUILD_VERSION, "backend": settings.db_backend})

    # queue depth = in-process queue (SQLite) + DB received/queued count
    qsize = 0
    try:
        qsize = worker.queue.qsize() if worker is not None else 0
    except Exception:
        qsize = 0
    depth = int(snap.get("queue_db_depth", 0))
    if not settings.multi_worker:
        depth = max(depth, qsize)
    out.append("# HELP ctrlloop_queue_depth Jobs waiting to run.")
    out.append("# TYPE ctrlloop_queue_depth gauge")
    _line(out, "ctrlloop_queue_depth", depth)

    out.append("# HELP ctrlloop_jobs_total Jobs by status and kind.")
    out.append("# TYPE ctrlloop_jobs_total gauge")
    for r in snap.get("jobs_by_status_kind", []):
        _line(out, "ctrlloop_jobs_total", r["n"],
              {"status": r["status"], "kind": r["kind"]})

    out.append("# HELP ctrlloop_runs_today Claude invocations started today.")
    out.append("# TYPE ctrlloop_runs_today gauge")
    _line(out, "ctrlloop_runs_today", int(snap.get("runs_today", 0)))

    out.append("# HELP ctrlloop_watch_jobs Active post-ship watch jobs.")
    out.append("# TYPE ctrlloop_watch_jobs gauge")
    _line(out, "ctrlloop_watch_jobs", int(snap.get("watch_jobs", 0)))

    out.append("# HELP ctrlloop_stage_run_cost_usd_total Cumulative stage-run cost (USD).")
    out.append("# TYPE ctrlloop_stage_run_cost_usd_total counter")
    _line(out, "ctrlloop_stage_run_cost_usd_total",
          round(float(snap.get("stage_run_cost_usd_total", 0.0)), 6))

    lat = snap.get("gate_latency") or {}
    out.append("# HELP ctrlloop_gate_latency_seconds Gate post->answer latency.")
    out.append("# TYPE ctrlloop_gate_latency_seconds summary")
    _line(out, "ctrlloop_gate_latency_seconds_sum", round(float(lat.get("sum", 0.0)), 3))
    _line(out, "ctrlloop_gate_latency_seconds_count", int(lat.get("count", 0)))

    out.append("# HELP ctrlloop_autonomy_level Per-cell autonomy level (0..3).")
    out.append("# TYPE ctrlloop_autonomy_level gauge")
    for r in snap.get("autonomy_levels", []):
        _line(out, "ctrlloop_autonomy_level", r["level"],
              {"workspace": str(r["workspace_id"]), "project": r["project"],
               "stage": str(r["stage"])})

    # budget spend per workspace (best-effort; requires the workspace service)
    if workspaces is not None:
        out.append("# HELP ctrlloop_budget_spent_usd Month-to-date spend per workspace.")
        out.append("# TYPE ctrlloop_budget_spent_usd gauge")
        try:
            for ws in store.workspace_list():
                st = budgets.budget_status(store, settings, ws)
                _line(out, "ctrlloop_budget_spent_usd", round(st["spent"], 4),
                      {"workspace": ws.get("slug") or str(ws.get("id"))})
        except Exception:
            pass

    return "\n".join(out) + "\n"


def render_cached(store, worker, settings, workspaces=None) -> str:
    now = time.time()
    if now - _CACHE["at"] < _TTL_SECONDS and _CACHE["text"]:
        return _CACHE["text"]
    text = render(store, worker, settings, workspaces)
    _CACHE["at"] = now
    _CACHE["text"] = text
    return text
