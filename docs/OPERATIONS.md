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
| `BRANCH_PREFIX` | `ctrlloop` | prefix for engine-created branches (`<prefix>/feat-…`, `<prefix>/sentry-…`, `<prefix>/memory-…`, `<prefix>/outcome-…`). Must be ONE git-valid branch segment — starts alphanumeric, chars `A-Za-z0-9._-`, no `/`, no `.lock` suffix (validated at boot; e.g. `team/bot` is rejected). Jobs record their branch at first use, so changing it never strands in-flight work; pre-rename rows keep their historical `brain/…` branches. |
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
`CLICKUP_DRI_FIELD_MAP` (default `{"founder": "Assigned Founder DRI",
"dev": "Assigned Dev DRI"}`) follows the same posture: engine-generic role
field names, read-side lookups against your workspace's own schema, a quiet
no-op when the fields don't exist, `{}` to disable the reads entirely.

## 7. Team coordination (Epic A: dual DRIs, role gates, attribution, SLA)

| Env var | Default | Meaning |
| --- | --- | --- |
| `REQUIRE_ATTRIBUTED_ANSWERS` | `auto` | ClickUp gate verbs must come from a mapped commenter: `on` / `off` / `auto` (= strict once ANY user has a ClickUp link). Instance fallback; each workspace can override. |
| `STAGE_ROLE_MAP` | *(empty)* | JSON overrides of the stage→role ladder (keys `"0"`..`"9"`, values `founder`/`dev`); empty = built-in (P0/P1/P9 founder, P2–P8 dev). Workspace `stage_role_map` overrides it. |
| `GATE_SLA_HOURS` | `24` | gate SLA before escalation; `0` disables. Workspace `gate_sla_hours` overrides (empty string in a PATCH clears back to inherit). |
| `SLA_CHECK_INTERVAL_SECONDS` | `900` | escalation sweep cadence. |
| `CLICKUP_DRI_FIELD_MAP` | `{"founder": "Assigned Founder DRI", "dev": "Assigned Dev DRI"}` | role → the ClickUp people field feature adoption reads that DRI from; `{}` disables. |

How it fits together:

- **Linking people**: Settings → Users → "Link ClickUp id" (or
  `PATCH /api/users/{u} {"clickup_user_id": "<numeric id>"}`). One ClickUp
  identity per user (409 + a DB unique index). Once anyone is linked, the
  default `auto` strictness starts refusing gate verbs from unmapped
  ClickUp commenters (one explanatory reply per comment).
- **DRIs**: set per feature at submit (dashboard fields / API
  `founder_dri`+`dev_dri`; the old `owner` field is a deprecated alias for
  `dev_dri`) or via the ClickUp people fields at `[feature]` adoption.
  A job with NO explicit DRIs stays in solo mode — no role enforcement, no
  SLA escalation. **Re-submitting a feature replaces its DRIs from the fresh
  submission** — but note the Epic D1 interplay: with
  `PEOPLE_ROUTING_DEFAULTS=true` (the default) and people profiles covering
  the repo, an EMPTY slot re-fills from the profiles at intake, so a
  resubmit that omits both DRIs re-enables enforcement with the profile
  defaults. There is no per-job "no DRI" once profiles cover a repo —
  `PEOPLE_ROUTING_DEFAULTS=false` is the opt-out (profiles then feed
  prompts/display only). See §10.
- **Fail-closed corner**: `REQUIRE_ATTRIBUTED_ANSWERS=off` with DRIs set
  still refuses unmapped ClickUp commenters on role-owned gates (an
  unresolved commenter can never be the owner) — if a DRI'd feature's gate
  channel looks dead from ClickUp, link the ClickUp ids or answer on the
  dashboard. Admin override (audited) exists only on the dashboard.
- **Upgrades**: pre-existing jobs carry only the legacy `owner` column —
  they keep today's behavior verbatim (assignment yes, enforcement and
  escalation no). The first SLA sweep after a deploy escalates only gates
  that are ALREADY over the SLA *and* carry explicit DRIs — a freshly
  upgraded instance (no DRI columns populated yet) sends nothing.

## 8. The outcome loop (Epic B: metric at intake, watcher, ledger)

| Env var | Default | Meaning |
| --- | --- | --- |
| `METRIC_WINDOW_DAYS_DEFAULT` | `14` | measurement window (days) for features submitted without one; the API/intake accept 1–365. |
| `WATCH_ENABLED` | `true` | master switch: spawn watch jobs on merge + run the watch loop. |
| `WATCH_INTERVAL_SECONDS` | `3600` | watch loop cadence; metric reads are internally throttled to ~one per day per job. |
| `OUTCOME_FLAT_BAND_PCT` | `10` | ± band around the baseline inside which a verdict is `flat`. |
| `OUTCOME_MEMORY_PRS` | `true` | on `/proceed` at the Iterate gate, write the verdict into product memory via a mechanical draft PR (no model run). |
| `ANALYTICS_PROVIDER` | *(empty)* | instance-level analytics fallback: empty = none (verdicts stay `unmeasured`) or `mixpanel`. Per-workspace settings win. |
| `ANALYTICS_CONFIG` | `{}` | JSON object for the instance provider: `{project_id, service_account, secret, api_base}`. EU Mixpanel projects set `"api_base": "https://eu.mixpanel.com/api"` — never a code default. |

