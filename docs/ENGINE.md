# CtrlLoop — feature pipeline, shared artifacts, product memory

> Formerly "the Gumo Engine" / gumo_brain. The engine identity (CtrlLoop) is
> distinct from the configured product identity (§10); all code defaults are
> neutral — Gumo is one customer whose values live in its instance's config
> (worked example in OPERATIONS.md).

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
| `memory`  | dashboard, per project           | bootstrap `<ns>/memory/` → draft PR |
| `watch`   | spawned on a terminal feature's PR merge | metric reads → verdict → **Iterate gate** (§2b) — never a Claude run |

## 2. The stage ladder (P0–P9)

One headless Claude run per stage, on the feature branch
`<branch_prefix>/feat-<job>` (BRANCH_PREFIX, default `ctrlloop`; each job
records its branch at first use, so in-flight pipelines survive prefix
changes — pre-rename jobs are backfilled with their historical `brain/…`
branches).
P0–P4 are **document stages** (artifact only, produced in the run output and
written/committed by the ENGINE — the run gets read-only tools). P5–P8 are
**code stages** (full toolset). Every stage ends parked at a gate.

| stage | name       | artifact         | contract (summary) |
|-------|------------|------------------|--------------------|
| P0 | Intake      | `P0-intake.md`  | Restate request, ambiguities as questions, draft acceptance criteria. Memory only — read-only tools, instructed to stay within `<ns>/**`; if memory is absent, runs in **declared degraded mode** (may read code, must say so in the artifact header). |
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

**P0/P1 success-metric requirement (Epic B1, fail-closed).** The P0 and P1
artifact contracts demand a `## Success metric` section (restating the goal
set at intake — metric, target, window — and HOW it will be measured). A
P0/P1 `STAGE_DONE` whose payload lacks that heading does NOT close `done`:
the run closes `missing_metric` and parks flagged, after the artifact commit
and checkpoint push (the payload is on the branch and the gate), before any
light-mode auto-advance. `/redo` regenerates; a `/proceed` on the flagged
park is a **deliberate human override** of the metric requirement — the
pipeline continues without a stated metric and the outcome watch will be
skipped at merge unless a metric lands later (P9 protocol lines).

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
current head as `refs/ctrlloop/<job>/P<n>-attempt-<k>` (write-only archival
refs — no read path, so no legacy fallback), hard-resets the branch to
`stage_base_sha`, and re-runs. `stage_attempts` soft-caps at 3 (warns on the
gate, never blocks). Redo of an earlier stage stamps downstream artifact
mirrors with a SUPERSEDED banner.

**Single-writer gates.** Both channels funnel into one resolution path whose
advance is an atomic compare-and-set (`UPDATE jobs … WHERE status =
'awaiting_input' AND stage = ?`); the loser gets "already answered via
<channel>" (HTTP 409). The worker re-validates status at dequeue and discards
stale entries. Every decision (stage, action, verbatim text, channel,
timestamp, artifact blob SHA answered against) is appended to `guidance_log`
and mirrored to `<ns>/features/<job>/guidance.md` on the branch at next
stage start.

**Gate evidence (P5–P8).** Gate cards/comments include harness-captured
evidence, never self-reported: `git diff --stat` vs `stage_base_sha`, files
changed, and a GitHub compare link (enabled by per-stage push).

**Gate ownership & notifications (Epic A).** Features carry **two DRIs** —
`founder_dri` + `dev_dri` (ClickUp person id or CtrlLoop username; set at
submit, or read from the workspace's people fields at ClickUp adoption via
`clickup_dri_field_map`; an EMPTY slot may be filled at intake from the
people profiles — Epic D1, §16 — explicit submissions always win). Every stage has an **owning role** resolved from
the stage→role map (default ladder: P0/P1 → founder, P2–P8 → dev, P9 →
founder; overridable per workspace via `stage_role_map`, instance-wide via
`STAGE_ROLE_MAP`). The gate owner = the owning role's DRI, falling back to
the other DRI when the slot is empty. At every park the engine assigns the
owner on the ClickUp task, names them in the gate comment ("Owned by <name>
(<role> gate)") and in the Slack nudge — ClickUp's native push/email does
the nudging.

**Role-exclusive enforcement** happens at the single answer choke point
(both channels), before the CAS: a `proceed`/`redo`/`skip` — ask-gates and
redo-from-error included — from anyone who is not the owner is refused
(dashboard: HTTP 403 with "this is a <role> gate, owned by <name>"; ClickUp:
one explanatory reply per comment, deduped through `gate_events`).
Non-owners keep gate chat, steering visibility and plain comments. The only
bypass is an **explicit, audited admin override** from the dashboard (a
checkbox; recorded in `gate_events` only after the transition actually won
its CAS) — ClickUp offers no override path. Enforcement keys EXCLUSIVELY on
the explicit DRI columns: a job with neither DRI recorded (solo installs;
pre-upgrade jobs whose legacy `owner` column is set) is **inert** — anyone
may answer, exactly as before Epic A; the legacy `owner` still drives
assignment/display. NOTE the fail-closed corollary: with
`require_attributed_answers=off` but DRIs recorded, an *unmapped* ClickUp
commenter can never be the owner — their verbs are refused with the
ownership reply ("link your ClickUp account or answer on the dashboard");
A3 effectively implies A1 over the ClickUp channel.

**Gate SLA & escalation.** Effective SLA = workspace `gate_sla_hours` (NULL
= inherit `GATE_SLA_HOURS`, default 24; 0 disables). A sweep (every
`SLA_CHECK_INTERVAL_SECONDS`) escalates overdue gates of DRI'd features:
≥1.0×SLA re-assigns + nudges the owner; ≥1.5×SLA notifies the OTHER DRI —
visibility, never authority; ≥2.0×SLA records a standup flag. Each step is
keyed on the gated run's `stage_runs.id` in `gate_events` (fires once per
gate attempt; a `/redo` re-park re-arms the ladder; events are written
BEFORE any send, so a crash under-notifies, never double-fires). Jobs
without explicit DRIs and v1 items never escalate (the inbox `overdue` flag
covers them). Everything here is visibility only — no job state changes.

