# Launching the public site

The public site is `jobradar.web.hosted`: a landing page, a setup guide, an
author page, and the app at `/app`, where each visitor brings their own API
key and resume. Nothing a visitor adds is stored on the server.

## Before it goes live

1. **Rewrite the drafts in your own words.** Every page marked
   `DRAFT` in `jobradar/web/templates/` was drafted from the repo and your
   resume. Rewrite them, and the FAQ answers in `jobradar/web/hosted.py`
   (`FAQ`). Fill in the "Why Job Radar" paragraph on the author page yourself.
2. **Check the author details** in `hosted.py` (`AUTHOR`): name, GitHub and
   LinkedIn.

## Deploy on Render

1. Merge this work into `main` and push.
2. Render dashboard: **New > Blueprint**, pick the `jobradar` repo. It reads
   `render.yaml`.
3. Set the environment variable `SITE_URL` to your domain, with `https://` and
   no trailing slash, for example `https://jobradar.dev`.
4. **Custom domain:** Settings > Custom Domains, add the bare domain. Render
   adds `www` and redirects it to the bare domain in one hop. At your domain
   registrar, add the DNS records Render shows you.

## Google Search Console

1. Go to https://search.google.com/search-console and add a **URL prefix**
   property for your domain.
2. Choose **HTML tag** verification. Copy only the `content="..."` value into
   Render as `GOOGLE_SITE_VERIFICATION`, wait for the redeploy, then click
   Verify.
3. **Sitemaps:** submit `sitemap.xml`.
4. **URL inspection:** inspect the home page and click **Request indexing**.
   Repeat for `/app`, `/setup` and the author page.

Indexing usually takes a few days to a couple of weeks.

## Check it

- https://pagespeed.web.dev with your domain: aim for green on mobile.
- https://search.google.com/test/rich-results on the home page: the FAQ and
  software app should be detected; on the author page, the profile.

## Getting links to it

Links from real places that people read, written by you:

- The GitHub repo's About box and README, linking to the site.
- A "Show HN" post on Hacker News.
- Product Hunt.
- Reddit: r/developersIndia, r/cscareerquestions (read each sub's rules on
  self-promotion first).
- A dev.to or Hashnode post on how the scoring works.
- Your LinkedIn.

Don't buy links; Google penalises them.
