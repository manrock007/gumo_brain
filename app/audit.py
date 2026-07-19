"""Append-only audit log (Epic E4) — the single sink + typed action constants.

Every authority-moving or decision mutation records exactly one audit_log row
through ``record``. The canonical table SUPERSEDES admin_events (db.admin_event_add
repoints here; a one-time boot-copy migrates legacy rows). autonomy_events stays
as the C-surface store, mirrored here for the unified export.

Detail redaction is centralized in ``db.audit_add`` (allow-listed field copy),
so no call site can accidentally log a secret; the constants below name the
verbs and the actor helpers keep the actor string consistent.
"""

# ---- action constants (dotted verbs) ----
GATE_DECISION = "gate.decision"      # proceed / redo / skip (channel = dashboard|clickup)
GATE_OVERRIDE = "gate.override"      # audited admin bypass of role-exclusive gate
STEER = "session.steer"
CHAT = "session.chat"
CONFIG_INSTANCE = "config.instance"
CONFIG_WORKSPACE = "config.workspace"
USER_CREATE = "user.create"
USER_UPDATE = "user.update"
USER_DISABLE = "user.disable"
USER_ROLE = "user.role"
TOKEN_CREATE = "token.create"
TOKEN_REVOKE = "token.revoke"
LOGIN = "auth.login"                 # password | oidc (detail.method)
CLICKUP_LINK = "user.clickup_link"
AUTONOMY_PIN = "autonomy.pin"
AUTONOMY_CLAWBACK = "autonomy.clawback"
AUTONOMY_LEVEL = "autonomy.level"
BUDGET_BLOCK = "budget.block"
BUDGET_OVERRIDE = "budget.override"

# admin_events legacy kinds -> unified actions (for admin_event_add shim + boot-copy).
LEGACY_KIND_MAP = {
    "clickup_link": CLICKUP_LINK,
    "workspace_config": CONFIG_WORKSPACE,
    "workspace_create": CONFIG_WORKSPACE,
    "people_profile": USER_UPDATE,
}


def dashboard_actor(user: dict) -> str:
    return f"dashboard:{user.get('username', '?')}"


def token_actor(user: dict) -> str:
    return f"token:{user.get('username', '?')}"


def record(store, action: str, *, actor: str = "engine", actor_kind: str = "system",
           scope: str = "instance", workspace_id=None, job_id: str = "",
           target: str = "", channel: str = "", detail: dict | None = None):
    """The single audit sink. Thin wrapper over db.audit_add — kept so call
    sites import one name and never touch the table directly."""
    store.audit_add(action, actor=actor, actor_kind=actor_kind, scope=scope,
                    workspace_id=workspace_id, job_id=job_id, target=target,
                    channel=channel, detail=detail or {})
