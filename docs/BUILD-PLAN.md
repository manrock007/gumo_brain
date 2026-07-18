# CtrLoop build plan — from single-founder engine to team-backwards OS

> The execution contract for the agreed scope in
> [ENTERPRISE-READINESS-GAPS.md](ENTERPRISE-READINESS-GAPS.md) (Tier 1 bets
> 1–4 + Tier 2 minus the model-billing switch, with the GitHub App migration
> in scope). Every epic below lists its features, data-model changes, and
> definition of done. Build order is the epic order. One PR on
> `claude/enterprise-readiness-gaps-f34hi3`, commits grouped per epic.
>
> Delivery mode per item: **BUILD** (full implementation + tests),
> **SCAFFOLD** (interface + first driver + tests; second driver stubbed), or
> **FLAG** (built, shipped off by default).

## Decision log addendum (2026-07-18)

- Model billing (Claude Max → `ANTHROPIC_API_KEY`) stays deferred, **but must
  flip before the first user who is not the founder** — Anthropic policy
  prohibits routing other users' requests through personal Max credentials.
  G5 below makes the flip a config change verified end-to-end.

---

## Epic A — Team coordination & dual DRIs (bet 4)

The core team-backwards change: two DRIs, role-exclusive gates, attributed
answers, per-person queues.

- **A1 · ClickUp answer attribution — BUILD.** Stop discarding the commenter:
  keep the ClickUp `user` on every fetched comment; map ClickUp user id →
  CtrLoop identity (new `clickup_user_id` on `users`, admin-editable).
  Gate verbs from unmapped/anonymous commenters are refused with an
  explanatory reply (strictness configurable per workspace:
  `require_attributed_answers`, default on once any user has a mapping).
  `guidance_log.via` becomes `clickup:<username>` — the audit hole closes.
- **A2 · Dual-DRI storage — BUILD.** `jobs.founder_dri` + `jobs.dev_dri`
  (ClickUp person id + resolved CtrLoop user where mapped). Intake reads both
  people fields; dashboard submit gains both fields. Legacy `owner` kept as a
  computed alias during migration.
- **A3 · Role-exclusive stage gates — BUILD.** Per-workspace `stage_role_map`
  (default: P0/P1 → founder, P2–P8 → dev, P9 → founder; every stage
  overridable). At gate park, the owning DRI is assigned + notified. Answers
  from the non-owning DRI: dashboard 403 with "this is a founder gate, owned
  by <name>"; ClickUp reply explaining ownership. Non-owners retain gate
  chat and comments. Enforcement bypass only via admin override (audited).
- **A4 · Per-person work queues — BUILD.** "Awaiting you" becomes first-class:
  owner-scoped inbox (gates owned by me, overdue first), per-user badge
  counts, and a `GET /api/inbox` endpoint powering it.
- **A5 · Gate SLA & escalation — BUILD.** Per-workspace gate SLA
  (`gate_sla_hours`, default 24). Overdue gates escalate: re-nudge owner →
  notify the other DRI (visibility, not authority) → flag on the standup
  surface. Escalation events recorded on the job timeline.

## Epic B — The outcome loop (bet 1)

Close Review + Accountability: metric goal in at P0, measured verdict out
after ship.

- **B1 · Metric goal at intake — BUILD.** Feature submit (dashboard +
  ClickUp `[feature]` adoption) captures `success_metric`, `metric_target`,
  `metric_window_days`. P0/P1 artifact contracts require a `## Success
  metric` section restating goal + how it will be measured; the gate refuses
  a STAGE_DONE lacking it (fail-closed, consistent with existing parsing).
- **B2 · Instrumentation check at P8/P9 — BUILD.** P8 self-review contract
  gains "verify the metric events/flag exist in the diff"; P9 keeps emitting
  `FLAG_NAME:`/`SUCCESS_METRIC:` and adds `METRIC_EVENT:` lines harvested to
  the job row.
- **B3 · Analytics adapter — SCAFFOLD.** `AnalyticsProvider` interface
  (`query_metric(name, window) -> series`), Mixpanel driver first
  (per-workspace credentials), null driver for instances without analytics.
- **B4 · Post-ship watcher — BUILD.** New job kind `watch`: spawned
  automatically when a feature's PR merges (shepherd detects merge today).
  Reads metric vs goal daily for `metric_window_days`, then renders a
  verdict — `moved / flat / regressed` — and parks a founder-owned
  **Iterate gate**: adopt a follow-up, log a learning, or close.