**Per-person queues.** `GET /api/inbox` is the "Awaiting you" surface:
gates the authed user owns + unassigned gates (plus feature error/timeout
parks, which are answerable), membership-scoped, sorted overdue-first then
oldest-gate-first, with `counts.mine` as the badge number.

**Auto-advance resolution (Epic C, §15).** A clean `STAGE_DONE` resolves its
gate through one order — **workspace pin > per-job `gate_mode='light'` >
computed autonomy level (opt-in) > default full gating** — and the safety
guards apply unconditionally on top under every mode. See §15 for the trust
ladder; light mode's own contract (auto-advance only at P2/P4–P8, boilerplate
question, all guards) is unchanged.

**Gate-park ordering (crash-safe).** The DB transition to `awaiting_input`
(with the current comment marker) commits BEFORE the gate comment is posted;
the poller ignores engine-authored comments (fixed prefix). On restart, parked
jobs re-scan comments after the stored marker, so answers posted during an
outage are honored.

## 2b. The outcome loop (Epic B)

Close Review + Accountability: a metric goal goes in at intake, a **measured
verdict** comes out after ship. The whole loop is HTTP + SQLite — a watch job
never invokes Claude (a fail-closed branch in the worker returns any queued
watch to the loop untouched).

**Metric at intake (B1).** `POST /api/features` accepts `success_metric`,
`metric_target`, `metric_window_days` (validated 1–365, atomically — a bad
window is a 400 with nothing queued). ClickUp `[feature]` adoption reads
`metric:` / `target:` / `window:` description lines (bold-tolerant, stripped
from the request like the `project:` line) with the ticket's `Success metric`
custom field as fallback. Stage prompts restate the goal in every header.

**Harvest (B1/B2).** Runs may emit `SUCCESS_METRIC:` / `METRIC_TARGET:` /
`METRIC_WINDOW_DAYS:` / `METRIC_EVENT:` protocol lines (P9's contract names
them; P8 verifies the instrumentation exists in the diff). They land on the
job row — NOT gated by the ClickUp field sync (the row is the record, the
mirror is visibility). **Human intake wins**: the first three fill only when
empty; `metric_event` is engine-owned and always takes the latest emission.

**Analytics adapter (B3, seam H4).** `app/analytics.py`: an
`AnalyticsProvider` interface (`query_metric(metric, window_days, event, end)
-> {status, series, total, detail}`), a Mixpanel driver (per-workspace
credentials: `analytics_provider` + `analytics_config` — the config is a
SECRET AT REST, stored on the workspace row and never returned by any API;
the dashboard sees only `analytics_configured`), and a Null driver for
instances without analytics. Resolution: workspace row > instance env
(`ANALYTICS_PROVIDER`/`ANALYTICS_CONFIG`) > null. Unknown provider names and
malformed config fail closed to the null driver (verdicts become
`unmeasured`, never a guess); provider errors (401s etc.) surface as visible
detail on the watch job.

**Post-ship watcher (B4).** When a tracked PR merges AND the feature is
terminal at `pr_opened`, the shepherd spawns `watch-<feature id>` — a single-
transaction insert born `kind='watch'`, `status='watching'` (invisible to the
queue/reaper/requeue by construction), copying the metric fields, workspace,
ticket, and BOTH DRIs (the founder DRI as owner). Merge detection covers the
mainline flow: the shepherd's scan includes `approved`, `stalled` and `draft`
PRs for merge/close detection ONLY (the review loop is never resumed for
them) — an approved PR the human merges on GitHub still flips to `merged`
and spawns the watch. A PR merged mid-pipeline spawns nothing;
the P9-approval path re-checks and spawns then. A feature with no metric and
no event gets ONE "watch skipped" ticket note (deduped per feature via
`gate_events`). Spawn does not flip the ticket status (the Stage field just
went Complete); status changes happen only at park/close.

