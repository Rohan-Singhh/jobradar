"""The public website: pages for people and search engines, plus a hosted copy
of the tool where each visitor brings their own API key and resume.

Nothing a visitor sends is stored. Their key, resume summary and scores live
in their own browser. The key passes through here only to reach the AI
provider, inside one request, and is never written to disk or logged. Job
listings are public data, fetched from the company boards and kept in
memory for a few hours.

Run with:  uvicorn jobradar.web.hosted:app
Set SITE_URL (e.g. https://jobradar.dev) so canonical links and the sitemap
name the real domain, and GOOGLE_SITE_VERIFICATION for Search Console.
"""
from __future__ import annotations

import html
import os
import re
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware

from .. import sources
from ..llm import ChatScorer, RateLimited
from ..main import ROOT, passes_filters
from ..relevance import apply_cap as apply_relevance_cap
from ..resume import build_details, build_profile, text_from_upload
from ..seniority import apply_cap

WEB = Path(__file__).parent
templates = Jinja2Templates(directory=str(WEB / "templates"))
ASSET_VERSION = "4"                      # bump with any CSS/JS change; assets cache for a year

AUTHOR = {"name": "Rohan Singh", "path": "/author/rohan-singh",
          "github": "https://github.com/Rohan-Singhh",
          "linkedin": "https://www.linkedin.com/in/rohan840"}
SOURCE_URL = "https://github.com/Rohan-Singhh/jobradar"

# Visitors may only point the server at these providers. Anything else, a
# localhost Ollama say, would let a stranger use the site to reach hosts it
# can see and they cannot.
PROVIDERS = ("xkiro", "openrouter")
MODEL_RE = re.compile(r"^[\w.:/-]{1,100}$")
DEFAULT_MODEL = "mistralai/mistral-large-2512"
MAX_RESUME = 5 * 1024 * 1024
JOBS_TTL = 6 * 3600


def _config() -> dict:
    """The tracked company list and title filters. config.yaml is personal
    and gitignored, so the site reads the example that ships with the code."""
    path = Path(os.environ.get("HOSTED_CONFIG", ROOT / "config.example.yaml"))
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# --- job listings, shared by every visitor ------------------------------------
_jobs: dict = {"at": 0.0, "jobs": [], "failed": []}
_jobs_lock = threading.Lock()
_refreshing = threading.Event()


def _plain(raw: str, limit: int = 600) -> str:
    """Board descriptions arrive as HTML, as HTML escaped once more
    (Greenhouse), or as text. The scorer and search only need the words."""
    text = html.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    return text[:limit]


def _fetch_all() -> dict:
    cfg = _config()
    companies = cfg.get("companies", [])
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(sources.fetch_company, companies))
    jobs, failed, seen = [], [], set()
    for entry, (found, err) in zip(companies, results):
        if err:
            failed.append(entry["name"])
            continue
        for j in found:
            # A board can list one posting twice; the id is company+title+url.
            if j["id"] in seen or not passes_filters(j, cfg.get("filters", {})):
                continue
            seen.add(j["id"])
            jobs.append({"id": j["id"], "company": j["company"], "title": j["title"],
                         "url": j["url"], "location": j["location"], "posted_at": j["posted_at"],
                         "domain": j.get("domain", ""), "desc": _plain(j.get("description"))})
    return {"at": time.time(), "jobs": jobs, "failed": failed}


def _refresh() -> None:
    try:
        fresh = _fetch_all()
        if fresh["jobs"]:                # a total outage keeps the last good list
            _jobs.update(fresh)
    finally:
        _refreshing.clear()


def jobs_snapshot() -> dict:
    """Fresh list if we have one; a stale list at once while a new one is
    fetched behind it; and only on a cold start does a visitor wait."""
    if _jobs["jobs"] and time.time() - _jobs["at"] > JOBS_TTL and not _refreshing.is_set():
        _refreshing.set()
        threading.Thread(target=_refresh, daemon=True).start()
    if not _jobs["jobs"]:
        with _jobs_lock:
            if not _jobs["jobs"]:
                _jobs.update(_fetch_all())
    return _jobs


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm the list in the background so the first visitor rarely waits.
    if os.environ.get("WARM_JOBS", "1") == "1":
        threading.Thread(target=jobs_snapshot, daemon=True).start()
    yield


