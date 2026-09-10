# CtrLoop marketing site (ctrloop.ai)

The static marketing site served at **ctrloop.ai** via AWS Amplify. It is a
plain multi-page site — every page is hand-written HTML sharing `styles.css`,
with images under `img/`, `logos/` and `team/` — no build step, no framework.
`DEPLOY.md` (from the site export) documents the pages, the Formspree
"Book a demo" form, GTM/GA4, SEO/AEO files and the go-live checklist.

## Files

- `index.html` + section pages: `platform`, `agent-teams`, `work`,
  `industries` (+ `travel`, `retail`, `nbfc`, `government`), `company`,
  `insights` (+ two insight posts).
- `careers.html` + the two `jd-*.html` job pages (maintained in this repo;
  not part of the site export).
- `styles.css` — shared styles linked by every exported page.
- `sitemap.xml`, `robots.txt`, `llms.txt`, `og-image.png`, favicons — SEO/AEO
  files that must stay at the site root.

## Deploy

Amplify is connected to this GitHub repo and auto-deploys on push to the
watched branch (`main`). The build spec is [`../amplify.yml`](../amplify.yml),
which publishes this `landing/` folder as the site root. (The site export
ships its own root-level `amplify.yml`; it is intentionally not checked in
here because the repo-level monorepo spec already covers this folder.)

- **Edit** the pages, open a PR, merge to `main` → Amplify builds and deploys
  automatically. Add any new page to `sitemap.xml` (and `llms.txt`).
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
