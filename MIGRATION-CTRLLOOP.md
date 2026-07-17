# gumo_brain → CtrlLoop rename: the coordinated infra checklist

The application is fully rebranded in code (UI, gate-comment prefix, git
author, docs). The pieces BELOW live outside this repo or break deploys if
flipped alone — do them together, in this order, in one maintenance window.

## 1. Before deploying this version

Nothing required. The app upgrade itself is backward compatible:

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
