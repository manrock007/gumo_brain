"""Outcome-loop pure logic (Epic B4/B5): verdicts, the Iterate-gate packet,
and the product-memory entries. No I/O — everything here is testable without
a provider, a DB, or a workspace.

Verdict formula (transparent — the inputs are stored verbatim on the ledger
row as `verdict_inputs` and rendered on the gate so the human sees WHY):

1. observed = the latest current-window reading's window-to-date total.
   No successful readings -> "unmeasured" (fail closed — never a verdict
   without data).
2. A DIRECTION is parsed from the target text (leading <=, <, >=, > or words
   like 'under'/'below'/'reduce' vs 'at least'/'over'/'grow'). Decrease-goal
   metrics (error rate, latency, churn) are judged inverted — a successful
   reduction is 'moved', never 'regressed'.
3. Numeric target parseable -> 'moved' when the target is met in the goal
   direction; otherwise compare to baseline within the flat band. With NO
   explicit direction the target is assumed increase-goal for 'moved', but a
   would-be 'regressed' is downgraded to 'flat' (direction ambiguity must not
   mislabel a reduction) — the applied rule says so.
4. No numeric target: baseline available -> moved/flat/regressed by the
   ±band around baseline (direction-aware); no baseline either -> unmeasured.
"""

import json
import re
import time

from .config import ENGINE_DIR

# first number in a target text: "15", "1,200", "3.5%", ">= 40"
_NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*")
_DOWN_RE = re.compile(
    r"^\s*<=?|\b(?:under|below|at\s+most|max(?:imum)?|reduce|decrease|drop|lower|"
    r"fewer|less|down)\b", re.IGNORECASE)
_UP_RE = re.compile(
    r"^\s*>=?|\b(?:at\s+least|over|above|min(?:imum)?|increase|grow|more|up|raise)\b",
    re.IGNORECASE)


def parse_target(target_text: str) -> float | None:
    m = _NUM_RE.search(target_text or "")
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def parse_direction(target_text: str) -> str:
    """'up' | 'down' | '' (ambiguous). Down cues win when both appear at the
    start (a leading '<=' is unambiguous whatever words follow)."""
    text = (target_text or "").strip()
    if not text:
        return ""
    if _DOWN_RE.search(text):
        return "down"
    if _UP_RE.search(text):
        return "up"
    return ""


def compute_verdict(readings: list[dict], target_text: str,
                    baseline: float | None, flat_band_pct: int) -> tuple[str, dict]:
    """(verdict, inputs). `readings` must already be current-window only —
    callers filter by window_start so a /redo never mixes two windows."""
    band = max(0, int(flat_band_pct or 0)) / 100.0
    target = parse_target(target_text)
    direction = parse_direction(target_text)
    measured = [r for r in readings if r.get("observed") is not None]
    observed = measured[-1]["observed"] if measured else None
    inputs = {
        "observed": observed,
        "target_text": (target_text or "").strip(),
        "target_numeric": target,
        "direction": direction or "ambiguous",
        "baseline": baseline,
        "flat_band_pct": int(flat_band_pct or 0),
        "readings_count": len(measured),
        "rule": "",
    }
    if observed is None:
        inputs["rule"] = "no successful readings — unmeasured (fail closed)"
        return "unmeasured", inputs

    def against_baseline(dir_: str) -> str:
        # inside ±band of baseline = flat; beyond it, the goal direction decides
        if baseline is None:
            return "flat"
        lo, hi = baseline * (1 - band), baseline * (1 + band)
        if lo <= observed <= hi:
            return "flat"
        improved = observed > hi if dir_ != "down" else observed < lo
        return "moved" if improved else "regressed"

    if target is not None:
        if direction == "down":
            if observed <= target:
                inputs["rule"] = f"decrease goal met: observed {observed} <= target {target}"
                return "moved", inputs
            v = against_baseline("down")
            inputs["rule"] = (f"decrease goal missed (observed {observed} > target {target}); "
                              f"judged against baseline within ±{inputs['flat_band_pct']}%")
            return v, inputs
        if observed >= target:
            rule = "increase goal met" if direction == "up" else \
                "target met (direction assumed increase)"
            inputs["rule"] = f"{rule}: observed {observed} >= target {target}"
            return "moved", inputs
        v = against_baseline("up")
        if direction != "up" and v == "regressed":
            # ambiguous direction: never assert a regression the human didn't define
            inputs["rule"] = (f"target missed (observed {observed} < target {target}); "
                              "direction ambiguous — regression not asserted, recorded flat")
            return "flat", inputs
        inputs["rule"] = (f"target missed (observed {observed} < target {target}); "
                          f"judged against baseline within ±{inputs['flat_band_pct']}%")
        return v, inputs

    if baseline is None:
        inputs["rule"] = "no numeric target and no baseline — unmeasured (fail closed)"
        return "unmeasured", inputs
    v = against_baseline(direction or "up")
    dir_note = direction or "assumed increase"
    inputs["rule"] = (f"no numeric target: observed {observed} vs baseline {baseline} "
                      f"within ±{inputs['flat_band_pct']}% ({dir_note})")
    return v, inputs