app = FastAPI(title="Job Radar", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.mount("/static", StaticFiles(directory=str(WEB / "static")), name="static")

CSP = ("default-src 'self'; img-src 'self' data: https://www.google.com https://*.gstatic.com; "
       "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; "
       "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'")


@app.middleware("http")
async def headers(request: Request, call_next):
    resp = await call_next(request)
    h = resp.headers
    # The API key sits in this page's storage, so no script from anywhere
    # else may run on it.
    h.setdefault("Content-Security-Policy", CSP)
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if request.url.path.startswith("/static/"):
        h["Cache-Control"] = "public, max-age=31536000, immutable"   # URLs carry ?v=
    elif "Cache-Control" not in h:
        h["Cache-Control"] = "no-cache"
    return resp


# --- rate limits ------------------------------------------------------------------
LIMITS = {"jobs": (60, 600), "profile": (10, 600), "score": (120, 600)}
_hits: dict[tuple, deque] = defaultdict(deque)
_hits_lock = threading.Lock()


def _client(request: Request) -> str:
    # Behind Render's proxy the caller is the last address it appended; the
    # first entry is whatever the client chose to send.
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[-1].strip() if fwd else (request.client.host if request.client else "?")


def _limit(request: Request, bucket: str) -> None:
    n, window = LIMITS[bucket]
    now = time.time()
    with _hits_lock:
        q = _hits[(bucket, _client(request))]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= n:
            raise HTTPException(429, "Too many requests from here. Try again in a few minutes.")
        q.append(now)


def _checked_key(provider: str, model: str, key: str) -> str:
    if provider not in PROVIDERS:
        raise HTTPException(400, f"provider must be one of {', '.join(PROVIDERS)}")
    if not MODEL_RE.match(model or ""):
        raise HTTPException(400, "that model name doesn't look right")
    key = (key or "").strip()
    if not key or len(key) > 300 or any(c.isspace() for c in key):
        raise HTTPException(400, "add your API key")
    return key


def _llm_error(e: Exception) -> HTTPException:
    """Turn a provider failure into a status the browser can act on. The
    message comes from the provider's reply, never from the key."""
    if isinstance(e, RateLimited):
        return HTTPException(429, "The AI provider is rate-limiting this key. Wait a minute and scan again.")
    msg = str(e)
    if "rejected the API key" in msg:
        return HTTPException(401, "The AI provider rejected this API key.")
    if "not found" in msg:
        return HTTPException(400, msg[:200])
    return HTTPException(502, f"The AI provider failed: {msg[:160]}")


# --- API ----------------------------------------------------------------------------
@app.get("/api/jobs")
def api_jobs(request: Request):
    _limit(request, "jobs")
    snap = jobs_snapshot()
    if not snap["jobs"]:
        raise HTTPException(503, "Couldn't reach the job boards just now. Try again shortly.")
    return {"jobs": snap["jobs"], "fetched_at": snap["at"], "failed": snap["failed"]}


@app.post("/api/profile")
def api_profile(request: Request, resume: UploadFile = File(...), provider: str = Form("xkiro"),
                model: str = Form(DEFAULT_MODEL), x_api_key: str = Header("")):
    """Reads an uploaded resume in memory and returns its summary and details.
    The file is never written anywhere."""
    _limit(request, "profile")
    key = _checked_key(provider, model, x_api_key)
    data = resume.file.read(MAX_RESUME + 1)
    if len(data) > MAX_RESUME:
        raise HTTPException(413, "Keep the resume under 5 MB.")
    try:
        text = text_from_upload(data, resume.filename or "")
    except Exception:  # noqa: BLE001 - any parser failure means the same thing to the visitor
        raise HTTPException(400, "Couldn't read that file. Upload a PDF or a .txt.")
    if len(text.strip()) < 200:
        raise HTTPException(400, "Found almost no text in that file. Is it a scanned image?")
    scorer = ChatScorer(provider, model, key)
    try:
        summary = build_profile(text, scorer)
        details = build_details(text, scorer)
    except (RateLimited, RuntimeError) as e:
        raise _llm_error(e)
    return {"profile": summary, "details": details, "tokens": scorer.tokens_used}


class JobIn(BaseModel):
    id: str = Field(max_length=40)
    company: str = Field(max_length=120)
    title: str = Field(max_length=300)
    location: str = Field("", max_length=300)
    desc: str = Field("", max_length=1200)


class ScoreIn(BaseModel):
    provider: str = "xkiro"
    model: str = DEFAULT_MODEL
    profile: str = Field(min_length=20, max_length=6000)
    jobs: list[JobIn] = Field(min_length=1, max_length=25)


@app.post("/api/score")
def api_score(request: Request, body: ScoreIn, x_api_key: str = Header("")):
    """Scores up to 25 jobs against the visitor's profile with their key,
    and applies the same seniority and relevance caps as the CLI."""
    _limit(request, "score")
    key = _checked_key(body.provider, body.model, x_api_key)
    scorer = ChatScorer(body.provider, body.model, key)
    payload = [{"id": j.id, "company": j.company, "title": j.title,
                "location": j.location, "description": j.desc} for j in body.jobs]
    try:
        results = scorer.score_batch(body.profile, payload)
    except (RateLimited, RuntimeError) as e:
        raise _llm_error(e)
    titles = {j.id: j.title for j in body.jobs}
    out = {}
    for job_id, (score, reason, breakdown) in results.items():
        score, reason = apply_cap(score, titles.get(job_id, ""), reason)
        score, reason = apply_relevance_cap(score, breakdown, reason)
        out[job_id] = {"score": score, "reason": reason, "breakdown": breakdown}
    return {"results": out, "tokens": scorer.tokens_used}


# --- pages ----------------------------------------------------------------------------
FAQ = [
    ("What is Job Radar?",
     "A tool that reads every open role at a list of tech companies and scores each one from 0 to 10 "
     "against your resume, with a one-line reason, so you can spend your time on the jobs that fit."),
    ("Is it free?",
     "Yes. You bring your own API key, and xkiro's free tier covers it: the first scan uses roughly "
     "150,000 to 350,000 of its million free daily tokens, and later scans only score new roles."),
    ("Do you store my API key or resume?",
     "No. Both stay in your browser. Your key travels with each scoring request to the AI provider and "
     "is dropped when the reply comes back; the resume is read in memory and never saved. The app's "
     "Forget me button clears them from your browser as well."),
    ("Which companies does it track?",
     "Companies on Greenhouse, Lever, Ashby, Workday, Oracle Recruiting Cloud and Atlassian's own board, "
     "including Stripe, Notion, Databricks, Figma, Anthropic, Adobe, NVIDIA, Salesforce, Oracle and Dell. "
     "Run it yourself to track any company you like."),
    ("How is a job scored?",
     "A language model rates skills, seniority, location, field, recency and reach, then gives an "
     "overall score. Two rules are enforced in code on top: senior titles and roles weak on both "
     "skills and field are capped at 3."),
    ("Can it email me when a job opens?",
     "When you run it on your own computer, yes: a daily alert for the companies you care about and a "
     "weekly digest. The hosted site keeps nothing, so it has nothing to email from."),
    ("Does it apply to jobs for me?",
     "No. It finds and ranks openings; you open the posting and apply yourself."),
]

PAGES = {
    "/": {"template": "home.html", "title": "Job Radar: open jobs ranked against your resume",
          "description": "Job Radar reads every open role at Stripe, Notion, Databricks, Adobe and more, "
                         "and scores each one 0 to 10 against your resume. Free, with your own API key.",
          "crumbs": [("Home", "/")], "priority": "1.0"},
    "/app": {"template": "app.html", "title": "Job Radar app: score open jobs against your resume",
             "description": "Add your resume and a free API key, press Scan, and see every open role "
                            "at the tracked companies scored 0 to 10 with the reason.",
             "crumbs": [("Home", "/"), ("App", "/app")], "priority": "0.9"},
    "/setup": {"template": "setup.html", "title": "Run Job Radar yourself: setup for Windows and Mac",
               "description": "Install Job Radar on Windows or macOS to get daily email alerts and a "
                              "weekly digest of new roles that match your resume.",
               "crumbs": [("Home", "/"), ("Run it yourself", "/setup")], "priority": "0.7"},
    AUTHOR["path"]: {"template": "author.html", "og_type": "profile",
                     "title": f"{AUTHOR['name']}: backend engineer and maker of Job Radar",
                     "description": f"{AUTHOR['name']} builds backend systems and made Job Radar, "
                                    "a tool that ranks open jobs against your resume.",
                     "crumbs": [("Home", "/"), ("Author", AUTHOR["path"])], "priority": "0.5"},
}


def _site(request: Request) -> dict:
    url = (os.environ.get("SITE_URL") or str(request.base_url)).rstrip("/")
    return {"url": url, "author": AUTHOR, "source": SOURCE_URL,
            "verification": os.environ.get("GOOGLE_SITE_VERIFICATION", "")}


def _jsonld(path: str, site: dict) -> list[dict]:
    url = site["url"]
    person = {"@type": "Person", "name": AUTHOR["name"], "url": url + AUTHOR["path"],
              "sameAs": [AUTHOR["github"], AUTHOR["linkedin"]]}
    out = []
    crumbs = PAGES[path]["crumbs"]
    if len(crumbs) > 1:
        out.append({"@context": "https://schema.org", "@type": "BreadcrumbList",
                    "itemListElement": [{"@type": "ListItem", "position": i + 1, "name": name,
                                         "item": url + p} for i, (name, p) in enumerate(crumbs)]})
    if path in ("/", "/app"):
        out.append({"@context": "https://schema.org", "@type": "SoftwareApplication",
                    "name": "Job Radar", "url": url + "/app", "applicationCategory": "BusinessApplication",
                    "operatingSystem": "Web, Windows, macOS",
                    "offers": {"@type": "Offer", "price": "0", "priceCurrency": "USD"},
                    "author": person})
    if path == "/":
        out.append({"@context": "https://schema.org", "@type": "FAQPage",
                    "mainEntity": [{"@type": "Question", "name": q,
                                    "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in FAQ]})
    if path == AUTHOR["path"]:
        out.append({"@context": "https://schema.org", "@type": "ProfilePage",
                    "mainEntity": {**person, "jobTitle": "Backend Engineer"}})
    return out


def _render(request: Request, path: str) -> HTMLResponse:
    meta = PAGES[path]
    site = _site(request)
    page = {**meta, "path": "" if path == "/" else path,
            "crumbs": [{"name": n, "path": p} for n, p in meta["crumbs"]],
            "jsonld": _jsonld(path, site)}
    ctx = {"page": page, "site": site, "v": ASSET_VERSION, "hosted": True,
           "faq": [{"q": q, "a": a} for q, a in FAQ]}
    return templates.TemplateResponse(request, meta["template"], ctx)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return _render(request, "/")


@app.get("/app", response_class=HTMLResponse)
def tool(request: Request):
    return _render(request, "/app")


@app.get("/setup", response_class=HTMLResponse)
def setup(request: Request):
    return _render(request, "/setup")


@app.get(AUTHOR["path"], response_class=HTMLResponse)
def author(request: Request):
    return _render(request, AUTHOR["path"])


# The pages only change when the code does, so the deploy time is their date.
_DEPLOYED = datetime.now(timezone.utc).date().isoformat()


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots(request: Request):
    return f"User-agent: *\nAllow: /\n\nSitemap: {_site(request)['url']}/sitemap.xml\n"


@app.get("/sitemap.xml")
def sitemap(request: Request):
    url = _site(request)["url"]
    items = "".join(
        f"  <url><loc>{url}{'' if p == '/' else p}</loc><lastmod>{_DEPLOYED}</lastmod>"
        f"<priority>{m['priority']}</priority></url>\n" for p, m in PAGES.items())
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + items + "</urlset>\n")
    return Response(xml, media_type="application/xml")


@app.exception_handler(StarletteHTTPException)
async def not_found(request: Request, exc: StarletteHTTPException):
    """A real 404 status with a page a person can use; JSON for the API."""
    if exc.status_code == 404 and not request.url.path.startswith("/api/"):
        site = _site(request)
        page = {"title": "Page not found · Job Radar", "path": request.url.path,
                "description": "This page doesn't exist on Job Radar.", "crumbs": [], "jsonld": []}
        return templates.TemplateResponse(
            request, "404.html", {"page": page, "site": site, "v": ASSET_VERSION,
                                  "missing": request.url.path}, status_code=404)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
