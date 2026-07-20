#!/usr/bin/env python3
"""Break-glass user administration for a CtrLoop instance.

Runs INSIDE the app container (or any environment with the app importable and
the same env as the server), so it resolves the database exactly like the
running app — `DATABASE_URL` if set, otherwise the SQLite file under
`DATA_DIR`. That is the whole point: a raw `sqlite3 …` one-liner can silently
write the wrong file (wrong DATA_DIR, or a Postgres deployment where the SQLite
file does not even exist) and appear to "do nothing". Going through the app's
own JobStore + password hasher can't diverge from what the server reads.

Typical use — an operator is locked out and needs back in:

    # 1. See who exists (never prints password hashes)
    python -m scripts.admin_user list

    # 2. Reset a password + clear any lockout / forced-change flags
    python -m scripts.admin_user reset-password --user gumo

    # 3. Or mint a fresh instance admin if the table somehow has none
    python -m scripts.admin_user create-admin --user founder

Passwords are read from a prompt (or --password-stdin / CTRLLOOP_NEW_PASSWORD)
so the secret never lands in shell history or `ps` output.

Deployed example (Docker):

    docker exec -it gumo-brain python -m scripts.admin_user list
    docker exec -it gumo-brain python -m scripts.admin_user reset-password --user gumo
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

# Allow both `python -m scripts.admin_user` and `python scripts/admin_user.py`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.auth import hash_password  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import JobStore  # noqa: E402


def _open_store() -> tuple[JobStore, str]:
    settings = get_settings()
    dsn = settings.database_url or settings.db_path
    store = JobStore(dsn)
    where = f"postgres ({dsn.split('@')[-1]})" if settings.db_backend == "postgres" \
        else f"sqlite ({dsn})"
    return store, where


def _read_new_password(args) -> str:
    """Prefer an explicit non-interactive source; fall back to a confirmed
    prompt. Never echoes, never accepts an empty password."""
    if args.password_stdin:
        # Strip only the line terminator (handles \n, \r\n and a lone \r from
        # CRLF/Windows-piped input). NOT a bare rstrip(): that would also eat an
        # intentional trailing space in the password, causing the same silent
        # lockout it's meant to prevent.
        pw = sys.stdin.readline().rstrip("\r\n")
    elif os.environ.get("CTRLLOOP_NEW_PASSWORD"):
        pw = os.environ["CTRLLOOP_NEW_PASSWORD"]
    else:
        pw = getpass.getpass("New password: ")
        if pw != getpass.getpass("Confirm password: "):
            sys.exit("passwords did not match")
    if not pw or not pw.strip():
        sys.exit("refusing to set an empty password")
    return pw


def cmd_list(args) -> int:
    store, where = _open_store()
    users = store.user_list()
    print(f"database: {where}")
    if not users:
        print("(no users — the instance will bootstrap an admin from "
              "CTRLLOOP_ADMIN_PASSWORD / DASHBOARD_PASSWORD on next start)")
        return 0
    print(f"{'username':<24} {'role':<16} {'disabled':<9} must_change_pw")
    for u in users:
        print(f"{u['username']:<24} {u.get('role', ''):<16} "
              f"{str(bool(u.get('disabled'))):<9} {bool(u.get('must_change_pw'))}")
    return 0


def cmd_reset_password(args) -> int:
    store, where = _open_store()
    user = store.user_get(args.user)
    if user is None:
        print(f"database: {where}", file=sys.stderr)
        sys.exit(f"no such user: {args.user!r} — run `list` to see exact usernames")
    pw = _read_new_password(args)
    # Clear everything that could still block a login after the hash is set:
    # a live lockout window, the forced-change flag, and the disabled flag.
    store.user_set(args.user, pw_hash=hash_password(pw), failed_attempts=0,
                   locked_until=0, must_change_pw=0, disabled=0)
    print(f"password reset for {args.user!r} on {where}; lockout and "
          "must-change cleared. You can log in now.")
    return 0


def cmd_create_admin(args) -> int:
    store, where = _open_store()
    if store.user_get(args.user) is not None:
        sys.exit(f"user {args.user!r} already exists — use `reset-password` instead")
    pw = _read_new_password(args)
    store.user_create(args.user, hash_password(pw), role="instance_admin",
                      must_change_pw=False)
    print(f"created instance_admin {args.user!r} on {where}. You can log in now.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="admin_user", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="list users (never prints password hashes)")
    p_list.set_defaults(func=cmd_list)

    for name, func, help_ in (
        ("reset-password", cmd_reset_password, "reset a user's password + clear lockout"),
        ("create-admin", cmd_create_admin, "create a new instance_admin"),
    ):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--user", required=True, help="username")
        p.add_argument("--password-stdin", action="store_true",
                       help="read the new password from stdin instead of prompting")
        p.set_defaults(func=func)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
