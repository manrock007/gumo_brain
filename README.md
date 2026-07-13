# gumo_brain

Self-hosted Sentry → Claude Code autofix service. When a Sentry alert rule fires (or an
issue is triggered manually), gumo_brain **grades** the issue, opens a **ClickUp ticket**,
runs **headless Claude Code** (`claude -p`) in a clean clone of the owning repo, runs the
repo's **unit tests**, and — if Claude finds a clear root cause — opens a **draft PR**.
Complex issues go through a **human-in-the-loop** step: Claude posts its root-cause
analysis and questions on the ClickUp ticket and waits for a `/proceed` reply.

```
Sentry alert rule ──webhook──▶ gumo_brain (FastAPI)          ◀──manual trigger── dashboard
                                 │ verify signature
                                 ▼
                               GRADING  — skip resolved/ignored/stale/low-impact issues
                                 │ score >= threshold (or forced)
                                 ▼
                               ClickUp ticket created ("Sentry Autofix" list)
                                 ▼
                               claude -p  (headless, allow-listed tools, runs unit tests)
                                 │
                    ┌────────────┼──────────────┐
                    ▼            ▼              ▼
                draft PR    NEEDS_INPUT      NO_FIX
                + ticket    → analysis on    → analysis on
                + Sentry      ticket, wait     ticket + Sentry
                  comment     for /proceed
                              or /skip ──▶ phase 2 fix with human guidance ──▶ draft PR
```

Also runs a periodic **sweep** that grades the top unresolved Sentry issues of the last
14 days, so legacy/backlog items get picked up even without alert-rule webhooks.

## Endpoints

- `GET /` — dashboard (basic auth, user `gumo`): pending / in-progress / awaiting-input /
  completed jobs with Sentry, ClickUp and PR links, plus a manual "Fix it" trigger that
  accepts an issue id, short id (`GUMO-1A`) or Sentry URL and returns the ClickUp ticket.
- `POST /api/trigger` — same, as JSON API (basic auth)
- `GET /api/jobs` — job list (basic auth)
- `POST /webhooks/sentry` — Sentry internal-integration webhook (HMAC signature-verified)
- `GET /health` — liveness + queue depth (no auth)

## Grading

Webhook floods don't reach Claude. Each issue is fetched from the Sentry API and scored;
it's **rejected outright** if resolved/ignored/archived, in an unmapped project, level
info/debug, or stale (`GRADE_STALE_DAYS`). Otherwise it scores on level, unhandled-ness,
users affected, event volume and recency (minus points if a human is already assigned),
and must reach `GRADE_MIN_SCORE`. Manual triggers bypass grading. Skips are recorded with
their reasons and visible on the dashboard — no ClickUp ticket is created for them.

## Human-in-the-loop (`/proceed` / `/skip`)

When Claude judges a fix COMPLEX (product decision, several defensible options), it stops
before changing anything and posts its analysis + concrete questions to the ClickUp ticket.
The job parks as `awaiting_input`. Reply on the ticket:

- `/proceed use option B, keep the old behaviour behind the flag` — runs phase 2: the fix,
  with your guidance treated as the decision.
- `/skip` — drops the issue.

The service polls awaiting tickets every `CLICKUP_POLL_SECONDS`.

## Unit tests

Per-repo `setup_cmd` / `test_cmd` in `REPO_MAP` tell Claude how to run the suite inside the
container (Node 22 is bundled; `npm ci` results persist across runs in the workspace volume).
Currently: `web` → vitest, `react-native` → jest. `gumo` (Django) needs postgres/GDAL and is
not runnable in-container yet — Claude is told to say so in the PR body.

## Other guardrails

- One branch/PR per issue (`brain/sentry-<id>`); an issue with an open PR is never re-run.
- Failed runs respect `ISSUE_COOLDOWN_HOURS`; `MAX_RUNS_PER_DAY` caps Claude invocations
  (manual triggers exempt). One job runs at a time.
- Claude gets an allow-list of tools, a hard timeout, and a prompt that treats stack-trace
  content as untrusted data. PRs are always drafts.
- ClickUp is best-effort: an outage degrades tracking, never fixing.

## Setup

### 1. Claude OAuth token (uses your Max subscription, not API billing)

```bash
claude setup-token
```

Browser flow; prints a long-lived (1 year) token `sk-ant-oat01-…` → `CLAUDE_CODE_OAUTH_TOKEN`.
Headless runs draw from the Max plan usage pool. Rotate yearly.

### 2. GitHub token

Fine-grained PAT scoped to the mapped repos: `Contents: RW` + `Pull requests: RW` → `GITHUB_TOKEN`.

### 3. ClickUp

Personal API token (ClickUp → Settings → Apps) → `CLICKUP_TOKEN`. The "Sentry Autofix"
list in Gumo Space is `901615853762` (default). Status sync adapts to whatever statuses
the list has; comments always work.

### 4. Sentry internal integration

Sentry → Settings → Custom Integrations → "Claude Autofix":

- **Webhook URL:** `https://gumo.co.in/brain/webhooks/sentry`
- **Alert Action:** ON; **Permissions:** Issue & Event = Read & Write
- **Client Secret** → `SENTRY_CLIENT_SECRET`; create a **Token** → `SENTRY_AUTH_TOKEN`

Then per project, create Alert Rules for what should auto-fire (grading still filters).

### 5. Secrets & deploy (via gumoiac)

AWS Secrets Manager secret `gumo/brain` (JSON): `CLAUDE_CODE_OAUTH_TOKEN`, `GITHUB_TOKEN`,
`SENTRY_CLIENT_SECRET`, `SENTRY_AUTH_TOKEN`, `CLICKUP_TOKEN`, `DASHBOARD_PASSWORD`.
Ansible renders `/opt/gumo/.env.brain`; nginx exposes `/brain/` on gumo.co.in.
Dashboard: `https://gumo.co.in/brain/` (user `gumo` + `DASHBOARD_PASSWORD`).

GitHub Actions needs repo secrets: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`AWS_REGION`, `ECR_BRAIN_REPO`, `EC2_SSH_PRIVATE_KEY`, `EC2_HOST`.

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in tokens; set DATA_DIR=./data
uvicorn app.main:app --reload --port 8010 --env-file .env
```

Simulate a webhook:

```bash
BODY='{"action":"triggered","data":{"event":{"issue_id":"123","title":"Test"}}}'
SIG=$(S="$SENTRY_CLIENT_SECRET" B="$BODY" python -c "import hmac,hashlib,os;print(hmac.new(os.environ['S'].encode(),os.environ['B'].encode(),hashlib.sha256).hexdigest())")
curl -X POST localhost:8010/webhooks/sentry \
  -H "Content-Type: application/json" \
  -H "Sentry-Hook-Resource: event_alert" \
  -H "Sentry-Hook-Signature: $SIG" \
  -d "$BODY"
```
