"""Job board adapters. Every adapter returns a list of normalized dicts."""
from __future__ import annotations

import hashlib
import re
import time
from typing import Any

import requests

UA = {"User-Agent": "jobradar/0.1 (personal job tracker)"}
TIMEOUT = 25


def _job_id(company: str, title: str, url: str) -> str:
    return hashlib.sha1(f"{company}|{title}|{url}".encode()).hexdigest()[:16]


def _norm(company, title, url, location="", posted_at=None, description="", source="",
          domain=""):
    return {
        "id": _job_id(company, title, url),
        "company": company,
        "title": (title or "").strip(),
        "url": url,
        "location": (location or "").strip(),
        "posted_at": posted_at,          # epoch seconds, or None if the board hides it
        "description": (description or "")[:6000],
        "source": source,
        "domain": domain,
    }


def _get(url: str) -> Any:
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _epoch(value) -> float | None:
    """Boards return ms ints, ISO strings, or nothing. Normalize to epoch seconds."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e11 else float(value)
    try:
        from datetime import datetime
        s = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


# --------------------------------------------------------------------------
# Per-company boards
# --------------------------------------------------------------------------

def greenhouse(name: str, slug: str) -> list[dict]:
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    out = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        out.append(_norm(name, j.get("title"), j.get("absolute_url"), loc,
                         _epoch(j.get("updated_at") or j.get("first_published")),
                         j.get("content", ""), "greenhouse"))
    return out


def lever(name: str, slug: str) -> list[dict]:
    data = _get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    out = []
    for j in data:
        cats = j.get("categories") or {}
        out.append(_norm(name, j.get("text"), j.get("hostedUrl"),
                         cats.get("location", ""), _epoch(j.get("createdAt")),
                         j.get("descriptionPlain", ""), "lever"))
    return out


def ashby(name: str, slug: str) -> list[dict]:
    data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=false")
    out = []
    for j in data.get("jobs", []):
        out.append(_norm(name, j.get("title"), j.get("jobUrl"), j.get("location", ""),
                         _epoch(j.get("publishedAt")), j.get("descriptionPlain", ""), "ashby"))
    return out


def workable(name: str, slug: str) -> list[dict]:
    data = _get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true")
    out = []
    for j in data.get("jobs", []):
        out.append(_norm(name, j.get("title"), j.get("url" ) or j.get("application_url"),
                         j.get("location", ""), _epoch(j.get("published_on")),
                         j.get("description", ""), "workable"))
    return out


BOARDS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "workable": workable}


def fetch_company(entry: dict) -> tuple[list[dict], str | None]:
    """Returns (jobs, error). Never raises - one dead board must not kill the run."""
    board = entry.get("board", "greenhouse").lower()
    fn = BOARDS.get(board)
    if not fn:
        return [], f"unknown board '{board}'"
    try:
        jobs = fn(entry["name"], entry["slug"])
        for j in jobs:
            j["domain"] = entry.get("domain", "")
        return jobs, None
    except Exception as e:  # noqa: BLE001
        return [], f"{type(e).__name__}: {e}"
