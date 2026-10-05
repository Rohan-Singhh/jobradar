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

## How a job is scored

Two stages, deliberately.

### Stage 1 — free triage (currently **off**)

`localmatch.py` counts overlap between the skills named in your resume and each
posting, settling hopeless ones locally for zero tokens.

It is disabled by default because it scored real engineering roles too low on
title alone — "Software Engineer, Web Products" at Databricks was settled at
2/10; the model scored the same job **8/10**. With a 1M daily token budget and
a full board costing ~343K, there is no saving worth that trade.

Enable it with `triage: true` only if your budget is tight.

### Stage 2 — the model

Batches of 25 go to the configured backend. The reply is a positional array,
one row per job:

```
[ref, score, skills, seniority, location, domain, recency, reach, "reason"]
```

### The seniority gate

The prompt asks the model to cap senior roles. A prompt is a request, not a
guarantee — so `seniority.py` enforces it in code on every score written.

A title containing Senior, Sr, Staff, Principal, Lead, Head of, Director, VP,
Manager or Architect caps at **3/10**, however well the skills match. A junior
marker wins: "Senior Engineer Intern" is an internship.

This matters more than it sounds. Before it existed, senior roles held 31 of
the top 100 slots and the first junior role sat at position #67 — the jobs a
new graduate could actually get were buried beneath the ones they could not.

### The relevance gate

The mirror problem: a role in the wrong field riding a strong *seniority*
match. "Intern, Investor Center" scored 8/10 for a full-stack developer purely
because it is an internship and the candidate is a new graduate.

When **both** `skills` and `domain` fall below 60, the score caps at 3. Both
must be weak — a role that is unfamiliar in one dimension but strong in the
other still gets through. A missing or all-zero breakdown means no data, not a
bad fit, and never triggers the cap.

Effect at Fidelity, whose board is 96% non-technical: jobs scoring 6+ went from
17 to 1 — and that one is "Summer 2027 Undergraduate Internship — Software".

Both gates are arithmetic on data already stored, so they apply to existing
scores for free: `./run.sh recap`.

---

## Token economy

The free tier is a fixed daily allowance. Four mechanisms keep a full board
inside it.

| Mechanism | Effect |
|---|---|
| Free triage | Off by default — it cost accuracy for a saving the budget does not need |
| Batching, 25/request | Amortises per-call overhead (~550 tokens on xkiro) |
| Positional replies | ~70% fewer output *characters* than JSON objects (estimated from format, not measured per-token) |
| Cacheable system prefix | Byte-identical across batches. Whether the provider actually caches it is **unverified** |

Measured on a real 3,379-job scan: **601K tokens before these changes, 343K
after** — a 1.75x reduction, not the 9x an earlier projection suggested. That
projection assumed triage would settle most of the board; with descriptions
present it settles about 18%.

### The budget guard

`budget.py` records spend per calendar day in SQLite and is checked **before
every request**, not once per run. An upfront estimate plans the run; the
per-batch check is the real gate, so a batch costing more than expected can
never carry the run past the ceiling.

**40,000 tokens are reserved** so a digest, an alert or a profile rebuild can
still run after a heavy scan. A run that hits the ceiling stops cleanly and the
remaining jobs are picked up next time.

Tested against batches costing 5× the estimate — the worst case observed.

### Cost after the first run

The full-board scan is a one-off backfill. Later runs score only genuinely new
postings: **10–60 a week**, a few thousand tokens.

---

## Schedules

Two launchd agents, installed by `./install.sh`.

| Job | When | Scope |
|---|---|---|
| `com.jobradar.daily` | daily 09:30 | shortlist only, skips aggregators |
| `com.jobradar.weekly` | Mondays 09:00 | all 56 companies + aggregators |

The daily run deliberately does **not** do a full scan — it fetches only the
companies in `alerts.companies`. Seconds of work against half an hour.

### Alert delivery

- **Email** to the configured address, subject naming the role
- **macOS banner** at the same moment

Nothing is sent when nothing is new, so anything arriving means something
actually opened.

**First sighting is a baseline.** A newly added company has its whole board
looking "new", which would fire dozens of alerts for months-old postings.
Those are recorded silently; alerts begin from its next genuine opening. Each
company baselines on its own schedule.

---

## The web UI

```bash
./run-web.sh        # http://localhost:8765
```

- **Grid** — every open job as a tile, logo from its company domain, tinted
  from pale to deep green by match strength. Click to open the posting.
- **Mosaic ordering** — jobs are dealt round-robin across companies. SQL order
  would group every Stripe role into one block of identical logos.
- **Search** — server-side across title, company, location and description.
  Descriptions stay in SQLite; shipping thousands to the browser would be
  megabytes.
- **Filters** — minimum match, company, new-this-week.
- **Live scan** — Server-Sent Events. Tiles light up as each company is fetched
  and each job scored.
- **Match Made** — on completion the grid recedes and the five strongest
  matches step forward.
- **Profile panel** — name, headline and experience parsed from your resume,
  with the model's six sub-scores for whichever job is selected.

The profile panel works with **no model configured** — the local parser reads
the resume directly.

---