The watch loop (every `WATCH_INTERVAL_SECONDS`) reads the metric ~daily
(throttled to one read per ~22h per job) into the INSERT-only
`metric_readings` table, each row stamped with its window's
`watch_started_at` so a `/redo` never mixes windows. At `watch_deadline`
(persisted at spawn — restarts never re-derive it) the finisher takes a final
read, queries a same-length pre-ship baseline (persisted on first finish —
a redo re-finish reuses the ORIGINAL baseline; when none is persisted, e.g.
the first finish's query errored, the re-query ends at the ORIGINAL
merge-time spawn — the watch row's `created_at` — never at the current
window's `/redo`-refreshed start, which would be post-ship data), computes
the verdict via the
transparent formula in `app/outcome.py` (direction-aware: decrease-goal
targets like "under 300ms" judge inverted; ambiguous directions never assert
a regression; no data → `unmeasured`, fail closed), UPSERTs the `outcomes`
ledger row (verdict fields only — it can never clobber a recorded learning),
and parks a founder-owned **Iterate gate**: single CAS
`watching → awaiting_input` first, ClickUp comment/status/assignee strictly
after (best-effort). The Iterate gate is **role-enforced exactly like feature
gates** (§2): the founder DRI owns it (the dev DRI is the fallback owner when
the founder slot is empty — both DRIs are copied onto the watch row at
spawn), a non-owner's verb is refused on both channels, the only bypass is
the audited dashboard admin override, and a watch without explicit DRIs is
inert (solo mode, exactly as for features). Verbs on the gate:

- `/proceed <learning>` → learning + decider recorded on the ledger row,
  watch closes `done`; with `OUTCOME_MEMORY_PRS=true` a background MECHANICAL
  task (no model run) writes a changelog entry (+ ADR when a learning exists)
  under the repo's engine namespace on a `<prefix>/outcome-<feature>` branch
  and opens a draft PR — git stays truth: memory changes only via a
  human-merged PR; the DB row is the record regardless.
- `/redo <days>` → the watch re-arms with a fresh window (days optional,
  clamped 1–365 — out-of-range is refused with the reason, never silently
  accepted). The ledger row stays; the re-finish overwrites verdict fields.
- `/skip` → closes `skipped` (also valid mid-window to cancel a watch); the
  verdict stands, no learning.

NOTE: a parked Iterate gate sits in `awaiting_input` and therefore counts
toward `runs_today`'s daily-cap denominator exactly like feature gates —
accepted and documented (watch jobs consume no Claude runs themselves).

**Ledger (B5).** `GET /api/outcomes` — membership-scoped rows + the verdict
distribution; the dashboard's "Outcomes" panel renders it, and the job detail
pane shows a verdict card (watch and feature views). Status vocabulary:
`watching` (active watch) and `done` (watch closed with a recorded outcome).

## 3. Shared artifacts — the sync protocol

**Git is the source of truth; ClickUp is the human editing surface; the brain
is the sync layer. Human edits always survive. Fail closed on anything
ambiguous.**

- Artifacts live on the feature branch under `<ns>/features/<job>/`
  (the engine namespace dir, §4/§10).
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
two scopes** (per-repo product.md triplication would drift — critic-confirmed).
Both live under the **engine namespace dir** `<ns>` = `.ctrlloop/` — repos
initialized before the rename keep their legacy `.gumo/` tree working, and
ONE precedence rule governs every resolution helper (working-tree reads,
base-pinned `git show` reads, freshness): **legacy wins when present**, so a
repo is never split-brained across two trees. Migrate with a single
`git mv .gumo .ctrlloop` PR (MIGRATION-CTRLLOOP.md).

- **Repo scope** — `<ns>/memory/` in each repo: `architecture.md`, `map.md`,
  `conventions.md`, plus `decisions/` and `changelog/` **directories** (one
  file per entry, `<date>-<slug>.md` — append-only files guarantee merge
  conflicts; per-entry files can't conflict, and "recent N" is trivial).
- **Product scope** — `<ns>/product/` in the configured canonical repo (§10):
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
  always reads from the stage's own clone, never the cache. On top of the
  capped inlines, an FTS retrieval block serves top-k snippets from memory,
  prior artifacts, decisions and guidance (Epic D4, §16).
- **Write**: P9 distills; **sentry and task implement prompts also append a
  changelog entry and update touched map/architecture sections** (bulk
  traffic must feed memory or it decays).
- **Cache** (`data_dir/memory/<project>/`): origin/<base> versions only, with
  `{commit_sha, fetched_at}`, dashboard-only (plus the base-pinned product
  scope inline for client-repo runs). Never last-writer-wins from branches.
- **Freshness metric**: commits on origin/<base> since the last commit
  touching the engine namespace (both `.ctrlloop/` and `.gumo/` pathspecs are
  passed; whichever exists counts), shown per repo on the Product brain panel.

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
  `owner`, `founder_dri`, `dev_dri` (Epic A2 — `owner` is the legacy
  computed alias, dev first then founder), `related_jobs`, `run_started_at`.
- Child tables (INSERT-only or row-per-key — no JSON blob read-modify-write):
  - `guidance_log(job_id, stage, action, text, via, artifact_sha, at)`
  - `gate_events(job_id, stage, kind, ref, detail, actor, at)` — Epic A's
    append-only audit substrate AND idempotence store
    (`UNIQUE(job_id, kind, ref)`): attribution/role refusals (ref = comment
    id), admin overrides (ref = uuid, recorded only after a won CAS), SLA
    escalation steps (ref = `run<stage_runs.id>-step<k>`). Deliberately NOT
    `guidance_log` — refusals and escalations must never render into stage
    prompts as "human decisions".
  - `admin_events(kind, target, detail, actor, at)` — append-only audit of
    admin/config mutations that grant or move authority: user↔ClickUp
    identity links (`clickup_link` — the mapping decides whose ClickUp
    comments answer role-owned gates) and workspace security config
    (`workspace_config`/`workspace_create` — `stage_role_map`,
    `require_attributed_answers`, …; secret values redacted before write).
    The minimal substrate until Epic E4's full audit_log folds/exports it.
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
- `POST /api/features` — `{project, clickup? | title+summary, founder_dri?,
  dev_dri?, related_to?, success_metric?, metric_target?,
  metric_window_days?}` (`owner` = deprecated alias for `dev_dri`;
  the window validates 1–365 atomically).
- `GET /api/outcomes` — the outcome ledger (§2b): membership-scoped rows
  (feature, metric, target, observed, verdict, learning, decider) + the
  verdict distribution.
- `POST /api/jobs/{id}/answer` — `{action: proceed|redo|skip, answer,
  override?}`, CAS-guarded, 409 on lost race, 403 for a non-owner on a
  role-exclusive gate (`override: true` = the audited admin bypass).
- `GET /api/inbox` — the per-person queue (§2): owned + unassigned gates,
  overdue-first, with `counts` for the badge.
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
edits there sync back to git) and the folder field at the branch's engine
namespace tree; feature adoption reads the `Assigned Dev DRI`/`Assigned Founder DRI`
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
merged/closed detection first — and for `approved`/`stalled`/`draft` rows it
runs merge/close detection ONLY (never resuming the review loop), so a
human-merged approved PR still reaches `merged` and fires the outcome watch
(§2b); a 🎉 on the engine's latest `@sentry review`
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

- ~~Auto-advance gate tiering (P5→P6, P7→P8 candidates) — after week-one
  data.~~ Shipped as graduated autonomy (§15): earned per-cell levels with
  workspace pins, computed from exactly that week-one data, continuously.
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
  (`<ns>/product/`). Must be a slug in the repo map (fail-closed on save);
  empty = no instance-level product scope (repo-scope memory only).
- **Product name** — the identity used in prompts ("the `<name>` Engine",
  "the `<name>` platform").
- **Business context** — operator-maintained free text injected into EVERY
  run's prompt (all P0–P9 stages, sentry fixes, tasks, memory bootstrap, the
  PR shepherd), capped at 4k chars. It is the always-present baseline; product
  memory, when present, is more current and takes precedence — the injected
  block itself states this, so it holds for custom contexts too.

