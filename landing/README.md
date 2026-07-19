# CtrLoop landing page (ctrloop.ai)

The static marketing site served at **ctrloop.ai** via AWS Amplify. It is a
single self-contained `index.html` (external Google Fonts only) — no build step.

## Deploy

Amplify is connected to this GitHub repo and auto-deploys on push to the
watched branch (`main`). The build spec is [`../amplify.yml`](../amplify.yml),
which publishes this `landing/` folder as the site root.

- **Edit** `index.html`, open a PR, merge to `main` → Amplify builds and
  deploys automatically.
- No local build/tooling required; open `index.html` in a browser to preview.

> This lives in the engine repo by request. Because Amplify watches `main`,
> every merge triggers a (fast, no-op) build. To rebuild only when this folder
> changes, configure the Amplify app as a monorepo with app root `landing/`
> (`AMPLIFY_MONOREPO_APP_ROOT=landing` + `AMPLIFY_DIFF_DEPLOY`), or set an
> "Ignore build" command in the Amplify console.
