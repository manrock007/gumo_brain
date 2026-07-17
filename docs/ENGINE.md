# CtrlLoop — feature pipeline, shared artifacts, product memory

> Formerly "the Gumo Engine" / gumo_brain. The engine identity (CtrlLoop) is
> distinct from the configured product identity (§10); Gumo remains the
> default project context.

> Status: AS-BUILT SPEC (v2 of gumo_brain). Reviewed by a 5-lens design critique
> (sync protocol, memory, state machine, operator UX, prompt/token economics);
> the amendments from that review are folded in below. §9 lists deliberate
> deferrals.

## 0. Problem statement

gumo_brain v1 fixes bugs. Building features — the actual product work — has
three unsolved problems:

1. **Long-horizon work needs a staged pipeline** with human veto power at each
   step: requirements → PRD → design → plan → build → test → review → ship.
   HITL after **every** stage (explicit user decision).
2. **Shared artifact editing.** PRD/design/plan are living documents. Claude
   writes v1; a founder rewrites the scope section in ClickUp on their phone;
   the engine must build from the *edited* version. Both sides edit; neither
   side's edits may be lost — and the substrate (ClickUp) must be swappable.
3. **Cold-start context.** Re-discovering what Gumo is on every run burns
   tokens and multiplies failure. The engine needs **product memory**:
   persistent, versioned, reviewed, warming every run.

**Multi-repo reality (v1 rule).** One product usually spans several repos
(Gumo, the default context: three). One pipeline = one repo. A cross-repo feature is split by the human into ordered
pipelines (server first, clients after), linked via `related_jobs` and a
shared parent ClickUp task; a client pipeline's P0–P3 receive the sibling
server pipeline's PRD + design and target its PR's API contract, not `<base>`.
Atomic cross-repo pipelines are deferred.

## 1. Job kinds

| kind      | trigger                          | flow                               |
|-----------|----------------------------------|------------------------------------|
| `sentry`  | webhook / sweep / manual         | grade → fix (HITL only if COMPLEX) |
| `task`    | dashboard (title or ClickUp URL) | analyse → gate → implement         |
| `feature` | dashboard (title or ClickUp URL) | **P0–P9 pipeline, gate after every stage** |
| `memory`  | dashboard, per project           | bootstrap `.gumo/memory/` → draft PR |

## 2. The stage ladder (P0–P9)

One headless Claude run per stage, on the feature branch `brain/feat-<job>`.
P0–P4 are **document stages** (artifact only, produced in the run output and
written/committed by the ENGINE — the run gets read-only tools). P5–P8 are
**code stages** (full toolset). Every stage ends parked at a gate.

| stage | name       | artifact         | contract (summary) |
|-------|------------|------------------|--------------------|
| P0 | Intake      | `P0-intake.md`  | Restate request, ambiguities as questions, draft acceptance criteria. Memory only — read-only tools, instructed to stay within `.gumo/**`; if memory is absent, runs in **declared degraded mode** (may read code, must say so in the artifact header). |
| P1 | PRD         | `P1-prd.md`     | User stories, scope IN/OUT, numbered acceptance criteria, non-goals. Same tool scoping as P0. |
| P2 | Recon       | `P2-recon.md`   | Read the code. Current behaviour, touched modules, constraints, risks. No solutioneering. |
| P3 | Design      | `P3-design.md`  | Technical design. Data-model decisions explicit, each with options + trade-offs + recommendation. |
| P4 | Plan        | `P4-plan.md`    | **Build groups**: `## Build group 1..N`, each independently committable + testable, with ordering rationale; file-level steps; test plan mapping every P1 acceptance criterion to a planned test. |
| P5 | Build 1     | code + `P5-build.md` | Execute build group 1 verbatim. Commit per step. **Opens the draft PR at stage end** (starts the sentry[bot] review loop early). |
| P6 | Build 2..N  | code + `P6-build.md` | Execute remaining build groups. Single-group plans auto-skip P6 (recorded, no gate). |
| P7 | Test        | tests + `P7-tests.md` | Tests per plan; run suite; results table mapping each P1 acceptance criterion → named test or explicit NOT-TESTED. |
| P8 | Review      | fixes + `P8-review.md` | Self-review `git diff <base>...HEAD` against P1 acceptance criteria + conventions (build narratives deliberately excluded from context); fix; re-run tests. |
| P9 | Ship        | `P9-ship.md`    | Memory distillation (changelog entry, ADRs from gate decisions, touched architecture/map sections), finalize PR body, "ready to un-draft" summary. |

