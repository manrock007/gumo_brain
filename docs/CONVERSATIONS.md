# Conversational gates — engine v2.1

> Status: AS-BUILT for increment 1; DESIGN for increments 2-3. Extends
> docs/ENGINE.md. This spec went through a 5-lens adversarial critique
> (resume mechanics, concurrency, state machine, operator UX, economics);
> the amendments are folded in below — including a reshaped ship order the
> economics critique forced.

## 0. Problem

v2's gates are one-shot: the engine speaks once, the human answers once.
Clarification costs a full stage re-run; the engine can't ask mid-work. The
target interaction: **the engine calls your attention; you converse until it
has what it needs; it works alone until it needs you again** — dashboard-first,
ClickUp as record.

## 1. Ship order (critique-mandated)

The original design led with session-resume chat. The economics critique
killed that ordering: resuming a P5 code session replays its 100-200k-token
transcript **per chat message**, while a fresh run primed with the gate's
documents answers doc-gate questions for ~5k tokens. And relocating the CLI's
config dir (needed for session persistence) is a deploy-affecting substrate
change — auth and onboarding state live there. So:

| increment | contents | status |
|-----------|----------|--------|
| **1** | Artifact-primed gate chat: `gate_chat` table, `POST/GET /api/jobs/{id}/chat`, dashboard chat panel, ClickUp mirroring + verb footer + non-verb nudge, the full per-repo lock domain, chat cost telemetry. Zero dependency on session storage. | **built** |
| **2** | Session persistence behind `session_persistence` flag (+ §4 bootstrap contract, session-lost detection, `--session-id` ownership, janitor), STAGE_ASK resume for code stages only. | **built** (flag off by default) |
| **3** | Fork-chat as the code-gate upgrade (session's memory of test output/exploration is the value there); chat sessions keyed by (job, stage, attempt); global claude-invocation lock for shared-store runs; artifact-primed mode stays the doc-gate default AND the fallback. | **built** (active only with the flag) |
| **4** | Gate modes: per-feature `gate_mode=light` auto-advances P2/P4–P8 on a clean STAGE_DONE — with the critique's guards built in: non-boilerplate Questions park, first clean run after a /redo parks, P5 without a captured PR_URL parks, mid-run human edits park, mirror-down parks. Default remains `full`; relax only after week-one `stage_runs` data. Since Epic C, light mode is rung 2 of the autonomy resolution order (workspace pin > light mode > computed level > full gating, ENGINE.md §15) — a pin or an opted-in earned level can extend or restrict it, the guards always apply. | **built** (opt-in per feature) |

## 2. Gate chat (increment 1 — as built)

**Artifact-primed, read-only, persist-then-poll.** Available on any parked
feature gate.

- `POST /api/jobs/{id}/chat {message}` → the human turn is persisted to
  `gate_chat` and **202-acknowledged immediately**; a background task answers
  when the repo workspace frees. The dashboard picks the reply up by polling
  `GET /api/jobs/{id}/chat` — client disconnects lose nothing, re-renders
  can't wipe in-flight state (v2's known bug class).
- The answering run is a **fresh** `claude -p` in a re-checked-out copy of the
  feature branch, primed with the gate summary, the stage artifact + PRD
  (capped inlines), and the conversation so far. Honest latency: typically
  15-90s; up to `chat_timeout_seconds` (300s) when it reads a lot of code.
- **Read-only by DENY, not just allow**: `--allowedTools Read,Grep,Glob` plus
  `--disallowedTools Edit,Write,NotebookEdit,Bash,WebFetch,WebSearch` —
  the allow-list alone is additive to settings-file grants that may live in
  the persistent workspace. After every chat run the tree is checked; residue
  is hard-reset and the reply flagged ("chat attempted writes; discarded") so
  `_checkpoint` can never commit chat leakage.
- **Single-flight per gate**: a second message while one is unanswered → 409.
  An orphaned pending turn (crash) unblocks after `chat_timeout + 60s`.
  `chat_max_turns_per_gate` (10 human turns) caps the conversation; the
  dashboard then points at Proceed/Redo/Skip.
- **Telemetry**: engine turns carry `cost_usd/num_turns/duration_ms/
  session_id/degraded` from the CLI envelope, surfaced in `/stats` and the
  dashboard. Chat rows never enter `stage_runs` (attempt/redo receipts stay
  clean).
- **Record**: every exchange mirrors to the ClickUp ticket as a
  `**[gumo_brain]** 💬 Q/A` comment ending with the footer *"Replies here
  must start with /proceed, /redo, or /skip."* The poller **nudges** (once
  per comment) any conversational non-verb human reply on a parked feature
  gate instead of silently dropping it, pointing at the verbs and the
  dashboard chat.
- Degraded honestly: unknown repo, lost branch, run failure, or
  gate-answered-while-queued all produce a labeled degraded reply, never a
  silent empty one.

