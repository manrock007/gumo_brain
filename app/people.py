"""People & ownership model (Epic D1): a profile layer OVER `users`.

Never a parallel identity — profiles feed (a) intake-time DRI defaults and
(b) an ownership block in stage prompts. Gate ENFORCEMENT is untouched: it
keys exclusively on the explicit founder_dri/dev_dri job columns (roles.py,
ENGINE.md §2); profiles only FILL those columns at intake when the submitter
left them empty (and PEOPLE_ROUTING_DEFAULTS is on).

Posture mirrors roles.py: fail-closed write-side validation
(validate_profile raises ValueError → 400, nothing changes), read-tolerant
resolvers (hand-edited rows degrade to "no coverage", never crash a prompt).
"""

import json
import logging

log = logging.getLogger("brain.people")

PERSON_ROLES = ("", "founder", "product", "dev", "design")
AREA_KINDS = ("workspace", "repo", "area")

MAX_AREAS = 32
MAX_AREA_VALUE = 64
MAX_AUTHORITY = 16          # entries (amendment: both caps explicit)
MAX_AUTHORITY_LEN = 48      # chars per entry, single-lined
MAX_NOTES = 2000

OWNERSHIP_BLOCK_CAP = 1200


def _line(value, cap: int = 120) -> str:
    """One bounded line — profile values render inside engine-voiced markdown
    and must never break out of their bullet (same rule as worker._single_line)."""
    return " ".join(str(value or "").split())[:cap].strip()


def validate_profile(fields: dict) -> dict:
    """Validate + normalize a profile write. Raises ValueError (→ 400, nothing
    changes); returns {person_role, areas (list), authority (list), notes}."""
    cleaned: dict = {}
    if "person_role" in fields:
        role = str(fields["person_role"] or "").strip().lower()
        if role not in PERSON_ROLES:
            raise ValueError("person_role must be one of: "
                             + ", ".join(f"'{r}'" for r in PERSON_ROLES if r)
                             + " (or empty)")
        cleaned["person_role"] = role
    if "areas" in fields:
        areas = fields["areas"]
        if isinstance(areas, str):
            try:
                areas = json.loads(areas) if areas.strip() else []
            except (ValueError, TypeError):
                raise ValueError("areas must be a JSON list of {kind, value}")
        if not isinstance(areas, list):
            raise ValueError("areas must be a list of {kind, value}")
        if len(areas) > MAX_AREAS:
            raise ValueError(f"areas is capped at {MAX_AREAS} entries")
        out = []
        for a in areas:
            if not isinstance(a, dict):
                raise ValueError("each area must be an object {kind, value}")
            kind = str(a.get("kind") or "").strip().lower()
            if kind not in AREA_KINDS:
                raise ValueError("area kind must be one of: "
                                 + ", ".join(f"'{k}'" for k in AREA_KINDS))
            value = _line(a.get("value"), MAX_AREA_VALUE)
            if not value:
                raise ValueError("area value must be a non-empty string")
            out.append({"kind": kind, "value": value})
        cleaned["areas"] = out
    if "authority" in fields:
        authority = fields["authority"]
        if isinstance(authority, str):
            try:
                authority = json.loads(authority) if authority.strip() else []
            except (ValueError, TypeError):
                raise ValueError("authority must be a JSON list of short strings")
        if not isinstance(authority, list):
            raise ValueError("authority must be a list of short strings")
        if len(authority) > MAX_AUTHORITY:
            raise ValueError(f"authority is capped at {MAX_AUTHORITY} tags")
        tags = []
        for t in authority:
            tag = _line(t, MAX_AUTHORITY_LEN)
            if not tag:
                raise ValueError("authority tags must be non-empty strings "
                                 f"(max {MAX_AUTHORITY_LEN} chars)")
            tags.append(tag)
        cleaned["authority"] = tags
    if "notes" in fields:
        notes = str(fields["notes"] or "").strip()
        if len(notes) > MAX_NOTES:
            raise ValueError(f"notes are capped at {MAX_NOTES} chars")
        cleaned["notes"] = notes
    return cleaned


def _areas_of(profile: dict) -> list[dict]:
    """Read-tolerant areas decode — a hand-edited row degrades to []."""
    raw = profile.get("areas") or "[]"
    try:
        areas = json.loads(raw) if isinstance(raw, str) else raw
        return [a for a in areas if isinstance(a, dict)] if isinstance(areas, list) else []
    except (ValueError, TypeError):
        return []


def _authority_of(profile: dict) -> list[str]:
    raw = profile.get("authority") or "[]"
    try:
        tags = json.loads(raw) if isinstance(raw, str) else raw
        return [str(t) for t in tags] if isinstance(tags, list) else []
    except (ValueError, TypeError):
        return []


