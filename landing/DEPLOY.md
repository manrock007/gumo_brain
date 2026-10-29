# CtrLoop website - deploy guide

Static multi-page site. No build step, no server code, no dependencies.
See `README.md` for the page map. Serve this folder as the web root.

## Hosting (AWS Amplify, connected to this repo)

Amplify publishes `landing/` on every push to `main` (spec: `../amplify.yml`)
and invalidates its CDN automatically.

1. **Redirects / clean URLs** - Amplify does not read `_redirects`. Open the
   app -> *Hosting* -> *Rewrites and redirects* -> *Open text editor* and paste
   `amplify-redirects.json` (or run
   `aws amplify update-app --app-id <id> --custom-rules file://amplify-redirects.json`).
   It contains:
   - 301s from every old URL in the previous live sitemap (`/platform.html`,
     `/agent-teams.html`, `/work.html`, `/industries.html`, `/travel.html`,
     `/retail.html`, `/nbfc.html`, `/government.html`, `/company.html`,
     `/careers.html`) plus the paths listed in the handoff `_redirects`.
   - 200 rewrites so `/platform`, `/crew/bea`, `/industries/...`, `/company`
     and `/careers` resolve to their `index.html` without a trailing slash.
   - `/insights.html` and the two posts stay live (they have published posts).
2. **Preview** - enable *Previews* (or connect the feature branch) in Amplify
   to get a preview URL for sign-off before merging to `main`.
3. **Domain** - `ctrloop.ai` stays mapped under *Domain management*; canonical
   URLs are `https://ctrloop.ai/...` with no trailing slash.

## After go-live

- Search Console (shakir@ctrloop.ai): submit `https://ctrloop.ai/sitemap.xml`,
  then URL-inspect `/` and request indexing.
- Share cards: run `https://ctrloop.ai/` through the LinkedIn Post Inspector
  and the Facebook Sharing Debugger ("Scrape again") to drop the old preview.
- Rich results: run each of the 9 URLs through the Google Rich Results Test
  (expects Organization, BreadcrumbList and FAQPage with no errors).
- Lighthouse mobile: re-run on the live URLs (local runs are skewed by the
  sandbox network; GTM/GA4 and the always-on animations dominate the score).

## Book a demo form

The home-page form (`#contact`) posts JSON `{email, phone, source}` to
Formspree `https://formspree.io/f/xojgddab` via `fetch`, shows an inline
"Thanks" message and pushes a `request_access` event to `dataLayer`
(`form_location`, `has_phone`) for GA4. If the POST fails it falls back to a
pre-filled email to support@ctrloop.ai. Override the endpoint by setting
`window.CTRLOOP_FORM_ENDPOINT` before the page scripts run. All other
"Book a demo" / "Hire an AI worker" buttons link to `/#contact`.

## Analytics & SEO (already wired)

- Google Tag Manager `GTM-55WZFPGG` in every `<head>` plus the `<noscript>`
  frame right after `<body>` on all pages, careers and job pages included.
  GA4 `G-XCXSL3Z70K` fires via GTM.
- Canonical, robots, Open Graph + Twitter tags and the 1200x630 share image
  (`/assets/og-image.png`) on every page.
- Favicons: `/favicon.ico`, `/assets/ctrloop-mark.svg`, `/assets/favicon-32.png`,
  `/assets/apple-touch-icon.png`, `/site.webmanifest` (192/512 icons).
- JSON-LD per page: Organization (founders, LinkedIn `sameAs`, offices),
  WebSite, WebPage/AboutPage, BreadcrumbList, FAQPage.
- `llms.txt`, `sitemap.xml`, `robots.txt` at the root.