## 3. Concurrency (increment 1 — as built)

**The per-repo lock domain is every operation that reads or mutates a repo
workspace**: feature stage runs, sentry phase-1/2 fixes, task phase-1/2 runs,
memory bootstraps, chat runs, and `memory.product_scope`'s canonical-workspace
fetch (a client-repo stage briefly takes the canonical repo's lock for it —
lock order is always job-repo → canonical, chat never takes two, so no
cycles). Locks are in-process `asyncio.Lock`s: the service MUST run
single-process (uvicorn workers=1 — noted for the gumoiac deploy wiring).

Chat waits for the lock rather than bouncing: the 202/poll model means a
question asked during a 40-minute stage run on the same repo simply answers
when the workspace frees. Cross-repo, chat and the serial worker can run
`claude` concurrently — safe in increment 1 because chat runs make no config-
dir writes that matter and share no workspace; increment 2's shared-session
store adds the constraints in §4.

## 4. Session resume + STAGE_ASK (increments 2-3 — as built, flag-gated)

Everything below ships in code but activates only with
`session_persistence=true`; without it, asks still park as gates and their
answers route to fresh re-runs with the Q&A injected (labeled). The critique
amendments are folded in:

- **Persistence bootstrap (flag-gated)**: `session_persistence=false` by
  default. Turning it on points stage subprocesses at
  `{data_dir}/claude-config` — which is a logged-out CLI unless bootstrapped:
  seed credentials from the legacy location or assert env-token auth in the
  deploy; seed git identity (or pass GIT_AUTHOR_*/GIT_COMMITTER_* in the
  subprocess env); add a deploy-time smoke run (`claude -p 'ok'` + a scratch
  git commit) that fails the boot loudly.
- **Session-lost detection**: never trust CLI exit signals — a resume of a
  missing session exits 0 with EMPTY stdout (verified). Stat the transcript at
  `$CLAUDE_CONFIG_DIR/projects/<cwd-slug>/<session>.jsonl` before any `-r`;
  treat exit-0-empty-stdout as session-lost; both route to the artifact-primed
  fallback, clearly labeled — never into `parse_stage_output`.
- **Session-id ownership**: pre-generate a UUID and pass `--session-id` on
  fresh stage runs (envelope capture is a cross-check) — timeout/error runs
  otherwise record no id exactly when the transcript matters most.
