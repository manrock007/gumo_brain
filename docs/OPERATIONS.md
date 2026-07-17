# CtrlLoop operations — install, configure, back up, upgrade

The operator's guide for running a CtrlLoop instance. The product spec lives
in [ENGINE.md](ENGINE.md); this document is only about keeping an instance
healthy. Deployment wiring for the original Gumo instance (compose file,
ECR, nginx) lives in the `gumoiac` repo; the repo-rename checklist is
[MIGRATION-CTRLLOOP.md](../MIGRATION-CTRLLOOP.md).

## 1. What a running instance is

One container (FastAPI + a serial worker), one SQLite database, one data
volume. Everything durable lives under **`DATA_DIR`** (default `/data`):

| Path | What it holds | Loss impact |
| --- | --- | --- |
| `brain.db` | jobs, stages, users, sessions, workspaces, config overrides | total — this IS the instance |
| `workspaces/` | git clones the runs work in | none — re-cloned on demand |
| `claude-config/`, `claude-config-chat/` | CLI session stores (resume state) when `SESSION_PERSISTENCE=true` | parked gate answers degrade to fresh runs |
| `transcripts/` | replayable run activity (ENGINE.md §13) | activity history for finished runs |

The container needs outbound HTTPS to GitHub, Anthropic, and (if enabled)
Sentry, ClickUp, and Slack webhooks. Inbound: the dashboard/API port and the
`/webhooks/sentry` endpoint.

## 2. Install (fresh instance)

1. Run the image with a persistent volume mounted at `DATA_DIR` and these
   minimum env vars:

   ```
   DATA_DIR=/data
   CTRLLOOP_ADMIN_USER=admin          # optional, default "admin"
   CTRLLOOP_ADMIN_PASSWORD=<strong>   # first-boot admin bootstrap
   GITHUB_TOKEN=<fine-grained PAT>    # clone/push/PR for your repos
   CLAUDE_CODE_OAUTH_TOKEN=<token>    # or ANTHROPIC_API_KEY — the runs' auth
   PUBLIC_BASE_URL=https://your.host  # deep links in nudges/tickets
   SESSION_COOKIE_SECURE=true         # behind any TLS-terminating proxy
   ```

2. Sign in as the admin. The dashboard greets you with the **first-run
   checklist** (ENGINE.md §14): set your business context, point a workspace
   at your repositories, bootstrap product memory, invite your team. Every
   step auto-detects; dismiss the card when you're done.

3. Optional integrations, per workspace once configured globally:
   - **Sentry intake**: `SENTRY_CLIENT_SECRET`, `SENTRY_AUTH_TOKEN`,
     `SENTRY_ORG`, `SENTRY_API_BASE` + point an internal-integration webhook
     at `/webhooks/sentry`.
   - **ClickUp mirroring**: `CLICKUP_TOKEN`, then per-workspace list id +
     enable in Settings → Workspaces.
   - **Slack gate nudges**: per-workspace incoming-webhook URL in Settings.
   - **Fast-lane chat**: `CHAT_FAST_MODEL` (e.g. `claude-sonnet-5`) +
     `CHAT_API_KEY` (falls back to `ANTHROPIC_API_KEY`).
   - **Session persistence** (resumable gates/steering across restarts):
     `SESSION_PERSISTENCE=true`.

Legacy note: an instance upgraded from the single-password era keeps working
— if only `DASHBOARD_PASSWORD` is set, first boot creates admin user `gumo`
with that password.

## 3. Back up

Everything worth backing up is `DATA_DIR`; `brain.db` is the only
irreplaceable file.

- **Simple** (small teams): stop the container, copy `DATA_DIR`, start it.
  Seconds of downtime, always consistent.
- **Online**: `sqlite3 /data/brain.db ".backup /backups/brain-$(date +%F).db"`
  takes a consistent snapshot while the app runs. Copy `transcripts/` and
  the `claude-config*` dirs alongside if you want history/resume state.
- Do NOT copy `brain.db` with plain `cp` while the app is writing — use
  `.backup`.

Restore = stop container, put the files back under `DATA_DIR`, start. Users,
sessions, workspaces, jobs, and config overrides all live in the DB and come
back with it. (Cookie sessions survive restore; if the backup is old, users
just sign in again.)

## 4. Upgrade

1. Back up (`.backup` as above — upgrades run schema migrations).
2. Deploy the new image against the same `DATA_DIR`.
3. Start it and watch the log: migrations are additive column adds plus
   idempotent bootstraps (`bootstrapped admin`, `created default workspace`,
   …). A healthy boot ends with the worker started; `GET /health` returns
   `{"status": "ok"}`.

Downgrades are not supported (schema is forward-only); restoring the
pre-upgrade backup is the rollback path.

## 5. Routine care

- **Disk**: clones under `workspaces/` are the growth driver; they are safe
  to delete when no run is active. Transcripts self-prune after
  `TRANSCRIPT_TTL_DAYS` (30), CLI sessions after `SESSION_TTL_DAYS` (14).
- **Locked account**: 5 consecutive bad passwords lock sign-in for 5
  minutes (`AUTH_LOCKOUT_ATTEMPTS` / `AUTH_LOCKOUT_SECONDS`). An admin
  password reset clears the lockout immediately.
- **Lost admin password**: set `CTRLLOOP_ADMIN_PASSWORD` is only read when
  the users table is EMPTY — for a lost admin credential, reset it in the
  DB: delete the admin's row (`DELETE FROM users WHERE username='...'`) with
  the app stopped and at least one other admin existing, or clear the whole
  users table to re-trigger the first-boot bootstrap.
- **Run guardrails**: `MAX_RUNS_PER_DAY` (8), `ISSUE_COOLDOWN_HOURS` (72),
  `CLAUDE_TIMEOUT_SECONDS` (2400) — the daily-cap and cooldown protections
  described in ENGINE.md §7.
