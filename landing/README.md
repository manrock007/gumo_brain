# CtrLoop marketing site (ctrloop.ai)

The static marketing site served at **ctrloop.ai** via AWS Amplify. Plain
multi-page HTML, no build step, no framework. The October 2026 redesign
("AI workers" site: Bea, Otto, Dash) was ported from the Claude Design
handoff on the ClickUp build ticket; `DEPLOY.md` has the go-live checklist.

## Pages (clean URLs, one folder per page)

| URL | File |
|---|---|
| `/` | `index.html` |
| `/platform` | `platform/index.html` |
| `/crew/bea`, `/crew/otto`, `/crew/dash` | `crew/<name>/index.html` |
| `/industries/consumer-brands`, `/industries/travel-hospitality`, `/industries/financial-services` | `industries/<name>/index.html` |
| `/company` | `company/index.html` |
| `/careers` | `careers/index.html` (old template; GTM + canonical added) |
| `/jd-full-stack-engineer-{junior,senior}.html` | job pages (old template; OG + JobPosting JSON-LD, listed in the sitemap) |
| `/insights.html` + two posts | kept from the previous site (old template, `styles.css`, `img/`) |

Every redesigned page is self-contained: layout and colour are inline (as
exported from the design canvas), and `assets/site.css` carries the shared
keyframes, hover lift, `prefers-reduced-motion` and the <=640px mobile rules.
The `<head>` of each page is the ready-made SEO block from the handoff
(`seo/head/<page>.head.html`): title, meta, canonical, Open Graph/Twitter,
favicons, fonts, GTM and JSON-LD (Organization, WebSite, WebPage/AboutPage,
BreadcrumbList, FAQPage). If you change FAQ copy on a page, change the
FAQPage JSON-LD in the same file.

## Assets

- `assets/logos/*.webp` - client + partner logos (lossless WebP of the handoff PNGs, 120px tall).
- `assets/founders/founder-*.webp` - founder photos (600px square, from the handoff JPGs).
- `assets/crew/*.svg`, `assets/ctrloop-mark.svg` - static crew art and the loop mark.
- `assets/og-image-v2.png` (1200x630 share card; the old `og-image.png` stays only for the insights pages), `assets/logo.png`, favicon set
  (`/favicon.ico`, `assets/favicon-32.png`, `assets/apple-touch-icon.png`,
  `assets/icon-192.png`, `assets/icon-512.png`, `/site.webmanifest`).
- Root: `sitemap.xml`, `robots.txt`, `llms.txt`, `_redirects` (Cloudflare
  Pages format, reference), `amplify-redirects.json` (the same rules for Amplify).

## Deploy

Amplify is connected to this GitHub repo and auto-deploys on push to the
watched branch (`main`). The build spec is [`../amplify.yml`](../amplify.yml),
which publishes this `landing/` folder as the site root.

- **Edit** the pages, open a PR, merge to `main` -> Amplify builds and deploys
  automatically. Add any new page to `sitemap.xml` (and `llms.txt`).
- **Redirects** are not read from the repo by Amplify: paste
  `amplify-redirects.json` into the app's *Rewrites and redirects* editor (or
  `aws amplify update-app --custom-rules file://amplify-redirects.json`). It
  carries the 301s from every old `.html` URL and the rewrites that serve
  `/platform`, `/crew/bea`, ... without a trailing slash.
- No local build/tooling required; serve the folder with any static server
  that maps `/platform` to `platform/index.html` to preview clean URLs.

## Amplify only sees this folder

`../amplify.yml` is a **monorepo** spec with `appRoot: landing`, so:

- **Only `landing/` is published** - the engine code is never served (Amplify
  deploys only the artifacts baseDirectory).
- **Only `landing/` changes trigger a build** - set
  `AMPLIFY_MONOREPO_APP_ROOT=landing` on the Amplify app (and
  `AMPLIFY_DIFF_DEPLOY=true`) so engine commits don't rebuild the site.
