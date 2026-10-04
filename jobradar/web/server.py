"""Web UI for jobradar.

Serves a single page that shows every tracked job as a tile, tinted by how well
it matches the resume, and streams a live scan over Server-Sent Events so tiles
light up as they are scored.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import sources
from ..cursor import CursorError
from ..llm import RateLimited
from ..main import ROOT, get_profile, get_scorer, load_config, passes_filters
from ..resume import build_details, extract_text, parse_details
from ..store import Store

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="Job Radar")
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

_state = {"scanning": False, "started": 0.0}
_lock = threading.Lock()


def cfg():
    return load_config(ROOT / "config.yaml")


def store():
    return Store(ROOT / "jobradar.db")


@app.get("/")
def index():
    return FileResponse(str(STATIC / "index.html"))


@app.get("/api/profile")
def profile():
    c = cfg()
    cache = ROOT / ".resume_details.json"
    if cache.exists():
        cached = json.loads(cache.read_text())
        if cached.get("name"):          # only a real parse is worth keeping
            return cached
    try:
        text = extract_text(ROOT / c["resume_path"])
    except Exception as e:  # noqa: BLE001 - no resume at all
        return {"name": "", "headline": "", "location": "", "experience": [],
                "error": f"{type(e).__name__}: {e}"[:200]}

    # Reading a name off a resume does not need a model. Parse locally first so
    # the panel is populated with scoring off, then let a model refine it.
    details = parse_details(text)
    try:
        scorer = get_scorer(c)
        if scorer is not None and hasattr(scorer, "complete"):
            better = build_details(text, scorer)
            if better.get("name"):
                details = better
    except Exception as e:  # noqa: BLE001 - keep the local parse, note why
        details["error"] = f"{type(e).__name__}: {e}"[:200]
    if details.get("name"):
        cache.write_text(json.dumps(details))
    return details


@app.get("/api/state")
def state():
    c, s = cfg(), store()
    rows = s.db.execute(
        """SELECT id, company, title, url, location, score, reason, domain, source,
                  breakdown, first_seen, closed_at
           FROM jobs
           WHERE closed_at IS NULL          -- an expired posting is not a job
           ORDER BY score DESC NULLS LAST, company, title"""
    ).fetchall()
    jobs = _mosaic([dict(r) for r in rows], limit=4000)
    scored = [j for j in jobs if j["score"] is not None]
    threshold = c.get("llm", {}).get("min_score", 6)
    return {
        "jobs": jobs,
        "companies": [e["name"] for e in c.get("companies", [])],
        "stats": {
            "checked": len(scored),
            "total": len(jobs),
            "closed": s.db.execute(
                "SELECT COUNT(*) FROM jobs WHERE closed_at IS NOT NULL").fetchone()[0],
            "would_hire": len([j for j in scored if j["score"] >= threshold]),
            "avg": round(sum(j["score"] for j in scored) / len(scored) / 10, 2) if scored else 0,
            "threshold": threshold,
        },
        "scanning": _state["scanning"],
        "scoring": c.get("llm", {}).get("provider", "none") not in ("none", None, ""),
    }


def _mosaic(jobs: list[dict], limit: int) -> list[dict]:
    """Round-robin the jobs across companies.

    Straight SQL order groups every Stripe role together, which reads as a block
    of identical logos. Dealing one job per company at a time gives the grid its
    mosaic look and puts every company on screen early. Scored jobs are dealt
    first so results stay visible near the top.
    """
    from collections import OrderedDict, deque

    buckets: OrderedDict[str, deque] = OrderedDict()
    for j in sorted(jobs, key=lambda x: (x["score"] is None, -(x["score"] or 0))):
        buckets.setdefault(j["company"], deque()).append(j)

    out: list[dict] = []
    while buckets and len(out) < limit:
        for company in list(buckets):
            if not buckets[company]:
                del buckets[company]
                continue
            out.append(buckets[company].popleft())
            if len(out) >= limit:
                break
    return out


@app.get("/api/search")
def search(q: str = "", limit: int = 4000):
    """Full-text search across title, company, location and description.

    Descriptions are far too large to ship to the browser (thousands of jobs
    x several KB), so matching happens in SQLite and only the ids come back.
    Every word must match somewhere, so extra words narrow the result.
    """
    words = [w for w in q.lower().split() if w][:6]
    if not words:
        return {"ids": None}          # null means "no query", not "no matches"
    s = store()
    clauses = " AND ".join(
        "(LOWER(title) LIKE ? OR LOWER(company) LIKE ? OR LOWER(location) LIKE ?"
        " OR LOWER(COALESCE(description,'')) LIKE ?)" for _ in words)
    params: list[str] = []
    for w in words:
        params.extend([f"%{w}%"] * 4)
    rows = s.db.execute(
        f"SELECT id FROM jobs WHERE closed_at IS NULL AND {clauses} LIMIT ?",
        (*params, limit),
    ).fetchall()
    return {"ids": [r["id"] for r in rows]}