### Gate protocol

A stage run must end with one of (LAST line-start occurrence wins; everything
after it is the payload):

- `STAGE_DONE:` + the artifact/summary markdown, ending with `## Questions`
  (≥1; minimum "1. Approve and continue to P<n+1>?").
- `STAGE_FAIL:` + why (blocked, missing info).

A standalone `PR_URL: <url>` line is honored *in addition* (P5, P9) — a bare
URL elsewhere in output never changes state. Anything unparseable **fails
closed**: the job parks with the raw output flagged unparsed; nothing
auto-advances. `extract_questions` takes the LAST questions heading.

Gate verbs — dashboard buttons or ClickUp comments (parent task **or any
artifact subtask**; the poller scans both):

- `/proceed <guidance>` → guidance recorded, next stage.
- `/redo <notes>` → same stage re-runs with notes as mandatory corrections.
  `/redo P<k> <notes>` re-targets any earlier stage k.
- `/skip` → pipeline aborted (branch left intact).

**Redo semantics for code stages (P5+):** the engine records
`stage_base_sha` (branch HEAD before each stage run). Redo preserves the
current head as `refs/gumo/<job>/P<n>-attempt-<k>`, hard-resets the branch to
`stage_base_sha`, and re-runs. `stage_attempts` soft-caps at 3 (warns on the
gate, never blocks). Redo of an earlier stage stamps downstream artifact
mirrors with a SUPERSEDED banner.

**Single-writer gates.** Both channels funnel into one resolution path whose
advance is an atomic compare-and-set (`UPDATE jobs … WHERE status =
'awaiting_input' AND stage = ?`); the loser gets "already answered via
<channel>" (HTTP 409). The worker re-validates status at dequeue and discards
stale entries. Every decision (stage, action, verbatim text, channel,
timestamp, artifact blob SHA answered against) is appended to `guidance_log`
and mirrored to `.gumo/features/<job>/guidance.md` on the branch at next
stage start.

**Gate evidence (P5–P8).** Gate cards/comments include harness-captured
evidence, never self-reported: `git diff --stat` vs `stage_base_sha`, files
changed, and a GitHub compare link (enabled by per-stage push).

**Gate notifications.** Features have an `owner` (set at submit). At every
gate the engine assigns the owner on the ClickUp task and posts the gate
comment — ClickUp's native push/email does the nudging. (Optional Slack
webhook is the documented follow-up.)

**Gate-park ordering (crash-safe).** The DB transition to `awaiting_input`
(with the current comment marker) commits BEFORE the gate comment is posted;
the poller ignores engine-authored comments (fixed prefix). On restart, parked
jobs re-scan comments after the stored marker, so answers posted during an
outage are honored.

## 3. Shared artifacts — the sync protocol

**Git is the source of truth; ClickUp is the human editing surface; the brain
is the sync layer. Human edits always survive. Fail closed on anything
ambiguous.**

- Artifacts live on the feature branch under `.gumo/features/<job>/`.
  The ENGINE commits and **pushes the branch to origin at the end of every
  stage run** — including timeout/fail/unparsed — so no work exists only in a
  disposable workspace. For stage > 0, `prepare_workspace` checks out
  `origin/<branch>`; if it is missing or lacks the prior artifacts, the job
  parks as error ("feature branch lost") instead of silently rebuilding.
