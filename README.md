# gumo_brain

Self-hosted Sentry → Claude Code autofix service. When a Sentry alert rule fires,
gumo_brain fetches the issue + latest stack trace, spins up a **headless Claude Code**
run (`claude -p`) in a clean clone of the owning repo, and — if Claude finds a clear
root cause — opens a **draft PR** and comments the PR link back on the Sentry issue.

```
Sentry alert rule ──webhook──▶ gumo_brain (FastAPI, this repo)
                                 │  verify signature, dedupe, daily cap
                                 ▼
                               job queue (serial, sqlite-backed)
                                 │  fetch issue + stacktrace from Sentry API (de.sentry.io)
                                 ▼
                               claude -p  (subscription OAuth token, allow-listed tools)
                                 │  edits code in /data/workspaces/<repo>, commits, pushes
                                 ▼
                               draft PR on GitHub + comment on the Sentry issue
```

Runs as the `gumo-brain` container in the gumo docker-compose stack (see `gumoiac`),
reachable at `https://gumo.co.in/brain/…` behind nginx.

## Endpoints

- `POST /webhooks/sentry` — Sentry internal-integration webhook (signature-verified)
- `GET /health` — liveness + queue depth
- `GET /jobs` — recent jobs with status/PR URLs (internal; not routed publicly)

## Guardrails

- Only fires on `event_alert` webhooks (i.e. **your Sentry alert rules decide** what
  counts as major/trending). `issue created` handling exists behind `HANDLE_NEW_ISSUES`.
- One job at a time; one branch/PR per issue (`brain/sentry-<issue_id>`).
- Dedupe: an issue with an open PR is never re-run; failed runs respect
  `ISSUE_COOLDOWN_HOURS`. Global `MAX_RUNS_PER_DAY` cap protects your Max-plan quota.
- Claude runs with an allow-list (`Read/Grep/Glob/Edit/Write/Bash(git:*)/Bash(gh:*)`),
  a hard timeout, and a prompt that treats stack-trace content as untrusted data.
- PRs are always **drafts** — a human reviews and merges.

## Setup

### 1. Claude OAuth token (uses your Max subscription, not API billing)

On any machine where you're logged into Claude Code with your Max account:

```bash
claude setup-token
```

Follow the browser flow; it prints a long-lived (1 year) token `sk-ant-oat01-…`.
Store it as `CLAUDE_CODE_OAUTH_TOKEN`. Headless runs authenticated this way draw from
your Max plan usage pool. Set a calendar reminder to rotate it yearly.

### 2. GitHub token

Create a **fine-grained PAT** (Settings → Developer settings) scoped to the mapped
repos with `Contents: Read and write` + `Pull requests: Read and write`.
Store as `GITHUB_TOKEN`.

### 3. Sentry internal integration

Sentry → Settings → Custom Integrations → your "Claude Autofix" integration:

- **Webhook URL:** `https://gumo.co.in/brain/webhooks/sentry`
- **Alert Action:** ON (required so alert rules can target it)
- **Permissions:** Issue & Event = Read & Write (to read events and post comments)
- Copy the **Client Secret** → `SENTRY_CLIENT_SECRET`, and create a **Token** → `SENTRY_AUTH_TOKEN`.

Then in each project, create an **Alert Rule** for what "major/trending" means, e.g.
"a new issue is created AND is seen by > 10 users in 1 hour", with the action
"Send a notification via Claude Autofix".

### 4. Secrets & deploy (via gumoiac)

Production config lives in AWS Secrets Manager secret `gumo/brain` (JSON keys:
`CLAUDE_CODE_OAUTH_TOKEN`, `GITHUB_TOKEN`, `SENTRY_CLIENT_SECRET`, `SENTRY_AUTH_TOKEN`).
The gumoiac Ansible deploy renders them into `/opt/gumo/.env.brain` and runs the
`gumo-brain` service defined in the compose template. See gumoiac README.

GitHub Actions (build + deploy on push to `main`) needs repo secrets:
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`, `ECR_BRAIN_REPO`,
`EC2_SSH_PRIVATE_KEY`, `EC2_HOST` (same values as the other gumo repos —
`gumoiac/scripts/upload_gh_secrets.py` can upload them).

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in tokens; set DATA_DIR=./data
uvicorn app.main:app --reload --port 8010 --env-file .env
```

Simulate a webhook (signature = HMAC-SHA256 of the body with the client secret):

```bash
BODY='{"action":"triggered","data":{"event":{"issue_id":"123","title":"Test"}}}'
SIG=$(S="$SENTRY_CLIENT_SECRET" B="$BODY" python -c "import hmac,hashlib,os;print(hmac.new(os.environ['S'].encode(),os.environ['B'].encode(),hashlib.sha256).hexdigest())")
curl -X POST localhost:8010/webhooks/sentry \
  -H "Content-Type: application/json" \
  -H "Sentry-Hook-Resource: event_alert" \
  -H "Sentry-Hook-Signature: $SIG" \
  -d "$BODY"
```