Precedence: **DB overrides > env vars > code defaults** (the code defaults
are NEUTRAL: empty repo map, "your product", empty business context — the
original Gumo values live in docs/OPERATIONS.md as a worked example).
Operators edit the context
in the dashboard's "Project context" panel or via `PUT /api/context`; saved
values persist in the `app_config` table, apply to the live engine immediately
(next run picks them up) and survive restarts. `DELETE /api/context` reverts
to defaults. Validation is atomic and fail-closed: a malformed repo map or a
canonical slug missing from the map is a 400 and nothing changes; persistence
is one transaction. The engine directory names are engine namespace (like
`.github/`), not product branding — they never follow the product context.
The namespace is the constant `.ctrlloop/` with a read+write legacy fallback:
a repo already carrying `.gumo/` keeps using it (legacy wins when both exist,
deterministically), until a `git mv .gumo .ctrlloop` PR migrates it.

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
The ClickUp channel is no longer anonymous (Epic A1): each user may carry a
`clickup_user_id` (admin-linked in Settings → Users; one ClickUp identity
per user, enforced by a partial UNIQUE index), and gate verbs by comment
resolve their commenter — `via` becomes `clickup:<username>` for mapped
commenters and `clickup:<cu-name>#<cu-id>` otherwise, so even permissive
modes record WHO. Strictness is `require_attributed_answers` (workspace
value > instance `REQUIRE_ATTRIBUTED_ANSWERS`): `on` refuses verbs from
unmapped commenters with one explanatory reply per comment (deduped via
`gate_events`, so a refused comment can never wedge a stream), `off` never
refuses, and the default `auto` turns strict as soon as ANY enabled user has
a mapping. Applies to feature AND v1 (sentry/task) verbs alike.

The UI is served from `app/static/` (index behind auth with the product-name
substitution, login page + assets public; all paths relative so reverse-proxy
prefixes work). The engine comment prefix is `**[ctrlloop]**`; the poller
also recognizes the legacy `**[gumo_brain]**` prefix (§2 gate-park ordering).

## 12. Workspaces — Business → Workspace → Repo

A **workspace** is one product surface (App, Dashboard, …) owning: its repo
map, its canonical repo for product-scope memory, its product name, its
workspace context, its optional ClickUp list, and its optional Slack
webhook. **Project slugs are unique across ALL workspaces** (DB index), so
slug-keyed resolution — Sentry webhook routing, dispatch, the shepherd —
stays deterministic, and `settings.repo_for_project` keeps working: the
service rebuilds the merged map into live Settings after every edit.

- **Context hierarchy in prompts**: business context (instance, §10) +
  workspace context + repo memory (`<ns>/`, in the clone). Product name and
  canonical repo resolve per workspace. The instance context API now owns
  ONLY the business layer; repo topology edits there are refused with a
  pointer to the workspace API.
- **Membership**: `workspace_members` — members see and act on only assigned
  workspaces, enforced in the API with 404s (no existence leaks); admins see
  all. Jobs record `workspace_id` at intake.
- **Migration**: first boot wraps the effective §10 context into a `default`
  workspace and adopts existing jobs and users. Invisible upgrade.
- **ClickUp optional per workspace**: tickets are created in the workspace's
  list; a workspace with ClickUp off gets no tickets and degrades to
  dashboard-only (everything already treats ClickUp as visibility, §7).
  Sentry is per-workspace by construction: no mapped slugs → no sentry lane.
- **Slack gate nudges**: at every gate park the workspace's incoming-webhook
  (if set) receives the job, stage, and a dashboard deep link. Best-effort,
  never drives control flow.

## 13. Run transcripts — the replayable activity record

The live session stream (SSE via the stage broker) is ephemeral: subscribe
late or reload and the activity is gone — the dashboard chat showed only the
human↔engine messages, never what the engine actually *did*. Transcripts fix
that: **every stage run and every v1 fix run tees its stream events —
`status` (one line per tool call) and `delta` (assistant text) — through a
write-through JSONL writer** under `data_dir/transcripts/<job>/<key>.jsonl`.

- **Write-through, fail-open**: each line is flushed as written (a crash
  keeps everything up to it); transcript I/O errors disable the writer and
  never break a run. Hard 2MB per-run cap with an explicit `truncated`
  marker line.
- **Keys**: stage runs use `P<stage>-run<stage_runs.id>` and stamp the key on
  the `stage_runs.transcript` column; v1 runs use `v1-p<phase>-<ts>`.
  Keys and job ids are validated against a safe-segment pattern — traversal
  shapes read as absent (404), never as file errors.
- **API**: `GET /api/jobs/{id}/transcripts` (index: key + header + size, also
  inlined in the session snapshot) and `GET /api/jobs/{id}/transcripts/{key}`
  (parsed events). Both scoped through the same 404-membership gate as the
  job itself.
- **UI**: lazy-loading "Activity" accordions — per attempt inside each stage
  card, per run in the v1 detail pane — rendering the same status/delta
  visuals as the live log.
- **Retention**: the daily janitor prunes by file mtime after
  `TRANSCRIPT_TTL_DAYS` (default 30), independent of session persistence.
  Transcripts are replay history, not resume state: no keep-set.