Per-workspace analytics lives in Settings → Workspaces
(`PATCH /api/workspaces/{id}` with `analytics_provider` +
`analytics_config`). The config — including the Mixpanel service-account
secret — is stored in the DB row and **redacted from every API response**;
the dashboard only ever sees `analytics_configured: true/false`. A malformed
config or unknown provider degrades to the null driver (outcomes render
`unmeasured`), and read failures (e.g. a revoked credential's 401) appear as
detail text on the watch job.

Operational notes:

- Watch jobs (`watch-<feature id>`) never invoke Claude — the loop is pure
  HTTP + SQLite. A parked Iterate gate does sit in `awaiting_input`, so it
  counts toward the `runs_today` daily-cap denominator like any other parked
  gate (accepted; the cap only guards Claude-run statuses transitively).
- Re-submitting a shipped feature resets the previous lap's watch row,
  readings, and ledger row inside the atomic re-intake — the new lap is
  measured from scratch.
- `/redo <days>` on the Iterate gate re-arms a fresh window (1–365; anything
  else is refused with the reason) while keeping the ORIGINAL pre-ship
  baseline.

## 9. Graduated autonomy (Epic C: trust ladder, pins, clawback)

| Env var | Default | Meaning |
| --- | --- | --- |
| `AUTONOMY_ENABLED` | `true` | master switch: nightly scoring + the Autonomy surface + workspace pins. `false` = legacy behavior (only per-feature `gate_mode=light` auto-advances; pins ignored). Env-only and read once — **flipping it requires a restart**. |
| `AUTONOMY_WINDOW_DAYS` | `30` | rolling `stage_runs` window the scorer reads; runs that age out decay the cell back to level 0. |
| `AUTONOMY_MIN_RUNS` | `5` | cells with fewer counted runs stay level 0; level 3 also needs a clean streak of at least this many runs plus one human-answered gate in the window. |
| `AUTONOMY_AUTO_LEVEL` | `0` | computed level required to auto-advance a gate. **`0` (default) = computed levels never auto-advance** — scoring, the matrix and pins still work. Set `1`–`3` to opt in (`3` = only full trust); any other value disables the rule (never clamped). |
| `AUTONOMY_RECOMPUTE_HOURS` | `24` | nightly scorer cadence (`POST /api/autonomy/recompute`, admin, runs it on demand). |

Notes:

- **Fresh instances gate everything until a track record exists**; upgraded
  instances too — computed-level auto-advance is off until you set
  `AUTONOMY_AUTO_LEVEL`, so deploying this feature changes no gating
  behavior by itself. Pins in Settings → Autonomy override the computed
  level in both directions and are explicit admin actions.
- The safety guards (mid-run human edit, mirror down, first run after a
  `/redo`, P5 without a PR, non-boilerplate questions) always apply — under
  every pin and at every level — and P9 always parks (terminal gate).
- Clawback (per cell or per stage across the workspace, dashboard →
  Autonomy) is available to workspace members, drops levels to 0, and makes
  the cell re-earn from runs after the clawback only. Every pin change,
  clawback, level change and auto-advance lands in the `autonomy_events`
  audit table.
- These settings are env-only (not dashboard-editable context overrides);
  changes apply at the next restart.

## 10. Organizational context (Epic D: people, decisions, retrieval, Slack)

| Env var | Default | Meaning |
| --- | --- | --- |
| `PEOPLE_ROUTING_DEFAULTS` | `true` | people profiles fill EMPTY DRI slots at feature intake (exactly-one enabled workspace-member match per role; ambiguity fills nothing). Neutral: inert until profiles exist. `false` = profiles feed prompts/display only — the opt-out documented in §7. |
| `MEMORY_SEARCH_TOP_K` | `5` | FTS snippets injected into stage prompts. `0` disables the block; any value outside `1..20` also disables it (never clamped toward more context). |
| `SLACK_INGEST_ENABLED` | `false` | **the D3 flag** — Slack read ingestion of decision candidates. Even when `true` it needs `SLACK_BOT_TOKEN` and a per-workspace channel allowlist. |
| `SLACK_BOT_TOKEN` | *(empty)* | Slack bot token. Secret: env-only, never stored in a workspace row, never returned by any API, never interpolated into logs/details. |
| `SLACK_API_BASE` | `https://slack.com/api` | test seam; leave alone in production. |
| `SLACK_INGEST_INTERVAL_SECONDS` | `600` | ingest loop cadence. |
| `SLACK_DECISION_EMOJI` | `pushpin` | reaction NAME (no colons) marking a decision message; the `!decision` message prefix is always recognized when the flag is on. |

Per-workspace: `slack_channels` (Settings → Workspaces, or
`PATCH /api/workspaces/{id}` with a list of channel ids; `""` clears). A
channel may be allowlisted by exactly ONE workspace — candidates route
deterministically. When a channel is FIRST allowlisted its read watermark is
initialized to *now*: ingestion is forward-only, so enabling the flag never
floods the inbox with historical candidates.

Slack app scopes (read-only by design): `channels:history` covers PUBLIC
channels only — add `groups:history` for private channels the bot is in, and
`reactions:read` for the emoji convention. `chat.getPermalink` needs no
write scope; the engine never posts to Slack from this feature. Known
limits (documented, by Slack API construction): a reaction added to a
message older than the ~7-day re-scan overlap is not seen, and thread
replies are not captured unless broadcast to the channel.

What ingestion produces: decision-registry rows with `status='candidate'`,
parked in every member's inbox for **confirm** (with optional scope/title/
text edits; org scope is admin-only) or **dismiss**. Candidates are
quarantined until confirmed: never indexed for retrieval, never rendered
into any prompt, excluded from the default `GET /api/decisions` view.
Dismissals are remembered — the row is kept, so a re-scan can never
re-propose it.

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
