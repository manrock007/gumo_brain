# CtrlLoop (formerly gumo_brain)

Self-hosted **software-development engine**: Sentry errors, manual requests, and full
**feature pipelines** in; **draft PRs** out — with humans approving every consequential
decision from a dashboard or ClickUp. Runs **headless Claude Code** (`claude -p`) in
clean clones of the owning repos.

Four job kinds:

| kind | in | flow |
|------|----|------|
| `sentry` | alert webhook / manual / sweep | grade → fix → draft PR (HITL only if COMPLEX) |
| `task` | dashboard (title or ClickUp URL) | analyse → **gate** → implement → draft PR |
| `feature` | dashboard (title or ClickUp URL) | **P0 Intake → P9 Ship, human gate after every stage** |
| `memory` | dashboard, per project | bootstrap `.ctrlloop/` product memory → draft PR |

The engine's three core mechanics (full spec: **[docs/ENGINE.md](docs/ENGINE.md)**;
running an instance — install/backup/upgrade: **[docs/OPERATIONS.md](docs/OPERATIONS.md)**):

- **Staged pipeline with gates.** Features run P0 Intake / P1 PRD / P2 Recon /
  P3 Design / P4 Plan / P5–P6 Build / P7 Test / P8 Review / P9 Ship — one headless run
  per stage, parked at a gate after each. Answer `/proceed`, `/redo` (any earlier stage
  too) or `/skip` on the ticket or inline on the dashboard. Code gates show
  harness-captured evidence (diffstat, compare link, draft PR from P5 on).
- **Shared artifacts.** Every stage's document (PRD, design, plan…) lives in git on the
  feature branch (`.ctrlloop/features/<job>/` — legacy repos keep `.gumo/`) AND as an editable ClickUp subtask. Humans
  edit in ClickUp — even mid-run — and the engine folds edits back into git with
  human-wins reconciliation that tolerates ClickUp's markdown mangling. Git is the
  source of truth; ClickUp is the editing surface; neither side's work is ever lost.
- **Product memory.** `.ctrlloop/memory/` (per repo) + `.ctrlloop/product/` (canonical repo)
  hold curated, git-versioned knowledge — what the product is, how it's built, the
  codebase map, conventions, per-entry ADRs and changelog. Every stage prompt warms up
  from it (capped excerpts + file pointers); every shipped PR feeds it back. Bootstrap
  once per repo from the dashboard; freshness is tracked and shown.

**Human-in-the-loop:** complex Sentry issues, *every* manual request, and *every*
feature stage park in `awaiting_input` — the dashboard surfaces the exact questions
("new field on model A, or new model B?") in the queue so you answer them in place;
ClickUp comments work identically, and both channels are raced safely (single-writer
compare-and-set).

**Receipts:** every stage run records cost, turns, wall-clock, gate wait and redo count
(`stage_runs`) — visible per feature on the dashboard, so the efficiency claim is
measured, not asserted.

