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
