# Conversational gates — engine v2.1

> Status: DESIGN SPEC. Extends docs/ENGINE.md; nothing there is retracted.
> This document exists to be criticised — each section states its decision AND
> what was rejected.

## 0. Problem

v2's gates are one-shot: the engine speaks once (STAGE_DONE + questions), the
human answers once (/proceed, /redo, /skip). Clarification requires re-running
a whole stage; the engine cannot ask a question mid-work without failing the
stage; and interrogating a decision ("why option B?") costs a redo. The
interaction we want: **the engine calls your attention; you converse until it
has what it needs; it works alone until it needs you again** — dashboard-first,
ClickUp as record.

## 1. The unlock: resumable stage sessions

Every headless run (`claude -p --output-format json`) returns a `session_id`,
and the CLI can resume it (`claude -p -r <session_id> "<message>"`) — with the
run's full context intact — or fork it (`--fork-session`) into a new session
that shares the history but not the identity. v2 discards the id; v2.1 keeps it.

- `stage_runs` gains `session_id` (captured from the envelope).
- **Persistence:** stage subprocesses run with `CLAUDE_CONFIG_DIR=
  {data_dir}/claude-config` (with `HOME` pointed at a `{data_dir}/claude-home`
  fallback), so session transcripts live on the data volume and survive
  container restarts. Sessions are bound to the working directory they ran in;
  workspaces are stable per-repo paths on the same volume, so resume works
  after a restart.
- **Pruning:** session files grow with use. A daily janitor deletes session
  files whose job reached a terminal status more than `SESSION_TTL_DAYS`
  (default 14) ago.
- **Graceful degradation:** if a session file is missing (pruned, volume
  swapped), chat falls back to a fresh run primed with the gate context
  (artifact + questions + chat history), and STAGE_ASK resume falls back to a
  fresh stage re-run with the Q&A injected — v2 behavior, clearly labeled in
  the reply.

## 2. Gate chat — converse before deciding

**Decision: chat with a FORK of the parked stage session; the original session
is never consumed by conversation.**

Flow:
1. A stage parks at its gate (unchanged). The dashboard card gains a chat box.
2. First human message: engine forks the stage session
   (`-p -r <stage_session> --fork-session`) → `chat_session_id` stored on the
   job. Subsequent messages resume `chat_session_id` directly.
3. Chat runs get **read-only tools** (Read/Grep/Glob), a short timeout
   (`chat_timeout_seconds`, default 300), and a system-style preamble: *"You
   are at the P<k> gate answering the reviewer's questions. Do not modify
   anything; answer concisely; if the question requires changing the work,
   say so and recommend /redo with specific notes."* Plain text out — no
   STAGE markers.
4. Every turn (both directions) is stored in a `gate_chat` table
   (job_id, stage, attempt, role, text, at) and mirrored to the ClickUp ticket
   as `**[gumo_brain]** 💬` comments — the record stays complete; the poller
   ignores them (no leading /verb).
5. The human ends with Proceed/Redo/Skip as before. The chat transcript for
   the current gate (capped ~3000 chars, newest-first trimming) is included in
   the next stage's prompt as "gate conversation", alongside the recorded
   guidance.

Concurrency: chat runs share the per-repo workspace with stage runs. A
**per-repo asyncio lock** serializes them: the worker holds it for the duration
of a stage run; chat acquires it, re-checks out the job's branch (cheap), runs,
releases. If the lock is held (a stage is running on that repo), the chat
endpoint returns 423 with "engine is mid-run on this repo — try again in a few
minutes"; the dashboard renders that state honestly. Chat is only offered on
PARKED jobs, so the common case never contends with the job's own stage runs —
only with other jobs on the same repo.

Cost note: resuming a long session replays its context (prompt-cache pricing
applies but is not guaranteed across time). Bounds: chat runs are capped at
`chat_max_turns_per_gate` (default 10) with per-run `--max-turns`-style
brevity instructions; the dashboard shows a per-feature running cost total
(from `stage_runs` + chat run telemetry) so expensive conversations are
visible, not surprising.

Rejected alternatives:
- *Chatting by resuming the original session* — a later STAGE_ASK resume of
  that session would inherit the conversation noise; forking isolates it.
- *WebSocket/SSE streaming* — v2.1 is request/response with a spinner
  (~15-45s per answer); streaming is a later polish, not a design change.
- *An LLM proxy answering from artifacts alone (no session)* — cheaper but
  answers from documents, not from the context that did the work; that's the
  fallback mode, not the primary.

## 3. STAGE_ASK — the engine interrupts itself

**Decision: a stage that hits a genuine decision it cannot make ends with
`STAGE_ASK:` and is RESUMED (not re-run) after the human answers.**