VERDICT_ICON = {"moved": "📈", "flat": "➖", "regressed": "📉", "unmeasured": "❔"}


def build_gate_packet(job: dict, outcome_fields: dict, readings: list[dict]) -> str:
    """The Iterate-gate analysis markdown for a finished watch."""
    verdict = outcome_fields.get("verdict") or "unmeasured"
    icon = VERDICT_ICON.get(verdict, "")
    try:
        inputs = json.loads(outcome_fields.get("verdict_inputs") or "{}")
    except (ValueError, TypeError):
        inputs = {}
    lines = [
        f"## Outcome verdict: {verdict} {icon}".rstrip(),
        "",
        f"- Metric: {outcome_fields.get('metric') or '(none)'}"
        + (f" (event `{outcome_fields.get('metric_event')}`)"
           if outcome_fields.get("metric_event") else ""),
        f"- Target: {outcome_fields.get('target') or '(none set)'}",
        f"- Observed (window-to-date): {outcome_fields.get('observed')}",
        f"- Baseline (same-length pre-ship window): {outcome_fields.get('baseline')}",
        f"- Window: {outcome_fields.get('window_days')} day(s)",
    ]
    if inputs.get("rule"):
        lines.append(f"- Verdict rule applied: {inputs['rule']}")
    day_rows = [r for r in readings if r.get("window_day") is not None]
    if day_rows:
        lines += ["", "| day | window-to-date | note |", "|---|---|---|"]
        for r in day_rows[-31:]:
            lines.append(f"| {r.get('window_day')} | {r.get('observed')} "
                         f"| {(r.get('detail') or '')[:60]} |")
    lines += [
        "",
        "## Questions",
        "1. Adopt a follow-up (file a new feature), log a learning with "
        "`/proceed <learning>`, extend the watch with `/redo <days>`, or "
        "`/skip` to close.",
    ]
    return "\n".join(lines)


def _date_slug(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts or time.time()))


def build_outcome_entry(outcome: dict, feature: dict,
                        ns: str = ENGINE_DIR) -> tuple[str, str]:
    """(relative path, markdown body) for the changelog entry. `ns` is the
    engine namespace resolved from the ACTUAL clone (legacy `.gumo/` repos
    keep writing into their tree — never the literal constant at call sites)."""
    feature_id = outcome.get("feature_id") or (feature or {}).get("issue_id") or "unknown"
    path = f"{ns}/memory/changelog/{_date_slug()}-outcome-{feature_id}.md"
    verdict = outcome.get("verdict") or "unmeasured"
    lines = [
        f"# Outcome: {(feature or {}).get('title') or feature_id} — {verdict}",
        "",
        f"- Feature: {feature_id}"
        + (f" ({(feature or {}).get('clickup_task_url')})"
           if (feature or {}).get("clickup_task_url") else ""),
        f"- Metric: {outcome.get('metric') or '(none)'}"
        + (f" (event `{outcome.get('metric_event')}`)" if outcome.get("metric_event") else ""),
        f"- Goal: {outcome.get('target') or '(none set)'}",
        f"- Observed: {outcome.get('observed')} over {outcome.get('window_days')} day(s)"
        f" (baseline {outcome.get('baseline')})",
        f"- Verdict: {verdict}",
    ]
    if (feature or {}).get("pr_url"):
        lines.append(f"- PR: {feature['pr_url']}")
    if (outcome.get("learning") or "").strip():
        lines += ["", "## Learning", "", outcome["learning"].strip()]
    if (outcome.get("decided_by") or "").strip():
        lines.append("")
        lines.append(f"_Recorded by {outcome['decided_by']}._")
    return path, "\n".join(lines) + "\n"


def build_outcome_adr(outcome: dict, feature: dict,
                      ns: str = ENGINE_DIR) -> tuple[str, str]:
    """Companion ADR for a non-empty learning — the decision the outcome
    taught, filed where the next lap's P0–P3 context assembly reads."""
    feature_id = outcome.get("feature_id") or (feature or {}).get("issue_id") or "unknown"
    path = f"{ns}/memory/decisions/{_date_slug()}-outcome-{feature_id}.md"
    lines = [
        f"# Learning from shipping: {(feature or {}).get('title') or feature_id}",
        "",
        f"- Context: metric '{outcome.get('metric') or '(none)'}' → verdict "
        f"'{outcome.get('verdict') or 'unmeasured'}' (observed {outcome.get('observed')}, "
        f"goal {outcome.get('target') or 'none'}, {outcome.get('window_days')}-day window)",
        f"- Decided by: {outcome.get('decided_by') or 'unrecorded'}",
        "",
        "## Decision / learning",
        "",
        (outcome.get("learning") or "").strip(),
    ]
    return path, "\n".join(lines) + "\n"