- **STAGE_ASK (code stages P5-P8 only; doc stages: treated as unparsed)**:
  ends the run with a question; parks with an `ask` flag +
  `resume_session_id/resume_stage/resume_attempt/resume_head/resume_answer`
  written in ONE update before the ClickUp comment. The answer is a
  **distinct transition** (not proceed): CAS keyed on the ask flag, keeps
  stage and attempts, records guidance `action='answer'`, bypasses the P9
  terminal branch. Resume validation ordering: fetch/checkout → compare
  origin HEAD to `resume_head` (third-party pushes invalidate; engine-authored
  post-park commits don't) → only then pull human edits + write guidance,
  enumerated in the resume message. The resume also carries the gate's chat
  transcript — the working session never saw the fork's conversation. Ask
  budget: 3 per (job, stage, attempt), reset on new attempt/stage; exhausted
  → normal gate whose answer routes to a fresh re-run with all Q&A injected.
  Ask Q&A pairs land in `guidance_log` so fresh re-runs see them.
- **Fork-chat (code gates)**: chat sessions keyed by (job, stage, attempt) —
  never a bare jobs column (stale-fork answers, double-fork races); decided
  under the repo lock; fresh forks re-primed with the recorded transcript.
  Shared-config-dir concurrency: env-token auth asserted; artifact-primed
  chats use a separate config dir; fork-chats take a global claude-invocation
  lock (sessions are cwd- and config-dir-bound — a clone/worktree cannot see
  them). Dollar-visible via the same chat telemetry.
- **Pruning**: by session-file mtime with a keep-set (live jobs' resume/chat
  ids, parked jobs' stage sessions) — never by job terminal status alone
  (abandoned gates, orphaned forks, and v1 sentry/task traffic never map to
  terminal jobs). Config-dir disk usage on the dashboard.
- **Chat → decision durability**: when a chatted gate resolves, distill the
  conversation into `guidance_log` (action='chat') so it rides the existing
  guidance rendering into later stages and P9's ADR pass; precedence: current
  artifact > recorded guidance > chat summary > raw transcript.
- **Workspace-reuse honesty on resume**: if other jobs used the workspace
  while parked, the resume message says ignored files (node_modules, build
  outputs) may have changed — re-run installs before trusting earlier results.

## 5. Two-lane instant-messaging chat (as built)

The v2.1 chat is async (a full tool run per reply, tens of seconds). This
increment adds the instant-messaging feel on the same contract:

- **Fast lane (default when enabled)**: a direct **streaming Messages API
  call** primed with the gate bundle — the job row (gate summary, questions,
  evidence, title), the DB-cached artifact bodies (`artifact_state.content`,
  refreshed on every artifact write and every stage push, capped 60k), the
  last 5 guidance entries, and the gate transcript. First tokens in ~1-2s.
  No subprocess, no workspace, no locks.
- **Self-escalation**: the fast lane has no repository access and is
  instructed to open its reply with `NEED_CODE_RUN: <reason>` when the
  question needs code. A holdback buffer keeps the marker off the wire; the
  engine then runs the existing slow lane. Fast-lane **errors also fall
  through** to the slow lane — the feature can only add latency, never
  subtract answers.
- **Slow lane streams too**: chat tool runs now use `--output-format
  stream-json` (`run_claude_stream`, same return contract and session-id
  fallback rules as `run_claude_raw`) and surface each tool call as a
  `status` line ("Read app/billing.py") plus the answer text as it lands.
  Stage runs are untouched (still `json` mode).
- **Transport**: `GET /api/jobs/{id}/chat/stream` (SSE: `delta`/`status`/
  `done`, `:ping` heartbeats). An in-memory per-job **ChatBroker** buffers
  the current turn so late/reconnecting subscribers replay then follow live;
  single-process like every lock here. `POST .../chat` starts the turn's
  buffer; the background task `finish()`es it in a `finally`. **Persist-then-
  poll remains the contract** — the stream is pure UX and the 5s dashboard
  poll still delivers if SSE dies. The dashboard renders a live bubble
  (re-attached across transcript repaints) and tags fast-lane turns.
- **Config**: `chat_fast_model` (empty = disabled → behavior identical to
  v2.1) + a key from `CHAT_API_KEY` (falling back to `ANTHROPIC_API_KEY`).
  The key is used only for fast-lane HTTP calls; CLI runs keep their own
  auth. `chat_api_base` / `chat_fast_timeout_seconds` / `chat_fast_max_tokens`
  complete the knobs. Fast-lane turns persist `lane='fast'` on `gate_chat`
  (num_turns=1, duration_ms; no CLI cost envelope).
- **Invariants kept**: read-only chat (the fast lane can't even reach the
  repo; the slow lane keeps the DENY list + residue reset), INSERT-only
  transcript, turn caps, single-flight per gate, cancellation tombstones,
  ClickUp mirroring of every exchange.

## 6. What deliberately stays the same

Git as truth; artifact mirrors and human-wins sync; fail-closed parsing; the
three verbs; ClickUp phone-answering; single-writer CAS gates; per-stage
telemetry. Chat is an additive layer: the human's Proceed/Redo/Skip remains
the only thing that moves the pipeline.

## 7. Agent-runtime seam (H3)

The CLI invocation itself sits behind a seam (`app/runtime.py`, `AgentRuntime`),
so WHICH agent implementation runs a prompt is swappable without touching the
engine/worker call sites.

- **Interface** — `run` / `run_stream` (both return the shared `RawRunResult`),
  covering the four capabilities BUILD-PLAN H3 names: **resume**
  (`resume_session`), **fork** (`fork_session`), **stream** (`on_event`),
  **interrupt** (`interrupt_event`) — plus `session_transcript_exists` as the
  resume-availability query.
- **Current driver: `CLIRuntime` (default).** It IS today's behavior: the
  `claude -p` subprocess with the full envelope-parsing / session-lost / reaper
  / interrupt contract that lives in `fixer._run_claude_raw_impl` /
  `_run_claude_stream_impl` (unchanged). The public `fixer.run_claude_raw` /
  `run_claude_stream` are thin shims that dispatch through `runtime_for(...)`;
  CLIRuntime delegates back to the private `_impl` bodies (single hop — no
  recursion). Every existing importer and test-patch of the public shim names
  keeps intercepting runs.
- **Stub: `AgentSDKRuntime` (migration target).** Documented, not wired: it
  would drive in-process Agent-SDK session objects instead of subprocesses
  (`resume` → SDK session resume, `interrupt` → SDK cancel; the G2 subprocess
  env allow-list becomes the SDK tool sandbox). `run`/`run_stream` raise
  `NotConfigured` — a run cannot proceed without a real runtime, so a mis-set
  `AGENT_RUNTIME` fails loud, never fakes success. Default is `cli`, so this
  path is never hit on a zero-config install.
- **Config**: `AGENT_RUNTIME` — `cli` (default) or `agent-sdk`. Empty/unknown →
  `cli` (fail closed to the working driver).
- **Orthogonal to F3 (sandboxed runs).** H3 picks the agent PROTOCOL (CLI
  subprocess vs SDK); F3's runner picks the ISOLATION (local exec vs disposable
  container) and wraps the subprocess spawn INSIDE the CLI body. They compose
  (container-of-CLI); they do not merge.
