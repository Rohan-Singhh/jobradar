# Job Radar

Watches company job boards, scores every opening against your resume, and
emails you when something worth applying to shows up.

![grid](docs/grid.png)

It tracks 56 companies across seven ATS platforms — Greenhouse, Lever, Ashby,
Workable, Workday, Oracle Recruiting Cloud — which is what it takes to cover
both startups and large enterprises on one list.

## What it does

- **Finds new openings.** Every job gets a stable id, so "new" means new.
- **Notices when jobs close.** A posting that disappears is marked dead and hidden.
- **Ranks against your resume.** 0–10 with a reason, from an LLM that reads the
  job description.
- **Daily alerts** for a shortlist of companies — email plus a desktop notification.
- **Weekly digest** every Monday, ranked.
- **A local web UI** to search and filter everything.

![email](docs/email.png)

## Why it isn't just keyword matching

Two rules run in code, not in the prompt, because a model asked nicely will
comply inconsistently:

**Seniority.** A Senior/Staff/Principal title caps at 3/10 however well the
skills match. Without this, senior roles held 31 of the top 100 slots and the
first junior role sat at position #67 — the jobs a new graduate could actually
get were buried under the ones they could not.

**Relevance.** A role weak on both skills and domain caps at 3/10. Otherwise a
branch-office internship scores 8/10 for a developer purely because it is an
internship.

Both are arithmetic on data already stored, so `recap` re-applies them for free.

