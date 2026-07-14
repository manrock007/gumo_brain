# gumo_brain

Self-hosted **Sentry + manual requests → Claude Code autofix** service. Work arrives two
ways: a Sentry alert rule fires (or an issue is triggered manually), or a team member
**submits a request** (bug fix / change request) on the dashboard. Either way gumo_brain
opens (or adopts) a **ClickUp ticket** — the keeper of record for everything Claude and
the humans decide — runs **headless Claude Code** (`claude -p`) in a clean clone of the
owning repo, runs the repo's **unit tests**, and opens a **draft PR**.
**Human-in-the-loop:** complex Sentry issues, and *every* manual request, park in
`awaiting_input` — Claude posts root cause + fix strategy + concrete questions
("new field on model A, or new model B?") to the ClickUp ticket, and the dashboard
surfaces those questions in the queue so you can answer them in place.

```
Sentry alert rule ──webhook──▶ gumo_brain (FastAPI) ◀──trigger / submit request── dashboard
                                 │ verify signature            │
                                 ▼                             ▼
                               GRADING (sentry only)     ClickUp ticket created or adopted
                                 │ score >= threshold      (title+summary, or pasted URL)
                                 ▼                             │
                               ClickUp ticket created          ▼
                                 ▼                       phase 1: ANALYSIS ONLY
                               claude -p (headless)      root cause + fix strategy
                                 │                             │ always
                    ┌────────────┼──────────────┐              ▼
                    ▼            ▼              ▼        awaiting_input ◀──────────────┐
                draft PR    NEEDS_INPUT      NO_FIX      questions on ticket +         │
                + ticket    → awaiting_input → analysis  dashboard queue               │
                + Sentry      (same as ──▶)    on ticket       │ answer on dashboard,  │
                  comment                                      │ or /proceed on ticket │
                                                               ▼                       │
                                                         phase 2: implement ──────────-┘
                                                         (may ask again)  └▶ draft PR
```

Also runs a periodic **sweep** that grades the top unresolved Sentry issues of the last
14 days, so legacy/backlog items get picked up even without alert-rule webhooks.

## Endpoints

- `GET /` — dashboard (basic auth, user `gumo`): pending / in-progress / awaiting-input /
  completed jobs with Sentry, ClickUp and PR links; a manual "Fix it" trigger (Sentry issue
  id, short id `GUMO-1A`, or URL); a "Submit a request" form (ClickUp URL, or title+summary
  + project); and inline answering of awaiting-input questions.
- `POST /api/trigger` — manual Sentry fix trigger, as JSON API (basic auth)
- `POST /api/tasks` — submit a manual request: `{project, clickup?}` to adopt an existing
  ClickUp task by URL/id, or `{project, title, summary?}` to create one (basic auth)
- `POST /api/jobs/{job_id}/answer` — answer an awaiting-input job from the dashboard:
  `{action: "proceed"|"skip", answer}`; the decision is posted to the ClickUp ticket
  first, then phase 2 runs (basic auth)
- `GET /api/jobs` — job list (basic auth)
- `GET /api/projects` — configured project → repo mappings (basic auth)
- `POST /webhooks/sentry` — Sentry internal-integration webhook (HMAC signature-verified)
- `GET /health` — liveness + queue depth (no auth)

## Grading

Webhook floods don't reach Claude. Each issue is fetched from the Sentry API and scored;
it's **rejected outright** if resolved/ignored/archived, in an unmapped project, level
info/debug, or stale (`GRADE_STALE_DAYS`). Otherwise it scores on level, unhandled-ness,
users affected, event volume and recency (minus points if a human is already assigned),
and must reach `GRADE_MIN_SCORE`. Manual triggers bypass grading. Skips are recorded with
their reasons and visible on the dashboard — no ClickUp ticket is created for them.

## Manual requests (ClickUp as the conveyor belt)

Anyone with dashboard access can hand Claude a bug fix or change request. Two ways:

- **Title + summary** on the dashboard → a ClickUp ticket is created in the autofix list.
- **Paste a ClickUp task URL** → that ticket is adopted as-is (its name + description
  become the request).

Both need a **project** (picks the target repo from `REPO_MAP`). Manual requests skip
grading and the daily cap (a human vouched for them), and always run in two phases:

1. **Analysis only** — Claude explores the repo, writes up root cause / current behaviour,
   a fix strategy (with options + trade-offs where several are defensible), and a
   `## Questions` list. It is forbidden from changing code in this phase. The write-up
   lands on the ClickUp ticket and the job parks as `awaiting_input`.
2. **Implement** — after a human answers, Claude implements with the guidance treated as
   the decision, runs the tests, and opens a draft PR (branch `brain/task-<id>`). If a new
   decision surfaces mid-fix it can ask again, and the loop repeats.

Every hand-off is a ClickUp comment, so the ticket is the full record of the work.

## Human-in-the-loop (`awaiting_input`)

Sentry issues Claude judges COMPLEX (product decision, several defensible options) and
*all* manual requests park as `awaiting_input` with Claude's analysis + questions posted
to the ClickUp ticket. Answer wherever is convenient:

- **Dashboard** (primary): the awaiting-input card shows the questions in the queue
  itself, with a reply box and Proceed/Skip buttons. Your answer is posted to the ClickUp
  ticket as a `**Decision (via dashboard)**` comment before the job advances — ClickUp
  stays the keeper of record.
- **ClickUp ticket**: reply `/proceed use option B, keep the old behaviour behind the
  flag` to run phase 2 with your guidance as the decision, or `/skip` to drop it. The
  service polls awaiting tickets every `CLICKUP_POLL_SECONDS`.

## Unit tests

Per-repo `setup_cmd` / `test_cmd` in `REPO_MAP` tell Claude how to run the suite inside the
container (Node 22 is bundled; `npm ci` results persist across runs in the workspace volume).
Currently: `web` → vitest, `react-native` → jest. `gumo` (Django) needs postgres/GDAL and is
not runnable in-container yet — Claude is told to say so in the PR body.

## Other guardrails

- One branch/PR per job (`brain/sentry-<id>` / `brain/task-<id>`); a job with an open PR
  is never re-run.
- Failed runs respect `ISSUE_COOLDOWN_HOURS`; `MAX_RUNS_PER_DAY` caps Claude invocations
  (manual triggers and requests exempt). One job runs at a time.
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
