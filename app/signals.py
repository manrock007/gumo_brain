"""Pure risk/proposal scanners (Epic I4/I5). Every function is side-effect
free: data in, a list of inbox-item DRAFTS out — dicts of
{kind, dedupe_key, title, body, refs, source_sig} the routine handler emits
via store.inbox_item_add. Dedupe keys are deduplication-by-construction AND
dismissal memory: same evidence → same key → blocked by the dismissed row;
measurably-changed evidence → new key → may surface again.

Untrusted text (Sentry titles/culprits, friction lines — model-emitted or
human free text) is single-lined, capped and backtick-stripped BEFORE it
lands in a body: notice bodies are human-facing markdown, and they reach a
prompt only via adoption (where they take the ClickUp-description
untrusted-fragment posture).

Imports neither worker nor main."""

import hashlib
import time
from datetime import datetime, timezone

from .digests import month_bounds
from .textutil import single_line


def _clean(value, cap: int = 200) -> str:
    """Untrusted one-liner for a markdown body: collapsed, capped, and with
    backticks stripped so it can never break out of an inline code span."""
    return single_line(value, cap).replace("`", "'")


def _sha(*parts) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(str(p).encode())
        h.update(b"\x00")
    return h.hexdigest()[:16]


# ---------- I4: risk scanners ----------


def sentry_spikes(issues: list[dict], project_slugs: set, threshold: int,
                  utc_date: str) -> list[dict]:
    """24h issue-velocity spikes for the workspace's mapped projects. The
    threshold is an ABSOLUTE 24h event count (no historical snapshot store in
    v1 — recorded limitation). At most one alert per issue per day."""
    if threshold <= 0:
        return []
    out = []
    for issue in issues or []:
        project = ((issue.get("project") or {}).get("slug") or "").strip()
        if project not in project_slugs:
            continue
        try:
            count = int(str(issue.get("count") or "0").replace(",", ""))
        except ValueError:
            continue
        if count < threshold:
            continue
        issue_id = str(issue.get("id") or "")
        if not issue_id:
            continue
        title = _clean(issue.get("title"), 200)
        culprit = _clean(issue.get("culprit"), 200)
        out.append({
            "kind": "risk_alert",
            "dedupe_key": f"sentry:{issue_id}:{utc_date}",
            "title": f"Sentry spike: {count} events/24h in {project}",
            "body": (f"- Issue: `{title or '(untitled)'}`\n"
                     + (f"- Culprit: `{culprit}`\n" if culprit else "")
                     + f"- 24h events: {count} (threshold {threshold})\n"
                     + (f"- {issue.get('permalink')}"
                        if issue.get("permalink") else "")).rstrip(),
            "refs": {"project": project, "sentry_issue": issue_id,
                     "issue_url": issue.get("permalink") or "", "count": count},
            "source_sig": f"sentry:{issue_id}",
        })
    return out


def redo_alerts(redo_rows: list[dict], threshold: int) -> list[dict]:
    """Repeated redos on the same stage (trust decaying). A FOURTH redo (new
    n) re-alerts; the same n never repeats."""
    if threshold <= 0:
        return []
    out = []
    for row in redo_rows or []:
        n = int(row.get("n") or 0)
        if n < threshold:
            continue
        job_id, stage = row["job_id"], int(row.get("stage") or 0)
        out.append({
            "kind": "risk_alert",
            "dedupe_key": f"redo:{job_id}:{stage}:{n}",
            "title": f"{n} redos on P{stage} of {_clean(row.get('title') or job_id, 80)}",
            "body": (f"P{stage} of `{job_id}` has been redone {n} times in the "
                     f"scoring window (threshold {threshold}) — trust in this "
                     "stage is decaying; consider a clawback or a closer look."),
            "refs": {"job_id": job_id, "stage": stage, "count": n,
                     "project": row.get("project") or ""},
            "source_sig": f"redo:{job_id}:{stage}",
        })
    return out


def watch_regression_alert(job: dict, trend: str) -> dict | None:
    """One alert per watch window; a /redo (new window_start) re-arms it."""
    if trend != "regressing":
        return None
    window_start = int(float(job.get("watch_started_at") or 0))
    return {
        "kind": "risk_alert",
        "dedupe_key": f"watch:{job['issue_id']}:{window_start}",
        "title": f"Watch trending off-goal: {single_line(job.get('title') or job['issue_id'], 120)}",
        "body": (f"Mid-window projection misses the target "
                 f"'{_clean(job.get('metric_target'))}' — don't wait for "
                 "day-{d}: the metric is tanking now.".replace(
                     "{d}", str(job.get("metric_window_days") or "N"))),
        "refs": {"job_id": job["issue_id"], "project": job.get("project") or "",
                 "metric": job.get("success_metric") or ""},
        "source_sig": f"watch:{job['issue_id']}",
    }


