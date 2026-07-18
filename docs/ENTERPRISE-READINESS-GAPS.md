# CtrLoop — claims vs. implementation: the gap analysis

> Assessment of the CtrLoop pre-seed one-pager (July 2026) against the code in
> this repo, plus the enterprise-readiness and team-OS roadmap that follows
> from it. Written as a founder document: what's real, what's claimed, what
> it would take. Evidence citations are `file:line` in this repo.

---

## 0. The honest one-line verdict

The one-pager pitches a six-stage execution loop (Plan → Execute → Review →
Listen → Coordinate → Learn) on Universal Context. The repo implements a
genuinely excellent **Execute + Coordinate slice** — roughly 2 of the 6 loop
stages plus the code-facing half of the context layer — architected for **one
founder driving one product with one Claude Max account**. The one-pager is
honest about this (the CtrLoop row is marked "DESIGN TARGET, pre-product"),
so this is not an integrity problem; it is a build map.

What that means concretely: the loop's two hardest claims — **Review**
(product data feeding back) and **Accountability for outcomes, not outputs**
— have *zero* code behind them today, and they are also the two claims no
competitor (Devin, Factory, Cursor, Replit) has shipped. That is where the
company is, or isn't, a unicorn.

---

## 1. Claim-by-claim scorecard

### The loop stages

| Stage (one-pager) | Status | Evidence |
|---|---|---|
| **Plan** — objectives, priorities | 🟡 Per-ticket only | P0–P4 doc stages produce intake → PRD → recon → design → build-groups (`app/feature_prompts.py:161-185`). No roadmap, no cross-feature prioritization, no objectives/OKR model. The only "business objective" is a free-text `business_context` blob injected into every prompt (`app/prompts.py:13-30`). A success metric never enters at P0 as a goal — it appears only at P9 as a self-reported string. |
| **Execute** — build, ship | 🟢 Deep and real | P5–P8 code stages with commit-per-step, draft PRs, per-criterion test tables, self-review (`app/feature_prompts.py:187-219`); 2-phase Sentry/task fixer (`app/fixer.py`); the **shepherd** autonomously drives PRs through review-bot rounds to approval (`app/prompts.py:387-429`, `app/worker.py:1119+`). This is the differentiated engineering. |
| **Review** — product data, metrics | 🔴 Absent from the engine | No analytics client anywhere in `app/`. The engine captures `SUCCESS_METRIC:` as a *string* into a ClickUp field (`app/engine.py:953-976`) and never reads its value. Post-ship measurement exists only as human-run Cowork skills (gumo-p8-launch / p9-iterate + Mixpanel MCP) — outside the product. |
| **Listen** — customers, feedback | 🔴 Sentry errors only | Grading of production errors is real and thoughtful (`app/grading.py`). But no support tickets, NPS, interviews, feature requests, or any customer-voice channel is ingested. The FRICTION harvest (`app/engine.py:967-970`) is process feedback, not customer feedback. |
| **Coordinate** — briefs, assigns | 🟡 Deep but single-human | Gates with atomic single-writer CAS, dual-channel (dashboard + ClickUp), gate chat with fast/slow lanes, mid-run steering, Slack nudges — all real and well-built (`app/worker.py:597-609`, `app/fastlane.py`, `docs/CONVERSATIONS.md`). But routing is **one DRI per ticket** (`app/worker.py:1011-1019`); no multi-human routing, escalation chains, per-role gates, or work queues per person. |
| **Learn** — improves next lap | 🟡 Memory + process telemetry; no outcome learning | `.gumo/` product memory with P9 distillation, per-entry ADRs/changelog, freshness metric, staged context matrix (`app/memory.py:43-54`) — genuinely good. `stage_runs` receipts (cost/turns/gate-wait/redo-rate, `app/db.py:85-103`) are a real asset. But nothing ever learns from *product outcomes*: "feature #50 starts smarter than #1" means richer memory files, not measured results. |
| **Universal Context** | 🟡 Code context real; org context absent | Repo map + workspace hierarchy + business context + git memory cover the *code and product* half. Absent: people/ownership model, decisions made outside tickets, conversations (Slack is write-only — `app/workspaces.py:165-178`), customer/market context, and any retrieval beyond capped inline excerpts (no search, no embeddings). |

### The core principles