- **B5 · Outcome ledger — BUILD.** `outcomes` table (job, metric, goal,
  observed, verdict, decided_by); dashboard "Outcomes" view (ledger +
  verdict distribution); verdict written into product memory (changelog
  entry + optional ADR) so the next lap starts from measured reality.

## Epic C — Graduated autonomy / the trust ladder (bet 2)

- **C1 · Autonomy score — BUILD.** Nightly computation per (workspace, repo,
  stage) from `stage_runs`: redo rate, unparsed/fail rate, gate-answer
  latency, shepherd round counts → level 0–3 (0 = always gate, 3 = full
  auto-advance for that stage). Transparent formula, stored with inputs.
- **C2 · Auto-tuned gates with pins — BUILD.** Gate behavior resolves:
  **manual pin > computed level > default full-gating**. Pins are
  per-workspace per-stage: `always_gate` ("never auto-advance P7") or
  `always_auto`; pins always win and never expire. All existing light-mode
  guards remain (mid-run edit, mirror-down, first-run-after-redo, missing
  PR all force a park).
- **C3 · Autonomy surface — BUILD.** Per-stage trust dial on the feature
  strip, a workspace autonomy matrix (stage × repo), an auto-advance log
  ("P6 auto-advanced — level 3, 14 clean runs"), and one-click clawback
  (drops the level to 0 pending re-earning, audited).

## Epic D — Organizational context (bet 3)

- **D1 · People & ownership model — BUILD.** `people` profile layer over
  auth users: role (founder/product/dev/design), areas of ownership
  (workspace/repo/product-area), decision authority tags. Feeds A3 routing
  defaults and stage prompts ("product decisions here belong to <name>").
- **D2 · Decision registry — BUILD.** Cross-ticket `decisions` table:
  auto-registered from substantive gate answers (already detected for the
  ClickUp Decisions field), manual add from dashboard, each with scope
  (job/repo/product/org), decider, rationale, links. Search + filter UI.
  P9 distillation reads from the registry, not just per-job guidance.
- **D3 · Slack read ingestion — FLAG.** Per-workspace Slack channel
  allowlist; a poller captures decision-shaped threads (emoji-marked or
  `!decision` convention) as registry *candidates* parked for human
  confirmation — never auto-committed to memory. Off by default.
- **D4 · Memory retrieval upgrade — BUILD.** SQLite FTS5 index over `.gumo/`
  memory, artifacts, decisions, and guidance; stage prompts get a
  `memory_search` results block (top-k snippets + paths) in addition to the
  existing capped inlines; the index refreshes on every memory cache refresh
  and artifact write.

## Epic E — Identity & audit (Tier 2)

- **E1 · SSO — BUILD (OIDC), SCAFFOLD (SAML/SCIM).** OIDC login (authlib)
  against any provider (Okta/Entra/Google); JIT user provisioning with role
  mapping; local auth remains as fallback/break-glass. SAML + SCIM stubbed
  behind the same identity interface for a later pass.
- **E2 · API tokens — BUILD.** Per-user scoped tokens (`ctl_` prefix,
  hashed at rest, last-used tracking, revocation, expiry); HTTP Basic
  password auth for automation is deprecated and disabled once a user has
  a token.
- **E3 · RBAC v2 — BUILD.** Roles: instance admin, workspace admin, member,
  viewer. Config mutations require the admin role *of that scope*;
  submitting work/answering gates requires member; viewer is read-only.
  Per-repo restriction available per member (empty = all repos).
- **E4 · Audit log — BUILD.** Append-only `audit_log` covering every gate
  decision (both channels, with actor), steer, chat turn, admin/config
  mutation, user/token lifecycle, autonomy pin change, and clawback.
  Dashboard viewer + `GET /api/audit/export` (JSONL, cursor-paged) for SIEM.

## Epic F — Execution substrate (Tier 2)

- **F1 · Postgres + Alembic — BUILD.** DB layer gains a driver seam;
  Alembic owns schema (baseline autogenerated from current SQLite schema);
  SQLite remains the zero-config default for solo instances, Postgres the
  documented team deployment. All CAS transitions re-verified under
  Postgres semantics.
