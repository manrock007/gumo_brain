# gumo_brain → CtrlLoop rename: the coordinated infra checklist

The application is fully rebranded in code (UI, gate-comment prefix, git
author, docs). The pieces BELOW live outside this repo or break deploys if
flipped alone — do them together, in this order, in one maintenance window.

## 1. Before deploying this version

**REQUIRED for pre-existing instances (the neutral-defaults upgrade).** The
engine's code defaults are now neutral ("built for the world"): every value
that used to be baked in for the original Gumo instance is empty unless
configured. These values exist ONLY as code defaults today — they are not in
`app_config` and not in the workspaces tables — so deploying without setting
them as env vars silently disables the integrations that relied on them.
Before deploying to an instance that uses them, set:

| Env var | Old baked-in default | Disabled if unset |
| --- | --- | --- |
| `SENTRY_ORG` | `gumo` | the ENTIRE Sentry lane: webhook intake errors, sweep exits, `/api/trigger` 400s, `[sentry]` tickets reject |
| `SENTRY_API_BASE` | `https://de.sentry.io/api/0` (EU) | new default is the US host — EU orgs MUST set this |
| `CLICKUP_LIST_ID` | `901615853762` | the ENTIRE ClickUp integration (`clickup_enabled` needs token + list), including intake, gate-answer polling and mirroring — unless every workspace has its own list |
| `PUBLIC_BASE_URL` | `https://gumo.co.in/brain` | dashboard deep links in Slack nudges and the `Dashboard` ClickUp field |
| `PRODUCT_NAME` / `BUSINESS_CONTEXT` | the Gumo identity/context | prompts fall back to "your product" / no business block (unless already overridden via `PUT /api/context`, which wins anyway) |
| `MEMORY_CANONICAL_PROJECT` | `gumo` | instance-level product-scope fallback for legacy unmapped slugs (workspaces' own canonical is unaffected) |
| `CLICKUP_STAGE_FIELD_MAP`, `CLICKUP_REPO_STAGE_MAP`, `CLICKUP_PR_FIELD_MAP`, `CLICKUP_DOC_FIELD_MAP`, `CLICKUP_FOLDER_FIELD`, `CLICKUP_FRICTION_FIELD`, `CLICKUP_FLAG_FIELD`, `CLICKUP_METRIC_FIELD` | the gumo-speed conveyor maps/fields | the conveyor mirror (Stage board, PR fields, doc/folder links, friction log, launch fields) goes inert |

The old values are reproduced verbatim in the
"Example configuration: the original Gumo instance" appendix of
[docs/OPERATIONS.md](docs/OPERATIONS.md). Startup logs a loud warning for
half-configured integrations (token without org, ClickUp token without any
list id).

Everything else in the upgrade is backward compatible:

- **Credentials**: if only `DASHBOARD_PASSWORD` is set, first boot creates an
  admin account `gumo` with that password — sign-in keeps working. Prefer
  setting `CTRLLOOP_ADMIN_USER` / `CTRLLOOP_ADMIN_PASSWORD` for a named admin.
- **ClickUp threads**: engine comments now start `**[ctrlloop]**`; the poller
  recognizes the old `**[gumo_brain]**` prefix too, so jobs parked across the
  upgrade cannot misread old engine comments as human answers.
- **Git author**: engine commits are now authored `ctrlloop <engine@ctrlloop.local>`
  (override with `CTRLLOOP_GIT_NAME` / `CTRLLOOP_GIT_EMAIL`).

## 2. GitHub repo rename (your click)

Rename `manrock007/gumo_brain` → `manrock007/ctrlloop` in GitHub settings.
GitHub redirects the old URL for clones, pushes and API calls indefinitely,
so nothing breaks immediately — but update references at your leisure:

- gumoiac Ansible/compose references to the repo URL.
- Any local clones (`git remote set-url`).

## 3. gumoiac / server (edit together with the next deploy)

- docker-compose service `gumo-brain` → `ctrlloop` (or keep the name; it is
  purely cosmetic — but if you rename it, update
  `.github/workflows/build-and-push.yml` lines that run
  `docker compose pull/up/logs gumo-brain` to match, in the same change).
- ECR repository: `ECR_BRAIN_REPO` secret can keep pointing at the existing
  registry repo; create a new `ctrlloop` ECR repo only if you want the image
  name to match (then update the secret).
- nginx: the `/brain/` path prefix can stay (it's config, not branding);
  if you move it to `/ctrlloop/`, set `PUBLIC_BASE_URL` accordingly.
- AWS Secrets Manager secret `gumo/brain` may keep its name; only its
  contents matter.

## 4. After the window

- Sentry internal-integration webhook URL: unchanged unless the nginx path
  moved.
- Sanity check: sign in, submit a request, confirm the ClickUp comment prefix
  reads `**[ctrlloop]**`, and confirm `docker logs` shows the service healthy.