## 14. First-run setup

`GET /api/setup` (admin-only) returns a checklist whose steps auto-detect
from live state: business context / product name changed from the code
defaults, workspace repos changed from the default map (semantic compare
against the normalized default), GitHub token present, any product memory
cached, more than one user. The dashboard shows the wizard card to admins
until `POST /api/setup/dismiss` writes `setup_done` to app_config.
Deployments that already processed jobs are auto-dismissed at boot, and a
context reset (`DELETE /api/context`) preserves the flag — upgrades and
resets never resurrect onboarding. Members are never shown instance
onboarding; an unassigned member instead gets an inbox hint naming the
fix ("ask your admin for workspace access").

## 15. Graduated autonomy — the trust ladder (Epic C)

Trust is **earned from the receipts, per (workspace, repo, stage) cell**,
never asserted. A nightly scorer (`autonomy_forever`, cadence
`AUTONOMY_RECOMPUTE_HOURS`, plus the admin-only
`POST /api/autonomy/recompute`) reads the rolling `AUTONOMY_WINDOW_DAYS`
window of `stage_runs` and derives a level 0–3 per cell, persisted in
`autonomy_scores` **with the exact inputs the formula saw** (`inputs` JSON —
the transparency requirement).

**The formula** (`app/autonomy.py`, weights/thresholds are module constants):

```
score = 0.40 · clean_rate          runs closing 'done' / counted runs
      + 0.30 · (1 − redo_rate)     human redos TARGETING this stage / answered gates
      + 0.15 · latency_factor      clamp01(1 − median(gate answer wait) / 2·SLA)
      + 0.15 · rounds_factor       code stages: clamp01(1 − avg shepherd rounds / cap)

level:  ≥ 0.90 → 3   ≥ 0.75 → 2   ≥ 0.55 → 1   else 0
```

with the fail-closed edges spelled out: open runs (`result_status=''`),
`interrupted` and `skipped_single_group` are excluded from every
denominator; latency uses only runs with BOTH `gate_posted_at` AND
`gate_answered_at` and an empty sample is neutral (factor 1.0); zero
answered gates means `redo_rate = 0` (full credit on that term); doc stages
take a neutral rounds factor. Overrides: a sample under `AUTONOMY_MIN_RUNS`
stays level 0; **level 3 additionally requires a clean streak ≥ the same
minimum AND at least one human-answered gate in the window** — a cell that
only ever auto-advances can never hold full trust on autopilot (only
STAGE_FAIL/guard parks, a human redo, or clawback would otherwise demote
it). A cell whose runs age out of the window decays to level 0 on the next
pass. Redo attribution reads `guidance_log` (which records the TARGET
stage), never `stage_runs.gate_action` — a `/redo P2` answered at the P4
gate penalizes P2, not the innocent parked stage.

**Resolution order at a clean STAGE_DONE** (`autonomy.resolve_gate`, one
function, locked):

1. **Workspace pin** (`autonomy_pins`, per stage): `always_gate` |
   `always_auto` — pins always win in both directions and never expire.
   Admin-set (`PUT /api/workspaces/{id}/autonomy/pins`), audited.
2. **Per-job `gate_mode='light'`** — unchanged, including its
   P2/P4–P8-only restriction. Light mode's "P0/P1/P3/P9 always park"
   promise is now "…unless a workspace `always_auto` pin or a
   level ≥ `AUTONOMY_AUTO_LEVEL` cell fires for that stage (P9 excepted —
   always terminal-gated)".
3. **Computed level ≥ `AUTONOMY_AUTO_LEVEL`** — **opt-in, default OFF**
   (`0` = computed levels never auto-advance; values outside 1..3 disable
   the rule rather than clamping — never toward permissiveness).
4. Default **full gating**.

**Invariants.** The safety guards are unconditional at every level and
under every pin: a conflicted mid-run human edit, `mirror_ok=0`, the first
clean run after an explicit `/redo` of the stage, P5 without a captured PR,
and any non-boilerplate question all force a park; STAGE_FAIL / STAGE_ASK /
unparsed always park. "Level 3 = full auto-advance" means "auto-advance any
clean, guard-passing STAGE_DONE" — never "skip the guards". **P9 never
auto-advances**: its proceed is the terminal transition owned by the worker;
`always_auto` pins on stage 9 are refused at the API and ignored by the
resolver. A job with no stamped `workspace_id` skips pin and level
resolution entirely (fail closed) — only the workspace-independent light
mode path can still fire for it. `AUTONOMY_ENABLED=false` restores pure
legacy behavior (light mode only, pins ignored); it is env-only and read
once — flipping it needs a restart.