- Each artifact mirrors to a **ClickUp subtask** created in the parent task's
  own home list (stored at adoption; NOT the configured autofix list).
  Mirror failure is never silent: the job carries a visible `mirror off`
  flag and a parent-task comment says to answer via gates instead.

**Edit detection is content-based against git, tolerant of ClickUp's
round-trip mangling** (ClickUp regenerates markdown: escapes, list markers,
blank-line collapse):

- `synced_hash` = hash of the normalized **read-back** after every push
  (ClickUp's fixpoint), never of what was sent.
- On pull, a semantic normalizer (strip escape backslashes, unify bullet
  markers/numbering, collapse blank runs, trim) compares fetched text to the
  git file; only a **semantic** difference is a human edit (committed:
  `artifact: human edit P1-prd (via ClickUp)`); byte-only drift silently
  refreshes `synced_hash`. Pull is idempotent under replay.
- **Push is compare-and-set**: GET+hash immediately before each PUT; if a
  human edited during the run, commit their version first, SKIP the
  overwrite, and warn in the gate summary ("You edited P4-plan while this
  stage ran; the stage did NOT see it — /redo if it matters"). All artifact
  hashes are re-checked at gate-park time so mid-run edits surface in the
  gate being answered.
- **Degradation, fail-closed:** an empty/404/shorter-than-pushed pull NEVER
  becomes synced state or overwrites git. 404 → recreate the subtask from
  git, note it. Empty description → gate question ("deliberate wipe, or
  restore from git?"). Post-push read-back shorter than sent → artifact
  marked `truncated`, subtask rewritten as a pointer to the git path,
  excluded from edit pulls. ClickUp down → git + dashboard keep working.

Ordering (durability): pull = write file → git commit → push origin → update
hash. Stage end = push origin → ClickUp push → update hashes. The SQLite
update is always last.

## 4. Product memory

**Markdown in git, updated through the same PRs the engine ships, split into
two scopes** (per-repo product.md triplication would drift — critic-confirmed):

- **Repo scope** — `.gumo/memory/` in each repo: `architecture.md`, `map.md`,
  `conventions.md`, plus `decisions/` and `changelog/` **directories** (one
  file per entry, `<date>-<slug>.md` — append-only files guarantee merge
  conflicts; per-entry files can't conflict, and "recent N" is trivial).
- **Product scope** — `.gumo/product/` in the configured canonical repo (§10):
  `product.md`, product-level decisions/changelog, `contract.md` (endpoints/
  models the clients depend on). Client-repo runs get it inlined from the
  canonical repo's base branch (via the brain's canonical workspace —
  base-pinned, always fetched fresh). Client repos carry no product.md.

**Lifecycle:**
- **Bootstrap** (`memory` job): TWO sequential runs (map+architecture first;
  product+conventions second, reading the first pair), per-file size caps,
  every non-obvious claim carries a path citation, PR body ends with
  `## Questions` listing the ~10 least-certain claims. `decisions/` and
  `changelog/` are seeded EMPTY (format header only) — bootstrap never
  fabricates history from git archaeology.
- **Read**: context matrix below; memory files are in the clone, so prompts
  inline capped excerpts + full paths (pointers over floods). Prompt assembly
  always reads from the stage's own clone, never the cache.
- **Write**: P9 distills; **sentry and task implement prompts also append a
  changelog entry and update touched map/architecture sections** (bulk
  traffic must feed memory or it decays).
- **Cache** (`data_dir/memory/<project>/`): origin/<base> versions only, with
  `{commit_sha, fetched_at}`, dashboard-only (plus the base-pinned product
  scope inline for client-repo runs). Never last-writer-wins from branches.
- **Freshness metric**: commits on origin/<base> since the last commit
  touching `.gumo/`, shown per repo on the Product brain panel.

**Context assembly (binding inputs, not recency):**

| stage | inlined |
|-------|---------|
| P0–P1 | product.md, changelog tail |
| P2–P3 | + architecture.md, map.md, decisions digest (last ~10 titles + why) |
| P4    | + conventions.md, P1 + P3 in full |
| P5/P6 | P4 **in full** + P3 capped + conventions.md |
| P7    | P4 test-plan section + P1 acceptance criteria + one-para caps of P5/P6 |
| P8    | P1 acceptance criteria + `git diff <base>...HEAD` + conventions.md (NO build narratives) |
| P9    | P1 + P3 + changelog tail + guidance stubs (it maintains memory) |

Caps: product 6k, architecture 8k, conventions 6k, changelog tail 3k, ADR
digest 2k chars. Guidance rendering: verbatim for current + previous gate,
older entries as one-line stage-tagged summaries (~800 chars each max).
Precedence rule stated in every prompt: current artifact content > newer
guidance > older guidance; superseded guidance is marked.

## 5. State machine & storage

- `jobs` gains: `stage`, `stage_attempts`, `mirror_ok`, `cu_list_id`,
  `owner`, `related_jobs`, `run_started_at`.
- Child tables (INSERT-only or row-per-key — no JSON blob read-modify-write):
  - `guidance_log(job_id, stage, action, text, via, artifact_sha, at)`
  - `artifact_state(job_id, artifact, subtask_id, synced_hash, flags)`
  - `stage_state(job_id, stage, base_sha, attempts)`
  - `stage_runs(job_id, stage, attempt, queued_at, started_at, ended_at,
    gate_posted_at, gate_answered_at, gate_action, cost_usd, num_turns,
    duration_ms, result_status)` — populated from the `claude -p` JSON
    envelope (v1 discarded it). This is the 10x receipt: tokens + engine-
    minutes per shipped feature, gate-wait vs run-time, redo rate per stage.
- **SQLite is the queue of record.** Startup re-enqueues all
  `received|queued` jobs; the asyncio queue is a wakeup signal; dequeue
  re-validates status. Priority classes: live sentry (webhook/manual) ≥
  answered feature stages/tasks > sweep.
- **Reaper**: `run_started_at` set atomically with `running`; reaped at
  startup AND periodically using the per-stage timeout + grace. `run_claude`
  kills the subprocess on CancelledError as well as timeout.
- `db.insert` ON CONFLICT never resets `stage`/`artifact_state`/
  `guidance_log` for feature jobs. Error/timeout recovery: `redo` is valid
  from `error|timeout|awaiting_input`; dashboard shows a re-kick control.
- Per-stage timeouts: P0–P4 ≈ 900s; P5–P8 full `claude_timeout_seconds`.

## 6. API & dashboard

- `GET /api/context`, `PUT /api/context`, `DELETE /api/context` — the editable
  project context (§10): repo map, canonical project, product name, business
  context. GET returns `{context, defaults, overridden}`; PUT validates
  atomically (400 changes nothing) and applies live; DELETE reverts to the
  env/code defaults.
- `POST /api/features` — `{project, clickup? | title+summary, owner?,
  related_to?}`.
- `POST /api/jobs/{id}/answer` — `{action: proceed|redo|skip, answer}`,
  CAS-guarded, 409 on lost race.
- `GET /api/jobs` — feature rows carry `stage`, `stage_name`, `mirror_ok`.
- `GET /api/features/{id}/stats` — stage_runs rows.
- `GET /api/memory/{project}`, `POST /api/memory/{project}/bootstrap`.
- `GET /api/jobs/{id}/session` — live-session snapshot: meta, stage timeline,
  gate decisions, current artifacts, live/steerable flags.
- `GET /api/jobs/{id}/session/stream` — SSE of the running stage's activity
  (`status` tool calls, `delta` text, `done`); its own broker so a gate chat
  never clobbers it.
- `POST /api/jobs/{id}/session/steer` — `{note}`. Mid-run course-correction.
- Dashboard: an **inbox + split view**. The left pane lists every job (newest
  activity first; All / Active / Awaiting-you filters; mini stage strip on
  features). Selecting a row (`#/job/<id>`) opens the right pane — that item's
  whole world: the per-stage work thread (each stage collapsible with
  attempts/duration/cost + its artifact; the current stage streams live tool
  calls/text over SSE), the full conversation with the engine, the gate packet
  when parked (question / evidence / analysis + Proceed / Redo / Skip with a
  guidance box), and a composer with an explicit **Ask / Steer** toggle. Ask
  works mid-run AND at gates (fast lane answers from persisted artifacts; a
  code-run escalation queues on the repo lock, persist-then-poll). **Typed
  input survives re-renders** (stable composer DOM + localStorage drafts).
  With nothing selected the right pane is the intake view (Sentry fix /
  request / feature pipeline + the Product brain table). Error/timeout re-kick
  lives in the gate packet.
- **Every item kind is conversational and observable.** Sentry fixes and
  requests get the same detail pane: their runs stream live over the same
  broker (`run_claude` gains `on_event`), and Ask works for them too — the
  fast lane primes from the item's record (request / analysis / question /
  evidence) instead of stage artifacts; the slow lane is a read-only run on a
  fresh checkout of the base branch. Steering stays feature-only (v1 runs
  have no resumable session machinery); the gate answer box is their
  correction channel.

### ClickUp intake (tickets as the front door)

The autofix list doubles as an intake channel — the ClickUp mirror of the
dashboard forms, for anyone who lives in ClickUp (and for driving the engine
without dashboard credentials). The poller (`_poll_intake`, every
`clickup_poll_seconds`, gated by `clickup_intake_enabled`) adopts any
human-created top-level task whose name starts with a directive:

- `[fix] <title>` / `[bug]` / `[task]` — the 2-phase request flow; the task
  description needs a `project: <slug>` line, the rest becomes the request.
- `[feature] <title>` — the P0–P9 pipeline, same description format.
- `[sentry <issue id>] <title>` — a forced sentry run; the run adopts this
  ticket instead of creating its own.

Engine-created tickets never match (their names start `[<project>] …`).
Idempotent by construction: every adopted ticket's job row points back at it
(`job_for_clickup_task`), and a rejected one (unmapped project, missing issue
id, duplicate) is pinned by a `cu-<task id>` skipped row carrying the reason —
one explanatory comment, never a re-scan loop. Everything after adoption is
the ordinary flow: status sync, gate comments, `/proceed`-family verbs.

### ClickUp field sync (the gumo-speed conveyor mirror)

The original people-driven workflow (the `gumo-speed` repo) tracks a feature
as ONE ticket whose `Stage` dropdown is the board, with per-repo PR url
fields, a `Decisions` log and doc links — and ClickUp automations key off
those fields. The engine mirrors its state onto that contract (best-effort
display only, gated by `clickup_field_sync_enabled`; the engine's store stays
the record, §7): each feature stage sets `Stage` via
`clickup_stage_field_map` (P0→Brief … P9→Launch; the build stages resolve
per-repo via `clickup_repo_stage_map`; terminal `pr_opened`→Dogfood; the
shepherd sets Complete on merge), `record_prs` fills `Backend PR`/`Web PR`/
`App PR` via `clickup_pr_field_map`, substantive gate answers append to
`Decisions` (read-then-append, never overwrite), and `Dashboard` deep-links
to the job's inbox view. Fields are addressed by NAME and resolved at
startup (`load_fields`); a missing field or option is a quiet no-op — the
workspace schema belongs to the humans.

Full-parity extras: `PRD Doc`/`Contract Doc` point at the artifact-mirror
subtasks (the brain's EDITABLE equivalent of the workflow's Drive docs —
edits there sync back to git) and the folder field at the branch's `.gumo`
tree; feature adoption reads the `Assigned Dev DRI`/`Assigned Founder DRI`
people fields into `owner` (gates assign that person, as the original
automations did); stage runs may emit `FRICTION: <what> · <improvement>`
lines that append to `Gumo Workflow Improvements` (human redos append there
too — the `gumo-improve` harvest loop reads this field); P9 may emit
`FLAG_NAME:`/`SUCCESS_METRIC:` lines that fill the launch fields. Literal
Google Docs remain out (the brain has no Drive credentials; the subtask
mirrors supersede them functionally).

### Live steering (mid-run course-correction)

A human can interrupt a *running* stage from the session view. With session
persistence on, the engine trips a per-job interrupt event; `run_claude_stream`
stops the CLI and returns `interrupted` with the (engine-owned) session id. The
engine checkpoints the work-in-progress to origin and arms a resume of that same
session — reusing the STAGE_ASK machinery, keyed `gate_kind='steer'`, with the
note as `resume_answer` — then re-enqueues. The resumed run continues where it
stopped, keeping the work that still applies. A steer does **not** consume the
STAGE_ASK ask budget. With persistence off (no resume), or when the job is not
running, the note is recorded as guidance for the next checkpoint (the safe
fallback). Fail-closed: if the checkpoint push or session id is missing, the
steer degrades to a fresh re-run carrying the note as guidance — never lost.

### PR lifecycle

Every PR a run mentions (`PR_URL:` lines — a packet can open several: P5 +
per-build-group in P6) is tracked in the `prs` table, idempotently by URL.
On first capture the engine runs the lifecycle kickoff (best-effort, gated by
`pr_auto_ready`): flip the draft to ready-for-review (GraphQL
`markPullRequestReadyForReview`) and post the first `@sentry review` — the
review bot ignores plain pushes, so every round needs an explicit trigger.
States: `draft → ready → in_review → changes_requested → approved →
merged/closed` (plus `stalled` when the round cap hands off to a human); the
detail pane lists each PR with its state, round count and latest shepherd
note. Memory-bootstrap PRs are tracked but never kicked off (doc drafts).

**The shepherd** (`shepherd_forever`, every `shepherd_interval_seconds`)
autonomously drives each tracked `ready`/`in_review`/`changes_requested` PR:
merged/closed detection first; a 🎉 on the engine's latest `@sentry review`
trigger = clean pass → `approved` + a ClickUp note on the owning job. Open
findings (`BUG_PREDICTION` comments the bot has not edited to `*Resolved in*`)
that lack an engine reply get one verify-first headless fix run on the PR
branch (the run must commit AND push before reporting `FINDING <id>: FIXED`;
non-issues are `REBUT`ted, never touched); the engine replies on each thread,
then posts the next `@sentry review` — one explicit trigger per round, since
pushes and replies alone never re-engage the bot. `pr_max_review_rounds`
(default 6) stops runaway loops: the PR goes `stalled` with a needs-a-human
note. Finding bodies are reviewer-supplied input — the fix prompt treats
instructions inside them as data.

## 7. Guardrails

- One branch per feature; serial worker; PRs open as drafts (the lifecycle
  kickoff may mark them ready — never merges); fail-closed gates.
- Features are human-initiated → exempt from grading/daily cap; every stage
  run is recorded in `stage_runs` so cost is visible instead.
- Artifact content pulled from ClickUp is delimited as human-authored INPUT;
  quoted logs/user content inside requests is data, not instructions.
- ClickUp best-effort for *visibility*; never for progress (git + dashboard
  are sufficient to drive a feature end to end).

## 8. Why this is 10x (and how we'll know)

1. **Warm starts** — memory turns "understand the product" into a 2-minute
   read. 2. **Gates as answers, not meetings** — 30-second dashboard answers,
   ClickUp-native nudges. 3. **Knowledge compounds** — every PR feeds
   changelog/ADRs; feature #50 starts smarter than #1.
   
The claim is measured, not asserted: `stage_runs` records tokens, cost,
wall-clock, gate latency, and redo rate per stage. Week-one review reads the
receipts, then tunes the gate cadence (`stage_gates` config exists; default
all-gated).

## 9. Deliberate deferrals

- Auto-advance gate tiering (P5→P6, P7→P8 candidates) — after week-one data.
- Per-job worktrees + bounded concurrency — after latency data.
- Scheduled memory-refresh job kind — freshness metric first.
- Unmerged-PR memory salvage (abandoned-approach ADRs).
- Atomic cross-repo pipelines.
- ClickUp round-trip integration test against the live workspace (runs at
  deploy time; normalizer is built to its findings).

## 10. Project context — the engine is product-agnostic

Everything the engine knows about WHAT it is working on is **configuration,
not code** — the same brain serves any software team:

- **Repo map** — project slug → `{repo, base, setup_cmd?, test_cmd?, allow?}`.
  Any number of repos; drives intake validation, workspace clones, PR bases,
  test instructions and the shepherd's reverse lookup.
- **Canonical project** — which slug hosts product-scope memory
  (`.gumo/product/`). Must be a slug in the repo map (fail-closed on save).
- **Product name** — the identity used in prompts ("the `<name>` Engine",
  "the `<name>` platform").
- **Business context** — operator-maintained free text injected into EVERY
  run's prompt (all P0–P9 stages, sentry fixes, tasks, memory bootstrap, the
  PR shepherd), capped at 4k chars. It is the always-present baseline; product
  memory, when present, is more current and takes precedence — the injected
  block itself states this, so it holds for custom contexts too.

Precedence: **DB overrides > env vars > code defaults** (the Gumo repos and a
structural Gumo description ship as the defaults). Operators edit the context
in the dashboard's "Project context" panel or via `PUT /api/context`; saved
values persist in the `app_config` table, apply to the live engine immediately
(next run picks them up) and survive restarts. `DELETE /api/context` reverts
to defaults. Validation is atomic and fail-closed: a malformed repo map or a
canonical slug missing from the map is a 400 and nothing changes; persistence
is one transaction. The `.gumo/` directory names are engine namespace (like
`.github/`), not product branding — they stay fixed regardless of context.

Edits and running work: jobs keep their recorded project slug. If the new map
still contains the slug, they continue under the new mapping; if a slug was
REMOVED, those jobs are skipped at their next dispatch ("no repo mapped") —
the PUT/DELETE response carries a warning listing affected live jobs, and the
dashboard surfaces it. A run already in flight when an edit lands resolves its
context non-transactionally (it may see mixed old/new values in its briefing);
the next run is consistent.

## 11. Users, roles & sessions

Accounts live in the `users` table (argon2 hashes; never plaintext). Two
roles: **admin** (edits configuration — project context, users; Phase 2 adds
workspaces + integrations) and **member** (does the work: submit, gates,
chat, steer). Two ways in, same accounts:

- **Browser**: `/login` page → `POST /api/login` sets an HttpOnly cookie;
  the 256-bit token is stored HASHED in `auth_sessions`. Unauthenticated
  browser hits on `/` redirect to the login page.
- **Automation**: per-user HTTP Basic on any API route. An explicit
  Authorization header takes precedence over cookies and fails hard when
  wrong (no silent fallback to whatever browser session is around).

First boot with an empty users table bootstraps an admin from
`CTRLLOOP_ADMIN_USER`/`CTRLLOOP_ADMIN_PASSWORD`; if only the legacy
`DASHBOARD_PASSWORD` is set, the admin is created as user `gumo` with that
password (existing deployments upgrade with unchanged credentials).
Consecutive login failures lock the account temporarily. Admins create users
with temporary passwords (forced change at first sign-in), reset, disable
(revokes live sessions), and change roles; nobody can demote or disable
themselves. Password changes revoke all of the user's sessions.

**Attribution**: every gate decision, steer, and chat turn records the acting
user (`guidance_log.via = "dashboard:<username>"`, `gate_chat.author`), and
ClickUp gate comments carry it — "who approved P3" is always answerable.

The UI is served from `app/static/` (index behind auth with the product-name
substitution, login page + assets public; all paths relative so reverse-proxy
prefixes work). The engine comment prefix is `**[ctrlloop]**`; the poller
also recognizes the legacy `**[gumo_brain]**` prefix (§2 gate-park ordering).
