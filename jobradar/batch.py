"""Shared batch-scoring prompt and parser.

Three things drive the token bill, and this module addresses all three:

* **Per-call overhead.** Providers prepend their own preamble (xkiro adds ~550
  tokens), so one request per job would pay that thousands of times. Batching
  amortises it.
* **Output tokens.** They are billed like input and there is no cache discount.
  Repeating `"skills":`, `"seniority":` and friends for every job is pure
  waste, so replies are compact positional arrays, not objects.
* **The repeated prefix.** The instructions and the candidate profile are
  identical across every batch of a run. They go in a separate system message
  so providers that cache prompt prefixes can charge a tenth for them.
"""
from __future__ import annotations

import json
import re

KEYS = ("skills", "seniority", "location", "domain", "recency", "reach")

# Static across a whole run: identical bytes every batch, so it can be cached.
SYSTEM = """You score job postings for one candidate.

CANDIDATE:
{profile}

For each job output one array, positionally:
[ref, score, skills, seniority, location, domain, recency, reach, "reason"]

score 0-10. The six middle numbers are 0-100. reason is at most 8 words.

score 10 = perfect fit, 0 = wrong field.
Seniority is a hard gate, not a tiebreaker: a title containing Senior, Sr,
Staff, Principal, Lead, Manager, Head, Director or VP, or a stated requirement
of more years than the candidate has, scores at most 3 however well the skills
match. Set location at most 30 when the role is onsite somewhere they cannot
work.

Reply with ONLY a JSON array of these arrays. No prose, no keys, no markdown."""

USER = """Jobs, one per line as ref|company|title|location|description:
{jobs}"""


def build_messages(profile: str, jobs: list[dict]) -> list[dict]:
    """System holds everything reusable; user holds only what changes."""
    return [
        {"role": "system", "content": SYSTEM.format(profile=profile[:1400])},
        {"role": "user", "content": USER.format(jobs=_job_lines(jobs))},
    ]


def _job_lines(jobs: list[dict]) -> str:
    lines = []
    for i, j in enumerate(jobs):
        # Title, company and location carry most of the signal; the opening of
        # a description carries the rest.
        desc = " ".join((j.get("description") or "")[:240].split())
        lines.append(f"{i}|{j['company']}|{j['title']}|{j.get('location') or '?'}|{desc}")
    return "\n".join(lines)


def build_prompt(profile: str, jobs: list[dict]) -> str:
    """Single-string form, for backends that take one prompt (Cursor agents)."""
    msgs = build_messages(profile, jobs)
    return f"{msgs[0]['content']}\n\n{msgs[1]['content']}"