- **F2 · Multi-worker queue — BUILD.** Job claim moves to the DB
  (`FOR UPDATE SKIP LOCKED` on Postgres; current single-consumer path on
  SQLite). Per-repo serialization moves from in-process asyncio locks to DB
  advisory locks, removing the `workers=1` constraint on Postgres.
- **F3 · Sandboxed runs — FLAG.** Runner interface: `local` (today's
  in-container exec) and `container` (each run in a disposable container
  with the clone mounted, no ambient env, egress allowlist). Container
  runner ships flag-off with docs; local remains default.
- **F4 · Observability — BUILD.** `/metrics` (Prometheus: queue depth, runs,
  costs, gate latencies, autonomy levels), `/health/ready` (DB + integration
  reachability), structured JSON logs with request/job ids.

## Epic G — Credentials & security (Tier 2 + the PAT fix)

- **G1 · GitHub App — BUILD.** App auth with per-repo short-lived
  installation tokens minted per run; PAT remains a fallback path.
  Subprocess env gets the minted token only, never the long-lived secret.
- **G2 · Secrets provider seam — SCAFFOLD.** `SecretsProvider` interface:
  env driver (default) + file driver; vault driver stubbed. Subprocess env
  is allow-listed (only the vars a run needs) instead of `os.environ.copy()`.
- **G3 · Prompt-injection hardening — BUILD.** Every untrusted input
  (Sentry titles/culprits, ClickUp text, PR review findings, chat) wrapped
  in the delimited untrusted-data block; property test asserts no prompt
  builder interpolates untrusted text outside a delimiter.
- **G4 · Budgets & spend alerts — BUILD.** Per-workspace monthly budget
  aggregating `stage_runs` + `gate_chat` costs; warn at 80%, block
  non-forced runs at 100% (admin override, audited); spend panel on the
  dashboard.
- **G5 · API-billing readiness — BUILD (config-flip only).** Verify the
  whole engine end-to-end under `ANTHROPIC_API_KEY` (runs, sessions,
  fast-lane); document the flip; startup warning when Max token is used on
  an instance with >1 active user (the policy line).

## Epic H — Abstraction seams (Tier 2)

- **H1 · Tracker adapter — SCAFFOLD.** `Tracker` interface extracted from
  the ClickUp client (tickets, comments, fields, people); ClickUp driver =
  current behavior; Jira driver stubbed with a mapping doc. All engine/
  worker call sites go through the interface.
- **H2 · VCS adapter — SCAFFOLD.** `VCS` interface (clone/push URL minting,
  PR create/ready/comment/state); GitHub driver = current; GitLab stubbed.
- **H3 · Agent-runtime adapter — SCAFFOLD.** `AgentRuntime` interface
  wrapping today's CLI invocation (run, stream, resume, interrupt); CLI
  driver = current `run_claude_*`; Agent-SDK driver stubbed as the
  documented migration target.
- **H4 · Analytics adapter** — delivered in B3 (same seam).

## Out of scope (Tier 3 — future)

Listen-grader generalization, the Plan/objectives layer, Cowork-skill
fold-in, cross-org learning, SOC 2 audit itself, SCIM, the actual Max→API
*flip* (G5 makes it a config change).

---

## Swarm execution protocol

Phased workflow runs, human-checkpointed between phases:

1. **Plan verification** — per-epic design agents produce file-level change
   plans; adversarial reviewers attack each plan (migration safety, CAS
   races, fail-closed invariants) before any code.
2. **Implementation** — epics A→H in dependency order (A1 before A3; B3
   before B4; F1 before F2; E4 consumes A1). Parallel agents within an
   epic on disjoint modules; serialized across epics touching
   `worker.py`/`engine.py`.
3. **Review & test** — every epic: full test suite + new tests; then an
   adversarial review pass (correctness, security, the ENGINE.md
   invariants: human-edits-win, fail-closed, single-writer CAS) with
   verified findings fixed before the next epic.
4. **Docs & PR** — ENGINE.md/OPERATIONS.md/README updated per epic; final
   integrated review of the whole diff; PR opened (draft) with the epic map
   in the body.

Non-negotiable invariants for every agent: git is truth; human edits always
win; anything ambiguous fails closed; single-writer CAS on every state
transition; ClickUp stays best-effort visibility; every new mutation lands
in the audit log; every new config key has an env default and appears in
OPERATIONS.md.