- New end-anchored marker, same parsing discipline as STAGE_DONE/STAGE_FAIL:
  `STAGE_ASK:` + a short context block ending in `## Questions`. Prompts for
  code stages (P5-P8) instruct: *"If you hit a decision the plan doesn't
  settle and the human must make, commit what you have, then output
  STAGE_ASK — do NOT guess and do NOT STAGE_FAIL for askable questions."*
  Doc stages keep using their questions sections at STAGE_DONE (a doc stage
  IS one big ask); STAGE_ASK is accepted but discouraged there.
- On STAGE_ASK the engine: checkpoints the branch (as always), parks the job
  as `awaiting_input` with the question and an `ask` flag, records
  `resume_session_id` + `resume_stage` on the job. Gate card renders with
  **Answer** as the primary action (it is still `/proceed <answer>` under the
  hood — same verbs, same CAS, ClickUp phone-answering keeps working).
- On the answer, `run_stage` sees a valid pending resume for the current
  stage+attempt and **resumes the session** with the human's answer instead
  of building a fresh prompt: same workspace, branch re-checked-out, artifact
  pull still runs first (human edits made while parked must be visible;
  the resume message lists them). Telemetry: a new `stage_runs` row marked
  `resumed=1`; `stage_state.base_sha` and attempts are NOT advanced — a
  resumed run is the same attempt continuing.
- Redo/skip at an ask-gate behave exactly as at any gate (redo discards the
  pending resume and re-runs fresh with notes).
- **Ask budget:** max `max_asks_per_stage` (default 3) resumes per stage
  attempt; the next ask converts to a normal gate with a warning ("this stage
  keeps asking — consider /redo with clearer guidance or a better plan").
- Invalidation: a pending resume is dropped (fresh re-run instead) if the
  stage, attempt, or branch head at park time no longer match — fail closed
  to v2 semantics, never resume into a stale world.

## 4. Gate modes — "works alone until it needs you"

With STAGE_ASK in place, all-gated stops being the only safe mode.

- `jobs.gate_mode`: `full` (default — every stage parks, v2 behavior) or
  `light` — only **P0, P1, P3, P9** park unconditionally; P2 and P4-P8
  auto-advance on a clean STAGE_DONE, recording `action='auto'` in
  guidance_log and still posting the stage summary + evidence to ClickUp
  (the record never thins). STAGE_ASK, STAGE_FAIL, unparsed output, push
  failures, and mid-run human edits (conflicted artifacts) ALWAYS park,
  regardless of mode.
- Per-feature choice at submit (dashboard selector); global default via
  `default_gate_mode` config. The stats panel shows auto-advanced stages, so
  light mode's behavior is inspectable after the fact.
- Rejected: auto-advance for P3 (data-model decisions are the gate that
  matters most) and P9 (shipping is a human act).

## 5. Storage & API deltas

- `stage_runs` += `session_id TEXT`, `resumed INTEGER DEFAULT 0`.
- `jobs` += `chat_session_id TEXT`, `resume_session_id TEXT`,
  `resume_stage INTEGER`, `ask_count INTEGER DEFAULT 0`, `gate_mode TEXT
  DEFAULT 'full'`.
- New table `gate_chat(id, job_id, stage, attempt, role, text, at)`.
- `POST /api/jobs/{id}/chat {message}` → `{reply, degraded?}` (auth as ever;
  409 if not parked; 423 if the repo workspace is busy).
- `GET /api/jobs/{id}/chat?stage=<k>` → transcript for the gate (dashboard
  renders it; also included in `/stats`).
- `POST /api/features` accepts `gate_mode`.
- Dashboard: chat panel on parked cards (transcript + input + spinner; chat
  drafts get the same localStorage persistence as answer drafts), "Answer"
  primary button on ask-gates, gate-mode selector on the feature form,
  auto-advanced stages rendered dimmed in the stage strip.

## 6. What deliberately stays the same

Git as truth; artifact mirrors and human-wins sync; fail-closed parsing; the
three verbs; ClickUp phone-answering and the poller; single-writer CAS gates;
per-stage telemetry. Chat and asks are additive layers on the same machinery.

## 7. Risks the critics should attack

1. Resume/fork mechanics across CLI versions; CLAUDE_CONFIG_DIR vs HOME for
   session storage; session-to-cwd binding assumptions.
2. Workspace contention between chat and stage runs (the per-repo lock), and
   resume-after-another-job-used-the-workspace correctness.
3. STAGE_ASK state machine: crash windows around park/resume, ask-budget
   accounting, interaction with redo targeting earlier stages.
4. Cost blowups: long sessions × chatty gates; what the caps must be.
5. UX honesty: 15-45s chat latency, 423 busy states, degraded-mode labeling.
