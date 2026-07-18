# Tracker seam (H1) — ClickUp → Jira mapping

The tracker seam (`app/tracker.py`) abstracts the issue tracker behind the
`Tracker` ABC. The **current and default** driver is ClickUp
(`app/clickup.py`, `ClickUpTracker`); a `JiraTracker` scaffold ships inert
(`enabled=False`, every method a no-op returning the outage sentinel). This doc
is the concept map a real Jira driver must satisfy — it is **driver-config**,
not a code change to the engine.

## Result-shape contract (the normalization boundary)

A non-ClickUp driver MUST return exactly these shapes (what `ClickUpTracker`
returns today, verified by `tests/test_intake_clickup.py` /
`test_field_sync.py` / `test_attribution.py`):

| Method | Shape |
| --- | --- |
| `get_task` / `list_tasks` (per ticket) | `{id, name, url, list_id, description, archived?, missing?}` |
| `comments` (per comment) | `{id, text, date (epoch seconds), user_id, username}` |
| `task_fields` | `{lowercased field name: value}` |

The comment `user_id` / `username` fields are **load-bearing**: A1 answer
attribution matches a gate reply's commenter to a CtrLoop user by these. A Jira
driver maps them from `author.accountId` / `author.displayName`.

Failure/absence sentinels are uniform (`None` for get/list/create, `False` for
writes, `[]` for comments, `{}` for fields, silent no-op for setters) so an
outage and the scaffold look identical to callers — best-effort, never raising.

## Concept map

| ClickUp | Jira | Notes |
| --- | --- | --- |
| List (`clickup_list_id`) | Project / board | `list_tasks(list_id)` → JQL `project = X AND status != Done`. The per-workspace `list_id` is already a call argument, so routing is untouched. |
| Task | Issue | `get_task` → `GET /rest/api/3/issue/{key}`. |
| Subtask (artifact mirror) | Sub-task | `create_task(parent=…)` → issue with `fields.parent`. Sub-task created in the parent's project. |
| Custom field (Stage, Backend/Web/App PR, Decisions, Dashboard, Metric) | Jira custom field / workflow transition | `field_set`/`field_append` address fields by NAME; Jira resolves the name→`customfield_NNNNN` id at `load_fields`. Dropdown option-name→id resolution maps to Jira option ids. |
| Status candidate list (`STATUS_CANDIDATES`) | Workflow transition | ClickUp sets a status string; Jira POSTs a transition id. The candidate-name→transition mapping is per-project workflow config. |
| Comment | Comment | `comment` → `POST /issue/{key}/comment` (ADF body); `comments` reads `GET /issue/{key}/comment`. |
| Assignee (numeric user id) | `accountId` | `set_assignee` → `PUT /issue/{key}/assignee {accountId}`. ClickUp's numeric id maps to Jira's opaque accountId — a driver needs a user-id translation table. |
| Markdown description | Jira wiki / ADF | `update_description` and the markdown ticket bodies round-trip through ADF. The artifact-sync fixpoint (semantic-hash of the readback) still holds as long as the ADF↔markdown conversion is deterministic — a driver MUST normalize consistently or human-edit detection breaks. |

## No clean Jira analogue

- **The gumo-speed conveyor custom-field contract** (named Stage/PR/Decisions
  fields with specific dropdown options, `clickup_stage_field_map`, etc.) is a
  ClickUp-schema convention. On Jira it becomes project workflow + custom-field
  configuration the operator provisions once — **driver-config, not code**. The
  engine only needs the by-name `field_set`/`field_append`/`task_fields` surface
  to resolve.
- **The `@sentry review` PR loop** is unrelated to the tracker (it lives in the
  VCS seam, H2) and is out of scope here.

## Enabling a real driver

1. Implement `class JiraTracker(Tracker)` against the shapes above.
2. Set `enabled` from real credential presence.
3. Add `jira` handling in `tracker_for` (already routed) and set
   `TRACKER_PROVIDER=jira`. Per-workspace tracker credentials remain a
   documented future (the SCAFFOLD threads only the instance-level provider).
