# CtrLoop — admin setup & test-drive runbook

A **linear, do-this-then-that** guide to standing up a CtrLoop instance end to
end — SQLite quickstart or a full enterprise stack (Postgres + SSO + RBAC +
API billing) — and then taking it for a test drive.

This is the **runbook**. [`OPERATIONS.md`](OPERATIONS.md) is the exhaustive
**reference** (every env var, every default) and [`ENGINE.md`](ENGINE.md) is
the product spec. Each step here points at the OPERATIONS section that holds
the full knob list.

> Convention: `https://ctrloop.example.com` is your instance's public URL.
> Everything is a REST API, so every dashboard step has a `curl` equivalent —
> both are shown for the setup-critical ones.

---

## 0. Decide your shape first

| | **Quickstart** (test drive) | **Enterprise** (team / prod) |
|---|---|---|
| Database | SQLite (zero config) | Postgres |
| Workers | 1 (single process) | N (multi-worker) |
| Model billing | personal Max token OK *(solo only)* | `ANTHROPIC_API_KEY` (required for >1 user) |
| Auth | local password | OIDC SSO + local break-glass |
| Runner | local subprocess | local, or sandboxed containers |

You can start Quickstart and grow into Enterprise later — every enterprise
feature is additive and off by default, and SQLite→Postgres is a documented
migration (§4.2). **Do the whole test drive (§7) on Quickstart first**, then
layer in SSO/RBAC/Postgres.

---

## 1. Prerequisites — gather these before you start

**Always needed**
- A host that can run the container with a persistent volume for `DATA_DIR`.
- **GitHub access to your repos** — either a fine-grained PAT (`Contents: RW`
  + `Pull requests: RW`, scoped to the repos) *or* a GitHub App (§5.4).
- **Model auth** — `ANTHROPIC_API_KEY` from console.anthropic.com (required
  the moment more than one person uses the instance), or a personal
  `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token` for a solo test drive.
- TLS in front of the app (any reverse proxy) — set `SESSION_COOKIE_SECURE=true`.

**Enterprise / optional**
- **Postgres 14+** (managed or self-hosted) for multi-worker.
- **OIDC provider** (Okta / Entra / Google Workspace / any OIDC) — you'll
  register an app and get issuer + client id + secret.
- **Mixpanel** service account (for the outcome loop's real metric reads).
- **Sentry** internal integration, **ClickUp** token, **Slack** bot/webhook —
  each optional, each enables one lane.

---

## 2. Quickstart: boot a working instance (SQLite)

Minimum env — this is the whole from-zero path (pinned by
`tests/test_install_from_zero.py`):

```bash
DATA_DIR=/data
CTRLLOOP_ADMIN_USER=admin
CTRLLOOP_ADMIN_PASSWORD=<a strong password>   # first-boot admin bootstrap
GITHUB_TOKEN=<fine-grained PAT>
ANTHROPIC_API_KEY=<key>                        # or CLAUDE_CODE_OAUTH_TOKEN (solo)
PUBLIC_BASE_URL=https://ctrloop.example.com
SESSION_COOKIE_SECURE=true
```

Run the image with `/data` on a persistent volume. First boot:
- creates ONE admin user + ONE empty workspace named "Default";
- knows nothing about any product (all code defaults are neutral).

Verify health, then sign in:

```bash
curl -s https://ctrloop.example.com/health          # {"status":"ok","queued":0}
```

Open `https://ctrloop.example.com/`, sign in as `admin`. You'll see the
**first-run checklist** — that's your §3 to-do list. (Reference:
OPERATIONS.md §2.)

---

## 3. First-run configuration (both shapes)

Work the checklist top to bottom. All admin-only.

### 3.1 Business context & product name
Dashboard → **Project context** panel (or `PUT /api/context`):
```bash
curl -s -u admin:$PW -X PUT https://ctrloop.example.com/api/context \
  -H 'content-type: application/json' \
  -d '{"product_name":"Acme","business_context":"Acme is a payments app across two repos: api (canonical) and web…"}'
```
The business context is injected into **every** run's prompt — write it once,
well. (Repos and the canonical project are NOT set here — they're per
workspace; §3.2.)

### 3.2 Add your repositories (a workspace)
Dashboard → **Settings → Workspaces** (or `PATCH /api/workspaces/{id}`):
```bash
curl -s -u admin:$PW -X PATCH https://ctrloop.example.com/api/workspaces/1 \
  -H 'content-type: application/json' \
  -d '{"repos":[
        {"slug":"api","repo":"acme/api","base":"main","test_cmd":"pytest -q"},
        {"slug":"web","repo":"acme/web","base":"main","setup_cmd":"npm ci","test_cmd":"npm test","allow":["Bash(npm:*)","Bash(node:*)"]}
      ],
      "canonical_project":"api"}'
```
The slugs immediately appear in intake pickers (`GET /api/projects`). The
`canonical_project` hosts product-scope memory. (Reference: ENGINE.md §10, §12.)