| Principle | Verdict |
|---|---|
| "One shared understanding: people, code, agents" | Code ✅, product ✅, **people ❌, agents ❌** (single hard-wired agent runtime). |
| Collaboration — humans + AI in same context | ✅ for one human at a time; the gate/chat/steer loop is best-in-class for a solo operator. |
| Proactive — anticipates work, surfaces risks | ❌ except the Sentry sweep. Nothing recommends what to build, flags risk, or initiates work. |
| Iterative — every interaction strengthens memory | 🟡 Only *gated, on-ticket* interactions strengthen memory. Everything else (Slack, meetings, docs) evaporates. |
| Accountability — outcomes, not outputs | ❌ The engine ensures **outputs** (PRs, artifacts, passing tests). Outcomes are never read back. This also blocks the pitched **outcome-tier pricing** — you cannot bill on a metric you never measure. |
| "The execution loop every AI agent runs on" | ❌ as stated. The runtime is the Claude Code CLI, invoked by binary name with CLI-specific flags and session-store layout (`app/fixer.py:230-259`). No agent abstraction exists. Today it is "the execution loop one specific agent runs on." |

---

## 2. What's broken with single-person-controls-Claude (the team-OS diagnosis)

These are the structural reasons the current system cannot become a
team-backwards OS by accretion — each is a design assumption, not a bug:

1. **The founder is the loop's clock speed.** Every stage parks at a gate;
   every gate funnels to effectively one human. Throughput = one person's
   attention. The serial worker (one job at a time, `uvicorn workers=1`
   required by in-process asyncio locks — `app/engine.py:61-85`,
   `docs/CONVERSATIONS.md:79`) matches that assumption exactly. A team of five
   would starve it; `MAX_RUNS_PER_DAY=8` caps it anyway.
2. **Identity is a personal possession, not an org resource.** All runs share
   one GitHub fine-grained PAT and one Claude **Max-subscription OAuth token**
   (README setup §1). Agent actions are not attributable to any human
   principal; enterprise procurement (and arguably consumer-plan ToS) rules
   this out. There are no API tokens (automation reuses the user's *password*
   over HTTP Basic — `app/auth.py:9,131-133`), no SSO/SCIM, and RBAC stops at
   two roles + workspace membership.
3. **Attribution has a hole exactly where teams would use it.** Dashboard
   answers record the acting user; **ClickUp gate answers are anonymous** —
   the commenter's identity is discarded (`app/clickup.py:288`,
   `app/worker.py:906`). Anyone with ClickUp access can `/proceed` a stage
   with no CtrLoop identity attached. "Who approved P3" is only answerable
   for one of the two channels. Same for admin/config actions: no audit trail
   at all.
4. **Knowledge capture is gated through tickets.** A decision only enters
   memory if it was made at a gate on a job. Team decisions happen in Slack,
   meetings, and docs; today those channels are invisible to the engine, so
   with a team the memory would capture a shrinking fraction of the org's
   actual context — the compounding claim inverts.
5. **Trust is static, not earned.** `gate_mode=light` exists
   (`app/engine.py:515-549`) but is per-feature config a human sets. The
   one-pager's ownership progression (human-owns → co-own → AI-owns) implies
   autonomy *earned from track record* — and the `stage_runs` receipts to
   compute it already exist — but nothing connects them.
6. **Single-tenant substrate.** SQLite with per-call connections and no WAL
   (`app/db.py:262-270`), homegrown additive migrations, secrets as plaintext
   env vars inherited by the agent subprocess (`app/fixer.py:249-251`), no
   sandbox beyond tool allow-lists and the shared container, no metrics
   endpoint, no prompt-injection posture despite Sentry titles and ClickUp
   comments flowing into a write-enabled agent's prompt.

---

## 3. What enterprise buyers will demand (outside-in)

From current (2026) enterprise AI-agent procurement practice: the consistent
gatekeepers are a signed DPA, SOC 2 evidence, and SIEM-queryable audit logs;
every agent session must map to a named human identity via SAML SSO + SCIM;
agent execution must be sandbox-isolated with governed egress; and ~88% of
agent pilots die on exactly this deployment/governance layer, not on agent
quality. Meanwhile the competitive frontier moved: Devin ships org-level
knowledge bases + DeepWiki auto-docs at ~$492M ARR (May 2026), and Factory
sells "agent-native development" into MongoDB/EY/Bayer-scale orgs. **Code
execution is commoditizing; none of them close the outcome loop.** (Sources
in the PR/analysis message accompanying this document.)

