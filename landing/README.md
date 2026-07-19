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

## Amplify only sees this folder

`../amplify.yml` is a **monorepo** spec with `appRoot: landing`, so:

- **Only `landing/` is published** — the engine code is never served (Amplify
  deploys only the artifacts baseDirectory).
- **Only `landing/` changes trigger a build** — set
  `AMPLIFY_MONOREPO_APP_ROOT=landing` on the Amplify app (and
  `AMPLIFY_DIFF_DEPLOY=true`) so engine commits don't rebuild the site.

If you ever want literal repo-level isolation, split this into a dedicated
`ctrloop-site` repo — but the monorepo config already keeps Amplify scoped to
this folder.