**Clawback** (`POST /api/workspaces/{id}/autonomy/clawback`, workspace
members may pull the brake — it only reduces autonomy): drops the cell(s)
to level 0 and stamps `clawback_at`; the scorer counts only runs started
AFTER the stamp, so trust re-earns from zero. The stamp is never cleared —
the dashboard shows the "clawed back" flag only while the cell sits at
level 0. A workspace-wide clawback (project omitted) derives its slug list
inside the DB transaction from every project that ever held a score row
UNION the current repo set, so slugs since removed or moved keep their
stale cells clawable. A recompute pass that started before a clawback
landed can never resurrect the cell (conditional upsert; theoretical under
today's single-process SQLite, load-bearing under Epic F2 Postgres).

**Audit substrate.** Every mutation — `pin_set` / `pin_clear` / `clawback`
/ `level_change` / each `auto_advance` — is an INSERT-only
`autonomy_events` row with the acting principal (`dashboard:<username>` or
`engine`). Epic E4 folds/exports these; nothing here waits for it.
Auto-advances also carry their resolution reason into the guidance entry
and the ClickUp gate comment ("auto-advanced (pin: always_auto)" /
"…(autonomy level 3, 14 clean runs)").

**Surface.** `GET /api/autonomy` — per accessible workspace the stage×repo
matrix (level, score, sample, decoded inputs), the pin map, and the last 50
events; `GET /api/jobs` feature rows carry `autonomy_level`/`autonomy_pin`
for their current stage; the session snapshot carries a per-stage
`autonomy` block powering the trust dial on the stage cards. Dashboard:
the "Autonomy" panel (matrix + pins + clawback + event log), a trust chip
on the feature header, and per-stage L0–L3 dials.

**Recorded edges** (deliberate): a feature re-intake may change
`jobs.project` while keeping old `stage_runs` — historical runs then join
to the NEW project slug (acceptable: same repo lineage in practice). A repo
slug moved to another workspace leaves in-flight jobs resolving pins/levels
against their STAMPED `workspace_id` — the stamp is the record; the new
workspace's pins apply to newly-intaken jobs. The auto-advance transition
itself stays `set_fields`/`set_status` (no CAS) inside the serial worker
while `status='running'` — correct today because `answer_job` requires
`awaiting_input`; re-verify under Epic F2 with multiple workers (BUILD-PLAN:
"All CAS transitions re-verified under Postgres semantics").

## 16. Organizational context (Epic D)

### People & ownership model (D1)

`people` is a **profile layer over `users`** (1:1, admin-edited via
`PATCH /api/users/{u}`), never a parallel identity: person role
(founder/product/dev/design), ownership areas
(`{kind: workspace|repo|area, value}` — `area` entries are free-text
display-only tags), and decision-authority tags. Every profile mutation is
validated fail-closed (a bad payload changes nothing) and audited in
`admin_events` (`people_profile`) only when something actually changed —
areas and authority GRANT routing/decision authority.

Two consumers:

- **Intake DRI defaults** (`PEOPLE_ROUTING_DEFAULTS`, default on, inert
  until profiles exist): at feature intake an EMPTY DRI slot fills from the
  profiles iff EXACTLY ONE enabled user matches — role maps to the slot
  (founder→founder, dev→dev; product/design map to neither), the profile's
  areas cover the job's workspace or repo slug (an empty areas list covers
  nothing), AND the user is a member of the job's workspace (a non-member
  DRI would own gates behind membership 404s — refused). Ambiguity, zero
  matches, or no resolvable workspace all fail to `''`: the gate stays inert
  exactly as before. Explicit submissions (dashboard fields, ClickUp people
  fields) always win, and the fill happens INSIDE the single atomic intake
  upsert. **Interplay with §2**: a resubmit that omits DRIs no longer
  guarantees enforcement turns off once profiles cover the repo —
  `PEOPLE_ROUTING_DEFAULTS=false` is the opt-out (OPERATIONS.md §10).
- **The prompt ownership block**: stage prompts render "Ownership & decision
  authority" — the covering profiles for display, but the per-gate-role
  authority lines come from the JOB's OWN DRI columns (they must never
  contradict `roles.gate_owner`, which stays the ONLY enforcement path —
  §2's "enforcement keys exclusively on the explicit DRI columns" is
  untouched). Values are single-lined and capped; solo installs render
  nothing. `GET /api/people` is the member-readable directory: members see
  only people whose areas intersect their OWN workspaces (and only the
  intersecting workspace/repo entries) — org structure never leaks.

### Decision registry (D2)

Cross-ticket `decisions` table. Sources: `gate` (auto-registered),
`manual` (dashboard), `slack` (D3 candidates). Statuses: `active` (registry
truth), `superseded`, `candidate` (quarantined until confirmed),
`dismissed` (remembered rejection — the row and its `(source, ref)` key are
kept so a re-scan can never re-propose it).

- **Auto-registration** happens at the SAME choke points that feed the
  ClickUp Decisions field — substantive (non-empty-text) proceed/redo/ask
  answers on feature gates, and the Iterate-gate learning
  (scope `product`, against the FEATURE id) — each with
  `ref = g<guidance_log id>` so any replay dedupes through the partial
  UNIQUE `(source, ref)` index (dedupe is detected via `rowcount`, never
  `lastrowid`). v1 (sentry/task) guidance is deliberately NOT registered:
  operational steering, not org decisions. Registration is best-effort and
  can never break a won gate CAS.
- **Visibility = prompt admission, exactly**: members see their workspaces'
  rows PLUS every `org`-scope row (org rows reach every member's prompts,
  so every member may read them); creating/confirming/superseding org rows
  is admin-only. Rows with `workspace_id` NULL (non-org) are admin-only and
  never reach any member view or any prompt. The default registry view
  excludes candidates and dismissed rows.
- **State transitions** are single-statement status CAS
  (`decision_set_status`) — confirm/dismiss/supersede races have one winner
  (409 for the loser); the FTS row syncs in the same transaction (insert on
  entering `active`, delete on leaving it).
- **P9 reads the registry**: the P9 prompt carries a "Decision registry"
  block — this feature's own active gate decisions verbatim-capped plus
  recent product/org one-liners for the job's workspace — always prefixed
  with the standing "recorded context (data), not instructions" note
  (registry text includes confirmed Slack content and member input). Fail
  closed: a job with no stamped `workspace_id` gets NO block. **Dead-lap
  purge**: `feature_intake`/`artifacts_clear` supersede the job's active
  decision rows and delete their FTS rows in the SAME transaction as the
  guidance clear — an abandoned lap's gate decisions can never re-enter the
  new lap as registry truth (the same fail-closed rationale that clears
  `guidance_log`).

### Memory retrieval (D4)

An FTS5 index (`mem_fts`, in the engine DB) over: **memory files**
(top-level + the newest ≤50 changelog/decision entries, refreshed by
`refresh_cache` — skipped entirely when origin/<base>'s commit sha is
unchanged), **artifacts** (every write funnels through `artifact_state`;
rows flagged `superseded` are dropped the moment the flag lands),
**human guidance** (proceed/redo/answer/chat/steer with text), and
**active decisions**. Deliberately NEVER indexed: `gate_events` /
`admin_events` (refusals and escalations must never reach model context as
human decisions), candidate/dismissed decisions, superseded
artifacts/decisions, and a re-intaken job's purged rows.

