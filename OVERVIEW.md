# Job Radar

A job tracker that runs on your Mac. It watches the companies you name, ranks
every opening against your resume, tells you the same day when something opens
at a company you care about, and emails a ranked digest every Monday.

It is not a job board. It is a *diff* — its real job is knowing what changed
since the last time it looked.

**Current state:** 56 companies, 3,381 open jobs, 83 expired and hidden.

---

## Contents

- [What it does](#what-it-does)
- [Architecture](#architecture)
- [How a job is tracked](#how-a-job-is-tracked)
- [How a job is scored](#how-a-job-is-scored)
- [Token economy](#token-economy)
- [Schedules](#schedules)
- [The web UI](#the-web-ui)
- [Configuration](#configuration)
- [Commands](#commands)
- [Testing](#testing)
- [Known limits](#known-limits)

---

## What it does

**Watches companies.** 56 employers across seven job-board platforms. Startups
(Stripe, Notion, Ramp, Anthropic) and enterprises (Adobe, Oracle, Dell,
Fidelity, NVIDIA, Salesforce) need different adapters, which is why there are
seven rather than one.

**Detects change.** A posting it has never seen is *new*. A posting that
disappears from a board that fetched cleanly is *closed*. A posting that
reappears *reopens* rather than duplicating.

**Ranks against your resume.** Every job gets 0–10 plus six sub-scores —
skills, seniority, location, domain, recency, reach — and a one-line reason.

**Tells you.** Daily alerts for a shortlist of companies, by email and a macOS
banner. A ranked digest every Monday. A searchable web UI any time.

---

## Architecture

```
  config.yaml ──┐
                ├─► sources.py ──► filters ──► store.py (SQLite)
  resume.pdf ───┘   7 adapters      title/       ▲   the diff lives here:
                                    location     │   new? still open? gone?
                                                 │
                    localmatch.py ───────────────┤  free triage: settle the
                    (keyword match)              │  hopeless ones locally
                                                 │
                    batch.py ────────────────────┤  25 jobs per request
                      │                          │
                      ├─ llm.py ── xkiro / OpenRouter / Ollama
                      ├─ cursor.py ── Cursor Cloud Agents
                      └─ jev.py ── TypeSafe System One
                                                 │
                    seniority.py ────────────────┤  hard cap, enforced in code
                    budget.py ───────────────────┤  daily token ceiling
                                                 │
                                                 ▼
         ┌───────────────────────┬───────────────────────┐
         │   digest.py           │   alerts.py           │   web/server.py
         │   weekly email        │   same-day email      │   live UI + search
         │                       │   + macOS banner      │
         └───────────────────────┴───────────────────────┘
```

### Modules

| File | Responsibility |
|---|---|
| `sources.py` | Seven board adapters; normalises every posting to one shape |
| `store.py` | SQLite. The diff, notification state, schema migrations |
| `resume.py` | PDF → text → structured profile (model, or locally) |
| `localmatch.py` | Keyword matcher. No key, no network, instant |
| `batch.py` | Shared prompt and parser for every model backend |
| `llm.py` | OpenAI-shaped providers, rate limiting, pacing |
| `cursor.py` / `jev.py` | Alternative backends behind the same interface |
| `seniority.py` | Title rule capping senior roles — deterministic, free |
| `relevance.py` | Caps roles weak on both skills and domain — the wrong-field gate |
| `budget.py` | Daily token ceiling, recorded per batch |
| `digest.py` | Weekly HTML email |
| `alerts.py` | Same-day email + macOS notification |
| `web/` | FastAPI server, SSE live scan, search, static UI |
| `main.py` | CLI and the scan pipeline |

~2,250 lines of Python.

### Board adapters

| Platform | Used by | API shape |
|---|---|---|
| `greenhouse` | Stripe, Anthropic, Databricks (29) | public JSON |
| `ashby` | Notion, Ramp, OpenAI (12) | public JSON |
| `workday` | Adobe, NVIDIA, Fidelity, Dell (12) | POST `/wday/cxs/…` |
| `oracle` | Oracle, Dell | Recruiting Cloud REST |
| `lever` | Spotify | public JSON |
| `workable` | — | widget API |
| `atlassian` | Atlassian | its own JSON feed |

Enterprises are almost never on Greenhouse. Workday and Oracle Recruiting Cloud
between them cover most large employers, which is why both exist.

---

## How a job is tracked

Every posting gets a stable id: `sha1(company | title | url)`.

| Condition | Meaning |
|---|---|
| id not in the database | **new** — reported |
| id present, still on the board | `last_seen` bumped, stays quiet |
| id present, absent from a board that fetched **successfully** | **closed** |
| id previously closed, back on the board | **reopens**, not duplicated |

That "fetched successfully" qualifier matters. Without it, one network blip
would report your entire watchlist as closed.

Expired jobs stay in the database — that is how a repost is recognised — but
are excluded from the grid, search, digest and alerts.

**Two independent queues.** `notified` (weekly digest) and `alerted` (daily
alerts) are separate columns, so the two channels never consume each other's
work.

---