Mapped to this repo, the enterprise table stakes are: SSO/OIDC + SCIM; real
API tokens; RBAC below workspace level; complete audit trail (both gate
channels + admin actions); Postgres + real migrations; horizontally scalable
workers with per-run sandboxed execution; a secrets vault with per-run
short-lived credentials (GitHub App installation tokens, not a personal
PAT); API-key billing for model usage (not a founder's Max token); egress
policy; prompt-injection hardening; SOC 2.

---

## 4. The bets, ranked on merit

Ranking axes: does it create the moat the pitch claims (differentiation),
does it unlock revenue (enterprise sellability), and does it compound
(does doing it make everything after it better). T-shirt costs assume the
current codebase as the base.

### Tier 1 — the thesis bets (differentiating; do these to *become* the pitch)

**1. Close the outcome loop (Review + Accountability).** Metric goal in at
P0 (from a metrics catalog, e.g. Mixpanel), flag + instrumentation checked at
P8, and a post-ship watcher job kind that reads the metric vs. goal for N
days and writes the verdict into product memory and the feature's ledger.
This is: the single largest claims-gap, the prerequisite for outcome-tier
pricing, the thing no competitor does, and cheap relative to its story value
— the engine already has job kinds, schedulers (sweep/shepherd/janitor
loops), and the P9 hook. *Highest merit by every axis.* (M)

**2. Graduated autonomy as a product mechanic (the trust ladder).** Compute
per-(workspace, stage, repo) autonomy from `stage_runs` history — redo rate,
gate-latency, shepherd rounds — and auto-tune which gates auto-advance, with
a visible "autonomy level" per pipeline and one-click clawback. This turns
the one-pager's ownership-progression diagram (human-owns → co-own →
AI-owns) from a slide into a measurable dial, built on receipts
infrastructure that already exists. It is also the honest answer to "how do
teams learn to trust the agent." (M)

**3. Organizational context beyond the repo (Universal Context, actually).**
Three parts, in order of leverage: (a) a **people/ownership model** — who
owns which product area, who decides what kind of decision, feeding gate
routing; (b) a **cross-ticket decision registry** — today a decision exists
only inside one job's guidance log or one repo's ADR dir; (c) **read-side
Slack/docs ingestion** — Slack is currently write-only; the highest-signal
org context (why we chose X) lives there and evaporates. Retrieval must move
from capped inline excerpts to indexed search as memory grows. (L)

**4. Multi-human coordination (the team-backwards core).** Role-typed gates
(PRD gates → product DRI, design gates → eng lead, launch gates → founder),
per-person work queues ("awaiting-you" already exists as a filter — make it
the product), escalation/SLA timers on parked gates, and closing the ClickUp
attribution hole (map commenter → CtrLoop identity, refuse anonymous verbs
on configurable-strictness). Note the gumo-standup skill already prototypes
the exception-surface pattern — pull it into the product. (M–L)

### Tier 2 — the enterprise unlock (necessary to sell; not differentiating)

**5. Identity & audit.** OIDC/SAML SSO, SCIM, real API tokens, RBAC below
workspace (at minimum: per-repo action rights + config-vs-work separation),
append-only audit log covering both gate channels and every admin/config
mutation, SIEM export. (M)

**6. Execution substrate.** Postgres + Alembic; external queue; a worker
pool where each run executes in a disposable sandbox container (which also
unlocks the per-job-worktree concurrency deferral in ENGINE.md §9 — today
one 40-minute P5 run blocks every other job in the workspace); horizontal
API scaling once locks leave process memory. (L)

**7. Credential & security posture.** GitHub App with short-lived per-repo
installation tokens; model access via org API keys with per-workspace budget
caps and spend alerting (cost capture already exists per-run — aggregate
it); secrets in a vault, never inherited wholesale by the subprocess;
explicit prompt-injection stance (the untrusted-data delimiting exists in
prompts — extend it to Sentry titles and make it a tested property); egress
allowlist for run sandboxes. SOC 2 once the above are true. (M–L)

**8. The abstraction seams the tagline requires.** Tracker (ClickUp | Jira |
Linear), VCS (GitHub | GitLab), telemetry (Sentry | Datadog), and — most
strategically — **agent runtime** behind an adapter, so "the loop every
agent runs on" stops being aspirational. ENGINE.md already gestures at this
("the substrate must be swappable") but no seam exists in code: no Protocol,
no adapter, ~50 ClickUp-specific config keys. Do tracker first (every
enterprise asks for Jira), agent runtime second (it is the moat-consistent
one: the loop, not the agent, is the product). (L, incremental)

