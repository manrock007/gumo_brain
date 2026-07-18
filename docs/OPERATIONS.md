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

## 2. Install (fresh instance) — the from-zero walkthrough

Every code default is neutral: a fresh instance knows nothing about any
product until you tell it. **No Gumo value — or any other customer value — is
involved at any point** (the example appendix below is documentation only).

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

   First boot creates one admin user and one empty workspace named
   "Default" (no repos, no product name — the instance context applies until
   you set one).

2. Sign in as the admin. The dashboard greets you with the **first-run
   checklist** (ENGINE.md §14) with every step unticked:
   - **Business context** — Project context panel (or `PUT /api/context`):
     set your product name and paste your business context (the textarea
     ships a fill-in template).
   - **Repositories** — Settings → Workspaces (or
     `PATCH /api/workspaces/{id}` with
     `{"repos": [{"slug": "api", "repo": "you/api", "base": "main"}], "canonical_project": "api"}`):
     add your first repo; the slug immediately appears in the intake pickers
     (`GET /api/projects`).
   - **Memory** — Product brain panel: bootstrap each repo's engine memory
     (a draft PR you review).
   - **Team** — Settings → Users: add members, assign them to workspaces.

3. Submit your first feature (project + title on the dashboard, or
   `POST /api/features {"project": "api", "title": "…"}`) — it queues at P0
   even with no ClickUp configured (ClickUp is best-effort visibility,
   never required). Dismiss the checklist card when you're done.

   This whole path is pinned by `tests/test_install_from_zero.py`.

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

## 6. Configuration reference (selected env vars)

Every code default is **neutral** — nothing customer- or vendor-specific is
baked in. Key knobs (see `app/config.py` for the full set; env var = the
upper-cased field name):

| Env var | Default | Meaning |
| --- | --- | --- |
| `REPO_MAP` | `{}` | project slug → `{repo, base, setup_cmd?, test_cmd?, allow?}`; normally managed per workspace via the API, env seeds first boot |
| `PRODUCT_NAME` | `your product` | prompt identity ("the `<name>` Engine") |
| `BUSINESS_CONTEXT` | *(empty)* | free text injected into every run prompt; template in the dashboard's context panel |
| `MEMORY_CANONICAL_PROJECT` | *(empty)* | slug hosting product-scope memory; empty = repo-scope only |
| `SENTRY_ORG`, `SENTRY_AUTH_TOKEN` | *(empty)* | BOTH required to enable the Sentry lane |
| `SENTRY_API_BASE` | `https://sentry.io/api/0` | EU-region orgs set `https://de.sentry.io/api/0` |
| `CLICKUP_TOKEN`, `CLICKUP_LIST_ID` | *(empty)* | BOTH required to enable ClickUp (list may also be per-workspace) |
| `CLICKUP_STAGE_FIELD_MAP` | `{}` | stage → `Stage` dropdown option (`"build"` resolves per-repo) |
| `CLICKUP_REPO_STAGE_MAP` | `{}` | repo → its build-stage column |
| `CLICKUP_PR_FIELD_MAP` | `{}` | repo → its PR url field |
| `CLICKUP_DOC_FIELD_MAP` | `{}` | artifact filename → doc url field |
| `CLICKUP_FOLDER_FIELD` | *(empty)* | url field pointing at the branch's engine tree |
| `CLICKUP_FRICTION_FIELD` | *(empty)* | append-only friction/improvements log field |
| `CLICKUP_FLAG_FIELD`, `CLICKUP_METRIC_FIELD` | *(empty)* | P9 `FLAG_NAME:` / `SUCCESS_METRIC:` launch fields |
| `PUBLIC_BASE_URL` | *(empty)* | dashboard base for deep links; empty = links omitted |
| `CTRLLOOP_GIT_NAME`, `CTRLLOOP_GIT_EMAIL` | `ctrlloop` / `engine@ctrlloop.local` | git author for engine commits (entrypoint.sh) |

Empty maps/fields make the whole conveyor mirror inert — the engine never
touches a workspace's custom fields unless told which ones. Three field names
stay **literal by design** and are addressed by name with a quiet no-op when
the workspace lacks them: `Stage` (inert anyway until
`CLICKUP_STAGE_FIELD_MAP` is populated), `Dashboard` (inert until
`PUBLIC_BASE_URL` is set), and `Decisions` (substantive gate answers append
there). They are engine-generic mirror fields, not customer branding.

## Appendix — Example configuration: the original Gumo instance

The values that used to ship as code defaults, now purely this one customer's
config. Use them as a worked example:

```
SENTRY_ORG=gumo
SENTRY_API_BASE=https://de.sentry.io/api/0     # EU-region org
PUBLIC_BASE_URL=https://gumo.co.in/brain
PRODUCT_NAME=Gumo
MEMORY_CANONICAL_PROJECT=gumo
CLICKUP_LIST_ID=901615853762                   # "Sentry Autofix" list
CLICKUP_STAGE_FIELD_MAP={"0": "Brief", "1": "PRD", "2": "PRD", "3": "Contract", "4": "Grounding", "5": "build", "6": "build", "7": "Integration", "8": "Tech Review", "9": "Launch", "shipped": "Dogfood", "merged": "Complete"}
CLICKUP_REPO_STAGE_MAP={"manrock007/gumoserver": "Backend", "manrock007/gumowebclient": "Frontend - Web", "manrock007/gumoclient": "Frontend - App"}
CLICKUP_PR_FIELD_MAP={"manrock007/gumoserver": "Backend PR", "manrock007/gumowebclient": "Web PR", "manrock007/gumoclient": "App PR"}
CLICKUP_DOC_FIELD_MAP={"P1-prd.md": "PRD Doc", "P3-design.md": "Contract Doc"}
CLICKUP_FOLDER_FIELD=PRD Folder
CLICKUP_FRICTION_FIELD=Gumo Workflow Improvements
CLICKUP_FLAG_FIELD=Flag name
CLICKUP_METRIC_FIELD=Success metric
REPO_MAP={"gumo": {"repo": "manrock007/gumoserver", "base": "master"}, "web": {"repo": "manrock007/gumowebclient", "base": "dev", "setup_cmd": "npm ci", "test_cmd": "npm test", "allow": ["Bash(npm:*)", "Bash(npx:*)", "Bash(node:*)"]}, "react-native": {"repo": "manrock007/gumoclient", "base": "master", "setup_cmd": "cd codebase/Gumo && npm ci", "test_cmd": "cd codebase/Gumo && npx jest --ci", "allow": ["Bash(npm:*)", "Bash(npx:*)", "Bash(node:*)", "Bash(cd:*)"]}, "gumo-video-analyser": {"repo": "manrock007/gumo_video_analyser", "base": "main"}}
```

Business-context example (dashboard context panel / `BUSINESS_CONTEXT`):

```
Gumo is one product built across three main repositories:
- `gumo` (manrock007/gumoserver) — the Django backend: API, data models,
  business logic. This is the canonical repo hosting product-scope memory.
- `web` (manrock007/gumowebclient) — the web client.
- `react-native` (manrock007/gumoclient) — the React Native mobile app.
The clients consume the backend's API; cross-repo features ship server-first,
then clients. Deeper, versioned product knowledge lives in product memory.
```