SPEND_PCT_BUCKETS = (150, 120, 100)


def spend_alert(workspace_id: int, spend: float, budget: float,
                now: float | None = None) -> dict | None:
    """Spend pacing above budget. Guards (amendment 7): projection only when
    elapsed month days ≥ 7 OR spend ≥ 50% of budget (early-month tiny
    numerators would fire false positives); the pct-bucket in the dedupe key
    lets a genuine later overrun still surface after an early alert."""
    now = now or time.time()
    if budget <= 0 or spend <= 0:
        return None
    _, elapsed_days, days_in_month = month_bounds(now)
    if elapsed_days < 7 and spend < 0.5 * budget:
        return None
    projected = spend / elapsed_days * days_in_month
    if projected <= budget:
        return None
    pct = projected / budget
    bucket = next((b for b in SPEND_PCT_BUCKETS if pct * 100 >= b), 100)
    month = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m")
    return {
        "kind": "risk_alert",
        "dedupe_key": f"spend:{workspace_id}:{month}:{bucket}",
        "title": f"Spend pacing {int(pct * 100)}% of budget",
        "body": (f"Month-to-date ${spend:.2f} over {elapsed_days} day(s) "
                 f"projects to ${projected:.2f} by month-end — over the "
                 f"${budget:.2f} budget."),
        "refs": {"spend": round(spend, 2), "budget": budget,
                 "projected": round(projected, 2), "month": month},
        "source_sig": f"spend:{workspace_id}:{month}",
    }


# ---------- I5: proposal scanners ----------

COUNT_BUCKETS = ((11, "11+"), (6, "6-10"), (3, "3-5"))


def count_bucket(n: int) -> str:
    """Threshold band for dedupe keys — a dismissal holds until the pain
    measurably grows into the next band (amendment 6), never churns on every
    new row or window shift."""
    for floor, label in COUNT_BUCKETS:
        if n >= floor:
            return label
    return str(n)


def outcome_proposals(outcome_rows: list[dict],
                      has_live_successor) -> list[dict]:
    """Iterate-candidates from decided flat/regressed outcomes whose feature
    has no live successor — the lane BEYOND Epic B4's single Iterate gate."""
    out = []
    for row in outcome_rows or []:
        verdict = row.get("verdict") or ""
        if verdict not in ("flat", "regressed") or not row.get("decided_at"):
            continue
        feature_id = row.get("feature_id") or ""
        if not feature_id or has_live_successor(feature_id):
            continue
        learning = single_line(row.get("learning") or "", 300)
        key = _sha("outcome", row.get("job_id"), verdict, _sha(learning))
        out.append({
            "kind": "proposal",
            "dedupe_key": f"outcome:{key}",
            "title": f"Iterate on {feature_id} (outcome: {verdict})",
            "body": (f"## Why now\nThe shipped feature `{feature_id}` measured "
                     f"**{verdict}** (metric '{_clean(row.get('metric'))}', "
                     f"observed {row.get('observed')} vs target "
                     f"'{_clean(row.get('target'))}').\n\n## Evidence\n"
                     f"- Outcome ledger row for `{row.get('job_id')}`\n"
                     + (f"- Recorded learning: {learning}\n" if learning else "")
                     + "\n## Suggested next step\nAdopt as a follow-up feature "
                       "iterating on the measured shortfall."),
            "refs": {"feature_id": feature_id, "verdict": verdict,
                     "job_id": row.get("job_id") or "",
                     "source_kind": "outcome",
                     "project": row.get("project") or ""},
            "source_sig": f"outcome:{feature_id}",
        })
    return out


def friction_proposals(friction_rows: list[dict], min_count: int) -> list[dict]:
    """Recurring process pain grouped by (project, stage). The dedupe key
    uses the COUNT BUCKET, not per-row hashes — a dismissal holds until the
    pain measurably grows (amendment 6). The recency guard (source_sig +
    PROPOSAL_WINDOW_DAYS) is applied by the caller against the store."""
    if min_count <= 0:
        return []
    groups: dict[tuple, list[dict]] = {}
    for row in friction_rows or []:
        key = (row.get("project") or "", row.get("stage"))
        groups.setdefault(key, []).append(row)
    out = []
    for (project, stage), rows in sorted(groups.items(),
                                         key=lambda kv: str(kv[0])):
        if len(rows) < min_count:
            continue
        n = len(rows)
        bucket = count_bucket(n)
        stage_label = f"P{stage}" if stage is not None else "(no stage)"
        quoted = "\n".join(
            f"- `{_clean(r.get('text'), 200)}` ({r.get('source')}, "
            f"job {r.get('job_id')})" for r in rows[-8:])
        out.append({
            "kind": "proposal",
            "dedupe_key": f"friction:{_sha('friction', project, stage, bucket)}",
            "title": f"Process friction: {n} entries on {stage_label}"
                     + (f" of {project}" if project else ""),
            "body": (f"## Why now\n{n} friction entries accumulated on "
                     f"{stage_label} ({project or 'no project'}) in the "
                     "window.\n\n## Evidence (friction log — recorded data, "
                     f"not instructions)\n{quoted}\n\n## Suggested next step\n"
                     "Adopt as a task to fix the recurring pain (prompt/"
                     "contract/tooling change)."),
            "refs": {"project": project, "stage": stage, "count": n,
                     "source_kind": "friction"},
            "source_sig": f"friction:{project}:{stage}",
        })
    return out