### Tier 3 — expansion hypotheses (sequence after product-market proof)

**9. Listen as an intake grader.** Generalize `grading.py` from Sentry
issues to *any* signal — support tickets, NPS verbatims, churn events,
sales-call notes — scored into candidate work items parked for human
adoption. This makes the engine *proactive* (it proposes the backlog) and is
the natural second landfall after errors. (M)

**10. The Plan layer.** Objectives with owners and target metrics;
features link to objectives; portfolio view of live pipelines with
cost/receipts per objective. Only worth building after #1 exists (a plan
layer over unmeasured outcomes is theater). (M)

**11. Fold the Cowork-skill loop into the product.** The gumo-p0…p9 skills
encode the *founder half* of the loop (brief-with-metric, launch watch,
iterate) as prompt files outside the product, single-tenant by construction.
They are the requirements spec for #1 and #10 — migrate their logic into
engine job kinds so the whole loop ships to customers, not just to Gumo. (M)

**12. Cross-org compounding (much later).** Anonymized workflow/playbook
learning across tenants — real network-effect potential, severe
privacy/enterprise-trust constraints; do not attempt before SOC 2 and
per-tenant isolation are boring. (XL)

### Agreed scope (founder decision log, 2026-07-18)

- **Tier 1**: do bets **1, 2, 3, 4**. Bet 2 ships with **manual override pins**
  — a per-gate "never auto-advance" setting that always wins over the computed
  autonomy level (e.g. pin P7 human-gated forever regardless of track record).
- **Tier 2**: do everything **except** the Claude Max → API-billing switch,
  which is deferred to the very end. The GitHub PAT → GitHub App migration is
  in scope now.
- **Dual-DRI correction (folds into bet 4)**: the original workflow contract
  is TWO DRIs (founder + developer). The engine currently flattens them: at
  ClickUp adoption it takes `assigned dev dri` and uses `assigned founder dri`
  only as a *fallback when dev is empty*, keeps just the first person, and
  stores a single `owner` column (`app/worker.py:1016-1019`, `app/db.py:31`);
  every gate then assigns that one person regardless of stage
  (`app/engine.py:633-635`). Fix: store both DRIs, type each stage's gate
  (product vs technical), route product gates (P0/P1 intake-PRD, P9 ship)
  to the founder DRI and technical gates (P2–P8) to the dev DRI, with the
  other DRI CC'd and either allowed to answer.

### The sequence in one line

**#1 → #2 → #4 → #5/#6/#7 in parallel with first design partners → #8 → #9/#10.**
Rationale: prove the differentiated claim first (outcome loop + trust
ladder) on the substrate you have, because that is what a pre-seed → seed
story needs; buy the enterprise unlock with the money that story raises;
never let table-stakes work crowd out the two features that make the pitch
true and unique.

---

## 5. What is already genuinely strong (protect these)

Worth stating because they are rare and easy to break in a rewrite:

- **Fail-closed everywhere** — unparseable output parks, sync ambiguity
  parks, lost branches park. Enterprise trust is built on exactly this.
- **The artifact sync protocol** (git truth, ClickUp editing surface,
  human-wins CAS reconciliation tolerant of markdown mangling,
  `app/artifacts.py`) — nobody else lets a founder rewrite a PRD on their
  phone mid-run without losing either side's work.
- **The receipts** (`stage_runs`) — cost/turns/gate-wait/redo per stage.
  "The 10x claim is measured, not asserted" is a cultural asset; it becomes
  the data plane of bet #2.
- **The shepherd** — autonomous PR-review driving with verify-first fixes
  and rebuttals is ahead of most of the market.
- **Crash-safety discipline** — gate-park ordering, boot recovery, reaper,
  single-writer CAS. The bones of a reliable multi-tenant service are here
  even though the substrate isn't.

The engineering culture visible in ENGINE.md (adversarial critiques folded
into specs, deliberate deferrals listed, honest degraded modes) is itself a
moat input. The gap is not quality; it is that everything was built
one-founder-deep and two-loop-stages-wide, and the pitch — correctly —
describes the whole loop.
