"""Issue grading — decides whether a Sentry issue deserves a Claude run.

Filters out resolved/ignored issues, stale legacy noise, non-error levels,
and low-impact issues, so a flood of webhooks doesn't burn the daily budget.
"""

import time
from dataclasses import dataclass, field

from .config import Settings

ISO = "%Y-%m-%dT%H:%M:%S"


@dataclass
class Grade:
    accept: bool
    score: int = 0
    reasons: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return f"score={self.score}: " + "; ".join(self.reasons)


def _age_days(iso_ts: str | None) -> float | None:
    if not iso_ts:
        return None
    try:
        import datetime
        dt = datetime.datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return (time.time() - dt.timestamp()) / 86400
    except ValueError:
        return None


def grade_issue(issue: dict, settings: Settings, forced: bool = False) -> Grade:
    reasons: list[str] = []
    score = 0

    status = issue.get("status", "unresolved")
    if status != "unresolved":
        # already resolved / ignored / archived — the most common "don't invoke Claude" case
        if forced:
            reasons.append(f"status={status} (overridden by manual trigger)")
        else:
            return Grade(False, 0, [f"issue status is '{status}' — nothing to fix"])

    project_slug = (issue.get("project") or {}).get("slug", "")
    if settings.repo_for_project(project_slug) is None:
        return Grade(False, 0, [f"no repo mapped for project '{project_slug}'"])

    level = issue.get("level", "error")
    if level in ("info", "debug"):
        if not forced:
            return Grade(False, 0, [f"level '{level}' — not an error"])
        reasons.append(f"level={level} (overridden)")
    elif level == "fatal":
        score += 30
        reasons.append("level=fatal (+30)")
    elif level == "error":
        score += 20
        reasons.append("level=error (+20)")
    else:  # warning
        score += 5
        reasons.append(f"level={level} (+5)")

    last_seen_age = _age_days(issue.get("lastSeen"))
    if last_seen_age is not None and last_seen_age > settings.grade_stale_days and not forced:
        return Grade(False, score, [f"stale — last seen {last_seen_age:.0f}d ago"])

    if issue.get("isUnhandled"):
        score += 20
        reasons.append("unhandled (+20)")

    users = int(issue.get("userCount") or 0)
    if users >= 50:
        score += 30
        reasons.append(f"{users} users (+30)")
    elif users >= 10:
        score += 20
        reasons.append(f"{users} users (+20)")
    elif users >= 3:
        score += 10
        reasons.append(f"{users} users (+10)")
    else:
        reasons.append(f"{users} users (+0)")

    try:
        events = int(str(issue.get("count") or "0"))
    except ValueError:
        events = 0
    if events >= 1000:
        score += 20
        reasons.append(f"{events} events (+20)")
    elif events >= 100:
        score += 15
        reasons.append(f"{events} events (+15)")
    elif events >= 10:
        score += 10
        reasons.append(f"{events} events (+10)")

    if last_seen_age is not None and last_seen_age <= 1:
        score += 10
        reasons.append("active in last 24h (+10)")

    if issue.get("assignedTo"):
        score -= 10
        reasons.append("assigned to a human (-10)")

    if forced:
        return Grade(True, score, reasons + ["manual trigger — grading bypassed"])

    accept = score >= settings.grade_min_score
    if not accept:
        reasons.append(f"below threshold {settings.grade_min_score}")
    return Grade(accept, score, reasons)