def _covers(profile: dict, ws: dict | None, project: str) -> bool:
    """Does this profile's `areas` claim this job's scope? Matches workspace
    slug or repo (project) slug. An EMPTY areas list covers NOTHING — a
    profile must claim scope to route (fail closed). 'area' kind entries are
    free-text product-area tags: display only in v1, never routing."""
    ws_slug = (ws or {}).get("slug") or ""
    project = (project or "").strip()
    for a in _areas_of(profile):
        kind = str(a.get("kind") or "")
        value = str(a.get("value") or "")
        if kind == "workspace" and ws_slug and value == ws_slug:
            return True
        if kind == "repo" and project and value == project:
            return True
    return False


def default_dris(store, ws: dict | None, project: str) -> tuple[str, str]:
    """The A3 routing default at intake: (founder_dri, dev_dri) usernames.

    A slot fills iff EXACTLY ONE enabled, workspace-member user's profile
    covers the job with the matching person_role — ambiguity or zero leaves
    '' (fail closed: the gate stays inert exactly as today). product/design
    roles map to NEITHER slot in v1 (they appear in the prompt block only).

    Membership is required (workspace_members): a profile may claim a
    workspace its user cannot access — appointing a non-member would wedge
    the gate behind 404s. No resolvable workspace → no fill at all."""
    if ws is None:
        return "", ""
    try:
        rows = store.people_all()
    except Exception:
        log.exception("people lookup failed — no DRI defaults")
        return "", ""
    out: dict[str, str] = {"founder": "", "dev": ""}
    for slot in ("founder", "dev"):
        candidates = []
        for p in rows:
            if p.get("disabled"):
                continue
            if (p.get("person_role") or "") != slot:
                continue
            if not _covers(p, ws, project):
                continue
            if ws["id"] not in store.workspace_ids_for_user(p["id"]):
                continue  # non-members can never be routed a gate they can't see
            candidates.append(p["username"])
        if len(candidates) == 1:
            out[slot] = candidates[0]
    return out["founder"], out["dev"]


def ownership_block(store, ws: dict | None, project: str,
                    stage_roles: dict, job: dict) -> str:
    """The stage-prompt "Ownership & decision authority" block.

    Two parts: (1) the coverage list — every profile covering this job, with
    role + authority tags (display only); (2) per-gate-role authority lines
    rendered from the JOB's OWN DRI columns (explicit-submission-wins — the
    profiles never contradict the job's actual gate owner). Empty string when
    nothing covers the job and the job has no DRIs (no noise on solo installs)."""
    lines: list[str] = []
    try:
        rows = store.people_all()
    except Exception:
        rows = []
    for p in rows:
        if p.get("disabled") or not _covers(p, ws, project):
            continue
        role = _line(p.get("person_role") or "", 24)
        tags = ", ".join(_line(t, 48) for t in _authority_of(p)[:MAX_AUTHORITY])
        entry = f"- {_line(p.get('username'), 64)}"
        if role:
            entry += f" — {role}"
        if tags:
            entry += f"; decides: {tags}"
        lines.append(entry)

    # gate-authority lines come from the JOB row, never the profiles: the
    # actual owner is roles.gate_owner keyed on founder_dri/dev_dri
    founder = _line(job.get("founder_dri") or "", 64)
    dev = _line(job.get("dev_dri") or "", 64)
    if founder or dev:
        from . import roles  # local: avoid import cycles at module load
        role_stages: dict[str, list[str]] = {}
        for st, role in sorted(stage_roles.items(), key=lambda kv: int(kv[0])):
            role_stages.setdefault(role, []).append(f"P{st}")
        for role in ("founder", "dev"):
            own = founder if role == "founder" else dev
            value = own or (dev if role == "founder" else founder)
            if not value or role not in role_stages:
                continue
            display = roles.dri_display(store, value)
            stages = ", ".join(role_stages[role])
            if own:
                lines.append(f"- {role} decisions here ({stages} gates) belong to "
                             f"{_line(display, 64)}")
            else:
                # no DRI for this role — the gates fall to the other DRI as the
                # fallback owner (matches roles.gate_owner); say so explicitly
                # rather than implying this person holds the {role} role
                lines.append(f"- {role} decisions here ({stages} gates): no {role} "
                             f"DRI set — they fall to {_line(display, 64)}")
    if not lines:
        return ""
    block = ("## Ownership & decision authority\n\n"
             + "\n".join(lines))
    return block[:OWNERSHIP_BLOCK_CAP]
