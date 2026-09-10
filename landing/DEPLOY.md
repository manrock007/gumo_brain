# CtrLoop website — deploy guide

Static multi-page site. No build step, no server code, no dependencies.
Every page is plain HTML sharing `styles.css`. Just serve this folder.

## Files
- `index.html` + section pages: platform, agent-teams, work, industries,
  travel, retail, nbfc, government, company, insights (+ 2 insight posts)
- `styles.css` — shared styles (linked by every page)
- `logos/` — client logos (referenced by the pages)
- `team/` — founder photos
- `sitemap.xml`, `robots.txt` — SEO

## Deploy (pick one)
- Netlify / Vercel / Cloudflare Pages: drag-and-drop this folder, or point it
  at the repo. Framework preset: "None / static". Publish directory: root.
- AWS S3 + CloudFront: `aws s3 sync . s3://<bucket> --delete`; set
  `index.html` as index document.
- Nginx/Apache: copy this folder to the web root. Clean URLs optional.

## Before go-live
- Point the domain `ctrloop.ai` at the host.
- Replace `og.png` with a real 1200x630 social share image (referenced in
  each page's meta tags).
- Update the canonical/OG URLs if the domain differs.

## Early-access form (Book a demo)
Every "Book a demo" links to the `#contact` form on the home page (email +
optional phone). It submits via AJAX (fetch) to Formspree: `https://formspree.io/f/xojgddab`.
Submissions POST JSON `{email, phone, source}` and appear in the Formspree
inbox; the user stays on the page and sees an inline "Thanks" message. To
point it elsewhere, set `window.CTRLOOP_FORM_ENDPOINT` before the page scripts
run. If a submission fails, it falls back to a pre-filled email to
support@ctrloop.ai so no lead is lost.

## SEO, analytics & AEO (added)
Already wired into every page:
- Google Tag Manager `GTM-55WZFPGG` (head script + body noscript). GA4 is configured inside GTM.
- The access form pushes a `request_access` event to `dataLayer` on success (with `form_location` and `has_phone`) - mark it as a key event in GA4.
- `canonical`, `robots` (index, follow, max-image-preview:large), Open Graph + Twitter tags, and `og-image.png` (1200x630).
- Favicons: `favicon.ico`, `favicon.svg`, `apple-touch-icon.png` (upload all to the site root).
- Structured data on the home page: Organization, WebSite, SoftwareApplication and FAQPage (JSON-LD) - the FAQ block is also visible on the page for AEO.
- `llms.txt` (root) - a plain-text brief for AI answer engines (ChatGPT, Perplexity, Google AI).
- `sitemap.xml` + `robots.txt` at root; submit the sitemap in Google Search Console.

Go-live reminders (from the SEO setup guide):
1. Upload everything to S3, then invalidate CloudFront `/*` (cache is long-lived).
2. Search Console -> Sitemaps -> submit https://ctrloop.ai/sitemap.xml; then URL-inspect the homepage and Request indexing.
3. GA4 -> Admin -> Events -> mark `request_access` as a key event after the first test submit.
4. Replace the placeholder brand `og-image.png` / favicons here with final artwork if you have them.

## Deploying on AWS Amplify Hosting
The site is static, so nothing in the pages changes for Amplify — only how you ship.
Amplify serves files from the deploy root and **auto-invalidates its CDN on every
deploy**, so the manual "invalidate CloudFront /*" step does NOT apply here.

Option A — Manual deploy (fastest):
1. Amplify console → your app → "Deploy without Git provider" (or the app's manual
   deploy) → upload a zip whose ROOT contains `index.html` (this zip is already
   structured that way — index.html, styles.css, img/, logos/, favicon.ico… are all
   at the top level, not inside a subfolder).
2. Amplify publishes and refreshes its CDN automatically. Done.

Option B — Git-connected (CI):
1. Commit these files to the repo root and connect the branch in Amplify.
2. `amplify.yml` (included) tells Amplify there is no build and to publish everything
   from root. Each push auto-deploys and auto-invalidates.

Notes for Amplify:
- Root files resolve correctly: `/favicon.ico`, `/robots.txt`, `/sitemap.xml`,
  `/llms.txt`, `/og-image.png`, `/img/...`. Keep them at the deploy root.
- Amplify sets correct Content-Types automatically (incl. .webp, .svg, .txt).
- HTTPS is automatic; map `ctrloop.ai` under Amplify → Domain management (point
  Route 53 / your DNS as Amplify instructs). Canonical stays `https://ctrloop.ai/`.
- GTM / GA4 / Search Console steps are host-agnostic — unchanged from above.