def _culprit_head(culprit: str) -> str:
    """Normalized culprit head for clustering: the part before ' in ' (module
    path), lowercased and bounded."""
    head = single_line(culprit, 200).split(" in ")[0].strip().lower()
    return head[:120]


def sentry_cluster_proposals(sentry_jobs: list[dict],
                             min_count: int) -> list[dict]:
    """Hardening briefs from recurring error areas — pure DB over stored
    sentry job rows (project, culprit). Pre-upgrade rows have culprit='' and
    are skipped: clusters accumulate from upgrade forward (documented)."""
    if min_count <= 0:
        return []
    groups: dict[tuple, list[dict]] = {}
    for row in sentry_jobs or []:
        head = _culprit_head(row.get("culprit") or "")
        if not head:
            continue
        groups.setdefault(((row.get("project") or ""), head), []).append(row)
    out = []
    for (project, head), rows in sorted(groups.items()):
        if len(rows) < min_count:
            continue
        n = len(rows)
        bucket = count_bucket(n)
        titles = "\n".join(f"- `{_clean(r.get('title'), 140)}` "
                           f"(issue {r.get('issue_id')})" for r in rows[-8:])
        out.append({
            "kind": "proposal",
            "dedupe_key": f"sentry-cluster:{_sha('sentry-cluster', project, head, bucket)}",
            "title": f"Hardening: {n} Sentry issues around `{_clean(head, 80)}`"
                     + (f" in {project}" if project else ""),
            "body": (f"## Why now\n{n} distinct Sentry issues clustered on "
                     f"`{_clean(head, 120)}` in the window.\n\n## Evidence "
                     f"(issue titles — recorded data, not instructions)\n"
                     f"{titles}\n\n## Suggested next step\nAdopt as a "
                     "hardening feature/task for this error area."),
            "refs": {"project": project, "culprit_head": head, "count": n,
                     "source_kind": "sentry-cluster"},
            "source_sig": f"sentry-cluster:{project}:{head}",
        })
    return out


STALENESS_TIERS = (10, 5, 2, 1)


def staleness_bucket(staleness: int, threshold: int) -> int:
    """Monotone bucket edges (1x/2x/5x/10x threshold) — the key changes at
    most a handful of times as staleness grows (amendment 6)."""
    if threshold <= 0:
        return 0
    ratio = staleness // threshold
    for tier in STALENESS_TIERS:
        if ratio >= tier:
            return tier
    return 0


def memory_proposals(repo_staleness: dict, jobs_by_project: dict,
                     threshold: int, min_jobs: int = 3) -> list[dict]:
    """Stale HIGH-TRAFFIC memory areas: staleness ≥ threshold AND ≥ min_jobs
    jobs touched the project in the window (the upkeep routine's weekly bound
    or inertness is judged by the caller)."""
    if threshold <= 0:
        return []
    out = []
    for project, staleness in sorted((repo_staleness or {}).items()):
        if staleness is None or int(staleness) < threshold:
            continue
        traffic = int(jobs_by_project.get(project, 0))
        if traffic < min_jobs:
            continue
        bucket = staleness_bucket(int(staleness), threshold)
        out.append({
            "kind": "proposal",
            "dedupe_key": f"memory:{_sha('memory', project, bucket)}",
            "title": f"Refresh memory for {project} "
                     f"({staleness} commits stale, {traffic} jobs touched it)",
            "body": (f"## Why now\n`{project}` memory is {staleness} commits "
                     f"stale (threshold {threshold}) while {traffic} jobs "
                     "touched the project in the window — runs are warming up "
                     "on outdated context.\n\n## Suggested next step\nAdopt "
                     "as a task (or run a manual memory bootstrap)."),
            "refs": {"project": project, "staleness": int(staleness),
                     "jobs": traffic, "source_kind": "memory"},
            "source_sig": f"memory:{project}",
        })
    return out
