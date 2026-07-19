"""RBAC v2 (Epic E3) — scoped role checks.

Roles split across two axes (matching the existing model):
  * INSTANCE role on users.role: instance_admin | member  (viewer is a
    workspace role; a bare member without workspace-admin can still be a viewer
    in a given workspace).
  * WORKSPACE role on workspace_members.role: admin | member | viewer, plus a
    per-member `repos` JSON allow-list ([] = all repos in the workspace).

Instance admins are admins everywhere. Unknown roles fail closed to viewer
(least privilege). None of this replaces the role-EXCLUSIVE DRI gate
enforcement in roles.py — that layers ON TOP.
"""

import json

from fastapi import Depends, HTTPException, Request

from .auth import require_user

WRITE_ROLES = ("admin", "member")     # workspace roles that may mutate
READ_ONLY = ("viewer",)


def instance_role(user: dict) -> str:
    role = user.get("role") or "member"
    return "instance_admin" if role in ("admin", "instance_admin") else role


def is_instance_admin(user: dict) -> bool:
    return instance_role(user) == "instance_admin"


def workspace_role(store, user: dict, workspace_id) -> str | None:
    """The user's role in a workspace: instance_admin -> 'admin' everywhere;
    else the workspace_members.role, or None when not a member."""
    if is_instance_admin(user):
        return "admin"
    if workspace_id is None:
        return None
    row = store.workspace_member_row(workspace_id, user["id"])
    if row is None:
        return None
    role = row.get("role") or "member"
    return role if role in ("admin", "member", "viewer") else "viewer"


def is_read_only(role: str | None) -> bool:
    return role in READ_ONLY


def can_configure_instance(user: dict) -> bool:
    return is_instance_admin(user)


def can_configure_workspace(store, user: dict, workspace_id) -> bool:
    return workspace_role(store, user, workspace_id) == "admin"


def repo_allowed(store, user: dict, workspace_id, slug: str) -> bool:
    """A per-member repo restriction. Empty list = all repos. Instance/workspace
    admins are unrestricted."""
    role = workspace_role(store, user, workspace_id)
    if role == "admin":
        return True
    if role is None:
        return False
    row = store.workspace_member_row(workspace_id, user["id"])
    if row is None:
        return False
    try:
        repos = json.loads(row.get("repos") or "[]")
    except (ValueError, TypeError):
        repos = []
    return not repos or slug in repos


def can_submit(store, user: dict, workspace_id, project_slug: str) -> bool:
    """Member+ (not viewer) AND the repo is allowed for this member."""
    role = workspace_role(store, user, workspace_id)
    if role not in WRITE_ROLES:
        return False
    return repo_allowed(store, user, workspace_id, project_slug)


def can_answer_gate(store, user: dict, workspace_id, project_slug: str) -> bool:
    return can_submit(store, user, workspace_id, project_slug)


# ---- FastAPI dependencies ----

def require_instance_admin(request: Request, user: dict = Depends(require_user)) -> dict:
    if not is_instance_admin(user):
        raise HTTPException(status_code=403, detail="instance admin role required")
    return user


def require_write(request: Request, user: dict = Depends(require_user)) -> dict:
    """403 a viewer on any mutating route. A user with no workspace membership
    but instance_admin passes; a plain member passes (per-workspace/per-repo
    checks happen at the endpoint via can_submit)."""
    if is_instance_admin(user):
        return user
    # a user who is a viewer in ALL their workspaces (and admin in none) is
    # read-only; otherwise they may write somewhere and per-endpoint checks
    # apply. Fail closed only when the user has no write role anywhere.
    store = request.app.state.store
    for wid in store.workspace_ids_for_user(user["id"]):
        if workspace_role(store, user, wid) in WRITE_ROLES:
            return user
    raise HTTPException(status_code=403, detail="read-only (viewer) — write access required")