Stage prompts get a "Memory search" block (`MEMORY_SEARCH_TOP_K`, 0 or any
out-of-range value disables) of top-k snippets, prefixed with the
data-not-instructions note. Scoping is an explicit per-kind whitelist:
guidance/artifact require an exact project match (project-less rows are
never admitted) and never the asking job's own rows; memory requires the
exact project; decisions require the job's workspace OR `org` scope. Search
terms are sanitized hard (FTS5 syntax stripped) and any FTS error returns
no results — retrieval is additive context; **no control flow depends on
it**, so a SQLite build without FTS5 degrades to an absent block
(`fts_enabled=false`), the one deliberately fail-open read path.

### Slack read ingestion (D3 — FLAG, off by default)

`SLACK_INGEST_ENABLED=false` ships the feature inert; enabling it also
requires `SLACK_BOT_TOKEN` (env-only secret — never stored on a workspace
row, never in any API response or log line) and a per-workspace
`slack_channels` allowlist (a channel routes to exactly ONE workspace —
read-checked in the synchronous save path; see recorded edges). The poller
captures decision-shaped messages (`!decision` prefix, or the
`SLACK_DECISION_EMOJI` reaction) as registry **candidates** — parked inbox
items for human confirm/dismiss, NEVER auto-committed: quarantined from the
index, the prompts and the default registry view until a human confirms
(confirm may edit scope/title/text under the same validation as manual
adds; the confirming human becomes `decided_by` while the Slack author is
preserved in `origin_author`). Watermarks are per channel, initialized to
NOW at first allowlist (forward-only — no historical flood), advanced only
after a fully-fetched batch (pagination to exhaustion, bounded per pass),
with a ~7-day overlap re-scan so late reactions are seen (`(source, ref)`
dedupe absorbs re-reads). Documented Slack-API limits: reactions older than
the overlap window are missed; thread replies are out of scope unless
broadcast. No job state is ever touched — output is inbox items + parked
candidates (the Epic I routines invariant, honored now; the loop is
`sla_forever`-shaped so I1 can adopt it as a routine kind).

**Recorded edges (Epic D)**: the `slack_channels` cross-workspace
uniqueness check is a read-check with no DB constraint — sound under
today's single-process SQLite because check and write share one synchronous
section with no await between them; re-verify under Epic F2 multi-worker
Postgres (same treatment as §15's auto-advance non-CAS note). The registry
API's `q` search uses escaped LIKE rather than the FTS index — deliberate:
the index excludes non-active rows, and a status-filtered registry search
must not depend on it.

## 17. Proactive routines & planning cadence (Epic I)

The engine runs the team's operating rhythm and proposes work from signals
it already holds. **The invariant line**: routines emit inbox items and
parked candidates ONLY — never self-initiated pipelines; a routine NEVER
invokes Claude, and the single sanctioned queue interaction is the memory-
upkeep routine's `intake_memory` (a bootstrap that parks a draft PR for
human review like any other — git stays truth).

### The routine engine (I1)

