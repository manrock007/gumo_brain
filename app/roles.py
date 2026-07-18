"""Gate ownership resolution (Epic A: dual DRIs + role-exclusive gates).

One pure resolver consumed by BOTH channels (worker/_answer_feature and
engine/_park) so enforcement and display can never diverge. Invariant posture:

- Enforcement keys EXCLUSIVELY on the explicit ``founder_dri``/``dev_dri``
  columns. The legacy ``owner`` column may feed assignment/notification/
  display (``enforce=False``) but never makes a gate refusable — an upgraded
  instance whose in-flight jobs only carry ``owner`` behaves exactly as
  before (solo mode preserved).
- Anything ambiguous fails closed on the WRITE side (workspace saves reject a
  malformed stage_role_map); the READ side tolerates hand-edited rows by
  logging and falling through to the defaults.
"""

import json
import logging
from dataclasses import dataclass

from .config import DEFAULT_STAGE_ROLE_MAP, Settings

log = logging.getLogger("brain.roles")


@dataclass
class GateOwner:
    role: str                     # 'founder' | 'dev' — who owns the current stage
    value: str                    # the DRI value as stored (ClickUp id or username)
    clickup_id: str = ""          # numeric ClickUp id when resolvable (assignment)
    user: dict | None = None      # mapped CtrlLoop user row, when one exists
    display: str = ""             # human-readable owner name for comments/UI
    enforce: bool = True          # False = resolved only via legacy `owner` —
                                  # display/assignment only, NEVER refusable


def _role_map(settings: Settings, ws: dict | None) -> dict:
    """Effective stage→role map: workspace override > instance override >
    built-in default. Read-tolerant: a malformed STORED map logs and falls
    through (the write side rejects bad maps, so this only guards hand edits)."""
    for source, raw in (("workspace", (ws or {}).get("stage_role_map") or ""),
                        ("instance", settings.stage_role_map or "")):
        raw = raw.strip()
        if not raw:
            continue
        try:
            overrides = json.loads(raw)
            if not isinstance(overrides, dict):
                raise ValueError("not an object")
            return {**DEFAULT_STAGE_ROLE_MAP, **{str(k): str(v) for k, v in overrides.items()}}
        except (ValueError, TypeError):
            log.warning("malformed %s stage_role_map ignored: %r", source, raw[:120])
            continue
    return dict(DEFAULT_STAGE_ROLE_MAP)


def role_for_stage(settings: Settings, ws: dict | None, stage: int) -> str:
    role = _role_map(settings, ws).get(str(int(stage)), "")
    return role if role in ("founder", "dev") else DEFAULT_STAGE_ROLE_MAP.get(str(int(stage)), "dev")


def gate_owner(store, settings: Settings, ws: dict | None, job: dict) -> GateOwner | None:
    """Resolve who owns the job's CURRENT gate. None = no DRI of any kind is
    recorded (solo mode — enforcement N/A) or not a feature job. The role's
    own DRI slot wins; the other DRI is the fallback, then the legacy `owner`
    column (which resolves display/assignment only — enforce=False)."""
    if (job.get("kind") or "") != "feature":
        return None
    stage = int(job.get("stage") or 0)
    role = role_for_stage(settings, ws, stage)
    founder = (job.get("founder_dri") or "").strip()
    dev = (job.get("dev_dri") or "").strip()
    legacy = (job.get("owner") or "").strip()
    if role == "founder":
        value = founder or dev
    else:
        value = dev or founder
    enforce = bool(value)
    value = value or legacy
    if not value:
        return None
    user = store.user_for_dri(value)
    clickup_id = value if value.isdigit() else str((user or {}).get("clickup_user_id") or "")
    if user:
        display = user["username"]
    elif value.isdigit():
        display = f"ClickUp user {value}"
    else:
        display = value
    return GateOwner(role=role, value=value, clickup_id=clickup_id, user=user,
                     display=display, enforce=enforce)


def actor_is_owner(owner: GateOwner | None, actor: dict | None) -> bool:
    """Does the acting user own this gate? Matches on username (DRI stored as
    a CtrlLoop username, or the DRI's mapped user) OR on the actor's linked
    ClickUp id — non-empty compares only (empty never matches empty)."""
    if owner is None:
        return True
    if not actor:
        return False
    uname = str(actor.get("username") or "").strip()
    if uname:
        if uname == owner.value:
            return True
        if owner.user and str(owner.user.get("username") or "") == uname:
            return True
    cu = str(actor.get("clickup_user_id") or "").strip()
    if cu and owner.clickup_id and cu == owner.clickup_id:
        return True
    return False


def other_dri(job: dict, owner_role: str) -> str:
    """The NON-owning DRI's value — the Epic A5 step-2 escalation target."""
    if owner_role == "founder":
        return (job.get("dev_dri") or "").strip()
    return (job.get("founder_dri") or "").strip()


def dri_display(store, value: str) -> str:
    """Human-readable name for a raw DRI value (escalation texts)."""
    value = (value or "").strip()
    if not value:
        return ""
    user = store.user_for_dri(value)
    if user:
        return user["username"]
    return f"ClickUp user {value}" if value.isdigit() else value


def attribution_required(settings: Settings, ws: dict | None, store) -> bool:
    """Must ClickUp gate verbs come from a mapped commenter? Workspace value
    wins; the instance setting covers jobs without a workspace row. 'auto'
    (and anything unrecognized) = strict once any enabled user is mapped."""
    mode = str(((ws or {}).get("require_attributed_answers")
                if ws is not None else settings.require_attributed_answers)
               or "auto").strip().lower()
    if mode == "on":
        return True
    if mode == "off":
        return False
    return store.any_clickup_mapping()