```
Sentry alert rule ──webhook──▶ CtrlLoop (FastAPI) ◀──trigger / submit request── dashboard
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

All `api/*` and `/` require a signed-in user (cookie session via `/login`, or
per-user HTTP Basic for automation — docs/ENGINE.md §11); `/health`, `/login`,
static assets and the webhook are open. Roles: admin (configuration + users)
and member (submits work, answers gates). See MIGRATION-CTRLLOOP.md for the
rename/infra checklist.

- `GET /` — dashboard: intake (Sentry fix / request / **feature pipeline**), queue
  columns with inline gate answering (Proceed / Redo / Skip), feature **stage strips**
  P0–P9, per-feature stats (cost/duration/gate-wait per stage), and the **Product
  brain** panel (memory freshness + bootstrap).
- `POST /api/trigger` — manual Sentry fix trigger `{issue}`
- `POST /api/tasks` — 2-phase request: `{project, clickup? | title+summary}`
- `POST /api/features` — P0–P9 pipeline: same body + `founder_dri?` / `dev_dri?`
  (ClickUp user id or username — gate ownership + notifications; `owner?` is the
  deprecated alias for `dev_dri`), `related_to?` (sibling pipeline ids for
  cross-repo features), and the outcome-loop goal `success_metric?` /
  `metric_target?` / `metric_window_days?` (1–365; docs/ENGINE.md §2b — a
  merged feature is then measured and a verdict parks a founder-owned
  Iterate gate)
- `POST /api/jobs/{job_id}/answer` — `{action: proceed|redo|skip, answer, override?}`;
  `redo` accepts a `P<k>` prefix in the answer to re-run an earlier stage; 409 if the
  gate was already answered via ClickUp; 403 if a role-exclusive gate (dual DRIs,
  docs/ENGINE.md §2) is answered by a non-owner — `override: true` is the audited
  admin bypass
- `GET /api/inbox` — the per-person "Awaiting you" queue: gates you own +
  unassigned gates, overdue first (per-workspace `gate_sla_hours` SLA)
- `GET /api/features/{job_id}/stats` — per-stage telemetry (runs, guidance, artifacts)
- `GET /api/outcomes` — the **outcome ledger**: measured verdicts
  (moved / flat / regressed / unmeasured) per shipped feature + the
  distribution, membership-scoped
- `GET /api/memory` / `GET /api/memory/{project}` — cached product-memory state
- `POST /api/memory/{project}/bootstrap` — queue a memory bootstrap job
- `GET /api/jobs`, `GET /api/projects` — job list, project→repo map
- `GET|PUT|DELETE /api/context` — the **project context** (docs/ENGINE.md §10): repo
  map, canonical project, product name, business context. Editable at runtime (also via
  the dashboard's "Project context" panel); overrides persist in the DB and win over
  env/code defaults, so the engine adapts to any product/team. DELETE reverts to defaults.
- `POST /webhooks/sentry` — Sentry internal-integration webhook (HMAC verified)
- `GET /health` — liveness + queue depth

## Grading

Webhook floods don't reach Claude. Each issue is fetched from the Sentry API and scored;
it's **rejected outright** if resolved/ignored/archived, in an unmapped project, level
info/debug, or stale (`GRADE_STALE_DAYS`). Otherwise it scores on level, unhandled-ness,
users affected, event volume and recency (minus points if a human is already assigned),
and must reach `GRADE_MIN_SCORE`. Manual triggers bypass grading. Skips are recorded with
their reasons and visible on the dashboard — no ClickUp ticket is created for them.

## Feature pipelines (P0–P9)

Submit on the dashboard (project + title/summary, or adopt a ClickUp ticket by URL).
The pipeline runs one gated stage at a time:

- **P0–P4 are document stages**: read-only runs that produce `P0-intake.md` →
  `P4-plan.md` on the feature branch, each mirrored to an editable ClickUp subtask.
  P0/P1 work from product memory alone (they say so explicitly if memory is missing).
  P4 must structure the work into independently-committable **build groups** and map
  every acceptance criterion to a planned test.
- **P5–P8 are code stages**: build group 1 (opens the draft PR — the sentry review bot
  starts working during your gate waits), remaining groups, tests (honest results table
  vs the P1 acceptance criteria), self-review of the full diff.
- **P9 ships**: distills memory (changelog entry, ADRs from your gate decisions,
  touched architecture notes — riding the same PR), finalizes the PR body, and parks a
  final "ready to un-draft" gate.

Every gate: answer on the dashboard (buttons + guidance box) or comment `/proceed …`,
`/redo …` (`/redo P3 …` re-targets an earlier stage; code-stage redos hard-reset to the
stage baseline and preserve the failed attempt under `refs/ctrlloop/`), or `/skip` — on the
parent ticket or any artifact subtask. Cross-repo features: one pipeline per repo,
server first, linked via `related_to`.

**Gate chat**: before deciding, interrogate the engine from the dashboard — a read-only
run primed with the gate's documents answers questions about the work ("why option B?
what breaks at scale?") in ~15–90s, with every exchange mirrored to the ClickUp ticket
and its cost visible per answer (see [docs/CONVERSATIONS.md](docs/CONVERSATIONS.md)).

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
   the decision, runs the tests, and opens a draft PR (branch `<prefix>/task-<id>`,
   `BRANCH_PREFIX` default `ctrlloop`). If a new
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
A repo without a `test_cmd` (e.g. one whose suite needs services the container lacks) is
fine — Claude is told to say in the PR body that tests were not run here.

## Other guardrails

- One branch/PR per job (`<prefix>/sentry-<id>` / `<prefix>/task-<id>`); a job with an open PR
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

Personal API token (ClickUp → Settings → Apps) → `CLICKUP_TOKEN`, plus your autofix
list's id → `CLICKUP_LIST_ID` (or set a list per workspace in Settings → Workspaces).
Status sync adapts to whatever statuses the list has; comments always work.

### 4. Sentry internal integration

Sentry → Settings → Custom Integrations → "Claude Autofix":

- **Webhook URL:** `https://<your host>/webhooks/sentry`
- **Alert Action:** ON; **Permissions:** Issue & Event = Read & Write
- **Client Secret** → `SENTRY_CLIENT_SECRET`; create a **Token** → `SENTRY_AUTH_TOKEN`
- Set `SENTRY_ORG` (and `SENTRY_API_BASE=https://de.sentry.io/api/0` for EU orgs)

Then per project, create Alert Rules for what should auto-fire (grading still filters).

### 5. Secrets & deploy

Provide the tokens (`CLAUDE_CODE_OAUTH_TOKEN`/`ANTHROPIC_API_KEY`, `GITHUB_TOKEN`,
`SENTRY_*`, `CLICKUP_TOKEN`, `CTRLLOOP_ADMIN_PASSWORD`) via your secret store of
choice and run the container with a persistent `DATA_DIR` volume — the from-zero
walkthrough is in [docs/OPERATIONS.md](docs/OPERATIONS.md). (Example deployment:
the original Gumo instance wires these through AWS Secrets Manager + Ansible in
its own `gumoiac` repo.)

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