`routines` table: one row per (kind, scope); `routine_runs` is the
INSERT-only history. The scheduler (`app/routines.py`, one asyncio task)
ticks every `ROUTINE_TICK_SECONDS`, resolves each row's schedule
(`every:<seconds>` — floored at 300, never a hot loop — |
`daily@HH:MM[;days=…]` | `weekly@<day> HH:MM`, evaluated in `ROUTINE_TZ`
via zoneinfo), and fires due rows through a **claim CAS on `last_run_at`**
(single-flight per routine, correct today and load-bearing under Epic F2)
plus an in-process in-flight guard. Each due handler runs as its **own
asyncio task**, so a slow handler (the sweep's paginated Sentry HTTP) can
never delay the reaper. Handler failures record `error` in the run history;
the next due tick retries. `POST /api/routines/{id}/run` arms a fire
through the scheduler task — handlers never run inline in an HTTP request.

**Builtin rows** (instance-scoped, `workspace_id NULL`) generalize three of
the hardcoded loops: `sweep`, `reaper`, `janitor`. They store `schedule=''`
meaning *derive from live settings at each tick*
(`SWEEP_INTERVAL_HOURS`, 300s, 86400s) — env contracts keep working after
upgrade; an operator-edited non-empty schedule wins. Their handlers call
the existing `_once` bodies unchanged. Effective-enabled = routine row AND
the legacy settings flags (`sweep_enabled`+`sentry_enabled`) — either off
means off — EXCEPT the **reaper, which is non-disableable** (the PUT
endpoint refuses `enabled=false` and schedule edits; the scheduler ignores
a hand-edited disable). A builtin row whose stored schedule fails to parse
**falls back to its settings-derived default with a logged error** — never
a silently dead reaper. At EVERY boot, `last_run_at` is bumped to now for
the sweep/janitor rows (the settle-delay behavior; an explicit boot-time
bump, distinct from the INSERT OR IGNORE seeding that preserves operator
edits). **Deliberately NOT migrated**: the shepherd (it invokes Claude via
its fix runs — the strongest reason a routine may not host it), plus
`sla_forever`, `watch_forever`, `autonomy_forever` and the ClickUp poller
(control-flow-adjacent, tight cadence, already flag-guarded).

**Per-workspace rows** (seeded for every workspace, and at workspace
creation): `standup_digest`, `memory_upkeep`, `risk_scan`, `proposal_scan`,
`weekly_planning`. All enabled but inert-by-neutral-thresholds wherever
they would spend anything — a fresh install gets visibility with zero token
spend. `ROUTINES_ENABLED=false` silences ONLY these five; the builtin rows
keep firing ('off' never means 'less safe'). A workspace routine whose
schedule fails to parse is disabled fail-closed
(`last_status='error: bad schedule'`) — never a guessed cadence.
Fresh daily/weekly rows wait for their NEXT occurrence (no boot-fire of a
missed slot). Schedules are seed defaults only — after seeding the routine
ROW is authoritative (`PUT /api/routines/{id}`, audited in `admin_events`
as `routine_config`). NOTE (Epic F1): the `COALESCE(workspace_id,-1)`
unique expression index needs Postgres expression-index syntax review.

### Inbox items (the substrate)

Every routine output lands as a durable `inbox_items` row —
`UNIQUE(kind, dedupe_key)` is BOTH the re-scan idempotence guard and the
**dismissal memory**: rows are never deleted, so a dismissed key blocks
re-insert forever; only a candidate whose contributing content changed
(folded into the key) can surface again. Status is a single CAS transition
`open → dismissed | adopted | expired` (409 to the loser); the row itself
is the audit record. The janitor expires stale OPEN rows (risk alerts,
notes, digests, packs) after `INBOX_NOTICE_TTL_DAYS` — visibly, never
silently — and a fresh digest/pack expires its open predecessor directly.
Item bodies are human-facing markdown; they are **fed to prompts only via
adoption**, where the brief becomes `job.request` and takes the
untrusted-fragment posture of a ClickUp description (delimited data, not
instructions). Adoption (`POST /api/inbox/notices/{id}/adopt`) validates
exactly like `POST /api/features` BEFORE the CAS resolve (mapped project +
member project access), then runs the ordinary intake — the ONLY path from
a proposal to a pipeline. An unexpected intake failure leaves the item
`adopted` with the error recorded in refs (visible, auditable — never a
silent un-adopt).

### Standup digest (I2)

Exception-only; a quiet day sends nothing (the run records `quiet`).
Sections: gates overdue (ONE shared implementation of "overdue" —
`inboxlib.gate_summary` — used by the API inbox too) + exhausted SLA
flags, blocked/stalled pipelines, watch metrics trending off-goal
mid-window, budget position (only when a budget is configured or spend is
non-zero; pacing flag when the linear projection exceeds it), autonomy
changes. `since` = the last ok/quiet run **floored at now−24h** (a first
run or long outage never replays history). Deduped per workspace per local
date; the Slack copy (workspace webhook, `notify_text`) goes out only on a
NEW insert, strictly after the DB row.

### Memory upkeep (I3)

Threshold `MEMORY_STALENESS_THRESHOLD` (default 0 = inert; per-routine
config override). Staleness comes from the cache the engine refreshes after
every stage run — **an engine-idle repo's staleness is frozen** (verified
limitation: the routine under-fires, the safe direction, but it will not
catch exactly the most neglected repos; the no-cache/stale notes carry the
cache's `fetched_at` age for this reason). Bounded one refresh per repo per
`MEMORY_UPKEEP_COOLDOWN_DAYS` (human bootstraps count toward the bound),
skipped visibly at the daily run cap or a spent monthly budget.

### Risk surfacing (I4)

`risk_scan` emits attributed, deduplicated `risk_alert` items: Sentry 24h
velocity spikes (`RISK_SENTRY_SPIKE_EVENTS`, 0 = off; an ABSOLUTE count —
no historical snapshot store in v1, recorded limitation; one alert per
issue per day), mid-window regressing watch metrics
(`outcome.mid_window_trend`: needs a numeric target with an unambiguous
direction, ≥3 current-window readings and half the window elapsed —
anything else is `insufficient`, never a guess; one alert per watch window,
`/redo` re-arms), repeated redos on the same TARGET stage
(`RISK_REDO_THRESHOLD`; a higher count re-alerts, the same count never
repeats), and spend pacing (projection only after day 7 of the month OR
≥50% of the budget spent; the pct-bucketed key 100/120/150 means an early
alert cannot consume the month). The per-workspace `budget_monthly_usd`
column (NULL = inherit `BUDGET_MONTHLY_USD`; 0 = no budget) is the
substrate Epic G4 extends into the warn/block ladder.

### Proposal lane (I5)

Friction is engine data: `FRICTION:` protocol lines land in the
`frictions` table at harvest (OUTSIDE the ClickUp field-sync gate — the
row is the record, the mirror stays visibility) and human redos write rows
unconditionally. `proposal_scan` emits parked candidate **briefs**
(why-now, evidence, suggested next step) from four sources: decided
flat/regressed outcomes with no live successor (the lane beyond B4's single
Iterate gate; the key folds verdict + learning), friction groups per
(project, stage) with **count-bucketed keys** (3-5 / 6-10 / 11+ — a
dismissal holds until the pain measurably grows), Sentry clusters over the
stored `jobs.culprit` head (stamped at sentry intake; pre-upgrade rows have
`culprit=''`, so clusters accumulate from upgrade forward), and stale
high-traffic memory areas (monotone staleness tiers 1x/2x/5x/10x; proposed
only where the upkeep routine won't act itself). A source-signature recency
guard additionally suppresses any same-signature proposal younger than
`PROPOSAL_WINDOW_DAYS` regardless of status — dismissals are remembered.

### Weekly planning pack (I6)

Assembled mechanically (no model run): receipts this week vs last (open
runs excluded from every denominator — Epic C1's column semantics; median
gate wait; redo rate over answered gates) with trend arrows, outcome-ledger
movement, autonomy shifts, open proposals, and the deterministic "what I'd
do next lap" ranking (regressed outcomes → risk-linked clusters → friction
count desc → age), with the formula stated in the pack body (the §15
transparency posture). Carried by `GET /api/inbox` — no bespoke endpoint
(deliberate). ISO-week deduped; predecessor packs expire on the new insert.