### 3.3 Bootstrap product memory
Dashboard → **Product brain** panel → bootstrap each repo. Each produces a
**draft PR** you review and merge — that seeds `.ctrlloop/memory/`. Warm starts
begin here.

### 3.4 Invite your team
Dashboard → **Settings → Users**: add members with temporary passwords (forced
change at first sign-in). Assign each to workspaces. Role details in §6.

> Enterprise: do §4 (Postgres) and §5 (SSO) **before** inviting people, so
> accounts land under the right auth and billing from day one.

### 3.5 Locked out? Break-glass password reset

If you forget the admin password (or an account trips the failed-attempt
lockout), reset it from inside the running container. `admin_user` goes through
the app's own database layer, so it targets the exact DB the server reads —
SQLite **or** Postgres — with no risk of writing the wrong file:

```bash
# see the exact usernames (never prints password hashes)
docker exec -it gumo-brain python -m scripts.admin_user list

# reset a password and clear any lockout / forced-change / disabled flags
docker exec -it gumo-brain python -m scripts.admin_user reset-password --user gumo

# if the users table is somehow empty, mint a fresh admin
docker exec -it gumo-brain python -m scripts.admin_user create-admin --user founder
```

The command prompts for the new password (no echo, never stored in shell
history). For automation, pipe it in with `--password-stdin` or set
`CTRLLOOP_NEW_PASSWORD`. After a reset you can log in immediately — no restart
needed. (First boot with an **empty** users table auto-bootstraps an admin from
`CTRLLOOP_ADMIN_PASSWORD` / `DASHBOARD_PASSWORD`; the reset above is for when a
user already exists but you can't get in.)

---

## 4. Enterprise: database & scale

### 4.1 Postgres (OPERATIONS.md §20.1)
```bash
DATABASE_URL=postgresql://user:pass@host/ctrloop
# install extra deps in the image: pip install -r requirements-postgres.txt
```
Empty `DATABASE_URL` = SQLite (default). With Postgres, **Alembic owns the
schema** — run `alembic upgrade head` against the empty DB before first boot.

### 4.2 Migrate an existing SQLite instance to Postgres (no data loss)
1. Create an empty Postgres DB.
2. `alembic upgrade head` (materializes the baseline schema).
3. `python scripts/sqlite_to_pg.py --sqlite $DATA_DIR/brain.db --pg "$DATABASE_URL"`
4. Set `DATABASE_URL`, restart.

### 4.3 Multiple workers (OPERATIONS.md §20.2) — Postgres only
```bash
WORKERS=3                       # >1 REQUIRES Postgres
WORKER_ID=$(hostname):$$        # default is hostname:pid; scopes crash recovery
```
Jobs are claimed with `FOR UPDATE SKIP LOCKED`; per-repo serialization uses
Postgres advisory locks. **Multi-host caveat:** give each host its own
`CLAUDE_CONFIG_DIR`, or run `SESSION_PERSISTENCE=false` — never share one CLI
config dir across hosts.

### 4.4 Switch model billing to the API key (OPERATIONS.md §19) — required before user #2
Anthropic policy prohibits routing other users' requests through a personal
Max plan. Before onboarding anyone but yourself:
```bash
ANTHROPIC_API_KEY=<key>         # set this
# unset CLAUDE_CODE_OAUTH_TOKEN
```
Startup logs the detected backend and **warns loudly** if a Max token is used
with >1 enabled user.

---

## 5. Enterprise: identity (OIDC SSO)

Full reference: OPERATIONS.md §12.

### 5.1 Register the app with your IdP
- Redirect / callback URL: `https://ctrloop.example.com/auth/oidc/callback`
- Scopes: `openid email profile` (add your groups scope if you'll map roles)
- Note the **issuer**, **client id**, **client secret**, and the **claim** that
  carries group membership (e.g. `groups`).

### 5.2 Configure CtrLoop
```bash
OIDC_ENABLED=true
OIDC_ISSUER=https://acme.okta.com
OIDC_CLIENT_ID=<client id>
OIDC_CLIENT_SECRET=<secret>          # never logged, never returned by any API
OIDC_REDIRECT_URL=https://ctrloop.example.com/auth/oidc/callback
OIDC_SCOPES=openid email profile groups
OIDC_ROLE_CLAIM=groups
OIDC_ROLE_MAP={"ctrloop-admins":"instance_admin","engineers":"member"}
OIDC_ADMIN_GROUP=ctrloop-admins       # membership here → instance_admin
OIDC_DEFAULT_ROLE=member              # when nothing maps
OIDC_ROLE_SYNC=true                   # re-map role on each login
```
`oidc_configured` **fails closed** on partial config — all of issuer + client
id + secret + redirect must be present or SSO stays off.

### 5.3 Verify & the break-glass guarantee
- The login page shows an SSO button (from the public `GET /api/auth/providers`
  — no secrets leak).
- Sign in via the IdP; a new user is JIT-provisioned, auto-linked **only** on
  the stable `sub`. An SSO email that collides with an existing *local*
  account is refused (never overwrites a local password).
- **Local password login is never disabled.** `CTRLLOOP_LOCAL_LOGIN=false`
  only hides the form; a break-glass admin can always get in — keep one local
  admin password in your secrets vault.

> SAML and SCIM are scaffolds behind the same interface (`SAML_ENABLED` /
> `SCIM_ENABLED` / `SCIM_TOKEN`), inert by default.

### 5.4 (Optional) GitHub App instead of a PAT (OPERATIONS.md §16)
```bash
GITHUB_APP_ID=<id>
GITHUB_APP_PRIVATE_KEY=@/run/secrets/ghapp.pem   # PEM, or @/path for a mounted file
GITHUB_TOKEN=<PAT>                                # still the documented fallback
```
The engine mints a short-lived, single-repo installation token **per run** and
hands only that to the subprocess — never the PAT, never the private key.

---

## 6. Enterprise: RBAC v2 (OPERATIONS.md §14)

Two axes:
- **Instance role** (`users.role`): `instance_admin` | `member`. Admins can
  configure the instance and every workspace.
- **Workspace role** (`workspace_members.role`): `admin` | `member` | `viewer`,
  plus a per-member **repo allow-list** (`repos`, `[]` = all repos).

| Who | Can |
|---|---|
| instance_admin | everything, everywhere |
| workspace admin | configure that workspace (repos, integrations, members) |
| member | submit work, answer gates they own |
| viewer | read-only — every mutation is 403 |

Assign a member's workspace role + repo restriction:
```bash
curl -s -u admin:$PW -X PUT https://ctrloop.example.com/api/workspaces/1/members \
  -H 'content-type: application/json' \
  -d '{"username":"priya","member":true,"role":"member","repos":["api"]}'   # priya acts only on the api repo
```
Config mutations require the admin role **of that scope**; the last instance
admin can never be demoted. Legacy `admin` rows upgrade to `instance_admin` on
boot.

### 6.1 API tokens for automation (OPERATIONS.md §13)
Replace HTTP Basic passwords with scoped tokens:
```bash
curl -s -u priya:$PW -X POST https://ctrloop.example.com/api/tokens \
  -d '{"name":"ci"}'           # returns ctl_… ONCE; only its hash is stored
# then: Authorization: Bearer ctl_…
```
Once an account has an active token, Basic password auth is refused for it
(except break-glass admins). `API_TOKEN_DEFAULT_TTL_DAYS` sets expiry (0 = none).

### 6.2 Audit & SIEM (OPERATIONS.md §15)
Every gate decision (both channels, with actor), login, config mutation,
token/user lifecycle, budget block/override and autonomy event is in the
append-only `audit_log`.
```bash
# dashboard viewer:
curl -s -u admin:$PW https://ctrloop.example.com/api/audit
# SIEM export (JSONL, cursor-paged; next cursor in X-Next-Cursor):
curl -s -u admin:$PW 'https://ctrloop.example.com/api/audit/export?limit=500'
```

---

## 7. Test drive — the end-to-end happy path

Do this on the Quickstart instance to see the whole loop. Assumes §3 is done
(context + one workspace with an `api` repo + memory bootstrapped).

1. **Set up the team roles.** Add a second user (`priya`), assign her to the
   workspace as a `member`. You are the founder DRI, she's the dev DRI.
   *(To exercise ClickUp attribution: Settings → Users → link each person's
   ClickUp id — OPERATIONS.md §7.)*

2. **Submit a feature with a metric goal** (dashboard intake, or):
   ```bash
   curl -s -u admin:$PW -X POST https://ctrloop.example.com/api/features \
     -H 'content-type: application/json' \
     -d '{"project":"api","title":"Add referral cashback",
          "founder_dri":"admin","dev_dri":"priya",
          "success_metric":"referral_conversion","metric_target":"6.0","metric_window_days":14}'
   ```
   The success metric is **required at intake** — P0/P1 fail closed without a
   `## Success metric` section.

3. **Walk the gates (role-exclusive).** Watch it run P0 → P9 in the detail
   pane. Each stage parks at a gate:
   - **P0 Intake, P1 PRD, P9 Ship** are **founder** gates — only you can
     `/proceed`, `/redo`, `/skip`. If Priya tries, she's refused with an
     explanation.
   - **P2–P8** are **dev** gates — Priya's to answer; you're refused.
   - Before deciding, use **Ask** in the composer to interrogate the engine
     about the work (fast lane answers in ~1-2s).
   - Answer on the dashboard, or (if ClickUp is wired) by commenting on the
     ticket.

4. **See the draft PR.** P5 opens a draft PR; the shepherd drives the Sentry
   review bot automatically. Review and merge it.

5. **Watch the outcome loop.** On merge, a `watch-<id>` job spawns
   (no Claude cost — pure HTTP+DB). With Mixpanel wired (§8 / OPERATIONS.md §8)
   it reads `referral_conversion` daily for 14 days, then parks a
   **founder-owned Iterate gate** with a `moved / flat / regressed` verdict.
   `/proceed` writes the verdict into product memory. See it in **Outcomes**.

6. **Watch trust accrue.** After a few clean runs, **Settings → Autonomy**
   shows per-(stage, repo) levels. To let proven stages auto-advance, set
   `AUTONOMY_AUTO_LEVEL=2` (restart). Pin any stage to *always gate* — pins
   always win. (OPERATIONS.md §9.)

7. **Check the receipts & health.**
   ```bash
   curl -s -u admin:$PW https://ctrloop.example.com/api/features/<id>/stats   # cost/turns/gate-wait per stage
   curl -s https://ctrloop.example.com/health/ready                           # {ready, checks:{db,worker,scheduler}}
   curl -s -H "Authorization: Bearer $METRICS_TOKEN" https://ctrloop.example.com/metrics
   ```

---

## 8. Optional lanes — enable what you use

Each is off until configured; each adds one capability. Full knobs in the
cited OPERATIONS section.

| Lane | Turn on with | Ref |
|---|---|---|
| **Sentry autofix** | `SENTRY_ORG` + `SENTRY_AUTH_TOKEN` (+ webhook → `/webhooks/sentry`) | §6 |
| **ClickUp** (tickets as a front door + phone gate answers) | `CLICKUP_TOKEN` + per-workspace list id | §6 |
| **Slack gate nudges** | per-workspace incoming-webhook URL | §11 |
| **Slack decision ingestion** (FLAG) | `SLACK_INGEST_ENABLED=true` + `SLACK_BOT_TOKEN` + per-workspace channels | §10 |
| **Analytics / real metrics** | `ANALYTICS_PROVIDER=mixpanel` + per-workspace `analytics_config` | §8 |
| **Proactive routines** (standup, risk, proposals, planning) | on by default (`ROUTINES_ENABLED`); tune schedules | §11 |
| **Budgets & spend caps** | `BUDGET_MONTHLY_USD` (or per-workspace) | §18 |
| **Sandboxed runs** (FLAG) | `RUNNER_BACKEND=container` + image + egress network | §20.3 |

---

## 9. Security hardening checklist (before real traffic)

- [ ] `SESSION_COOKIE_SECURE=true` (behind TLS).
- [ ] Model billing on `ANTHROPIC_API_KEY` if more than you will use it (§4.4).
- [ ] OIDC on, with **one** local break-glass admin password stored safely (§5.3).
- [ ] `METRICS_TOKEN` set (else `/metrics` requires admin auth — never open).
- [ ] Secrets via `SECRETS_PROVIDER=file` (or vault) rather than raw env where
      you can; subprocess env is already allow-listed so operator secrets never
      reach the model shell (OPERATIONS.md §17).
- [ ] Audit export wired to your SIEM (§6.2); set `AUDIT_RETENTION_DAYS` if the
      SIEM is the archive.
- [ ] GitHub App instead of a broad PAT (§5.4).
- [ ] Backups: `sqlite3 … ".backup"` (SQLite) or your Postgres backup; the DB
      is the whole instance (OPERATIONS.md §3).

---

## 10. Where to go deeper

- **Every env var & default** → [`OPERATIONS.md`](OPERATIONS.md) §6–21.
- **The product model** (pipeline, gates, memory, sync) → [`ENGINE.md`](ENGINE.md).
- **Swapping ClickUp→Jira / GitHub→GitLab** → [`TRACKER-JIRA.md`](TRACKER-JIRA.md),
  [`VCS-GITLAB.md`](VCS-GITLAB.md).
- **Worked example config** (the original Gumo instance) → OPERATIONS.md appendix.
