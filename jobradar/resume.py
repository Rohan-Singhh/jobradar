"""Turn a resume file into plain text, then into a compact profile the LLM can
reuse for every job without re-reading the whole document each time."""
from __future__ import annotations

import re
from pathlib import Path


def extract_text(path: str | Path) -> str:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"resume not found: {p}")
    if p.suffix.lower() == ".pdf":
        return _pdf_text(str(p))
    return p.read_text(encoding="utf-8", errors="replace")


def text_from_upload(data: bytes, filename: str) -> str:
    """The same, for a file that only exists in memory: the hosted site reads
    an uploaded resume without ever writing it to disk."""
    if filename.lower().endswith(".pdf") or data[:5] == b"%PDF-":
        import io
        return _pdf_text(io.BytesIO(data))
    return data.decode("utf-8", errors="replace")


def _pdf_text(source) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise RuntimeError("pip install pypdf to read PDF resumes") from e
    return "\n".join(page.extract_text() or "" for page in PdfReader(source).pages)


PROFILE_PROMPT = """Summarize this resume into a hiring profile of at most 180 words.

START with one sentence stating, explicitly:
  - total years of PAID professional experience (count internships as partial,
    and say so; coursework and personal projects are not experience)
  - the seniority band this supports: intern / new-grad / junior / mid / senior
  - whether the person is still studying, and their graduation year
Then cover primary languages and frameworks, domains worked in, and
location plus work authorization if stated.

Be accurate about level even when the projects are impressive - a strong
portfolio does not make a new graduate a senior engineer.
Write plain prose. No preamble, no markdown headings.

RESUME:
{text}"""


def build_profile(text: str, llm) -> str:
    """One LLM call at startup; the result is cached to disk by the caller."""
    if llm is None:
        return text[:2500]
    return llm.complete(PROFILE_PROMPT.format(text=text[:12000]), max_tokens=400).strip()


DETAILS_PROMPT = """Extract structured facts from this resume.
Reply with ONLY a JSON object, no markdown fence:
{{"name": "...", "headline": "<role, max 5 words>", "location": "<city, country>",
  "experience": [{{"org": "...", "role": "...", "years": "<e.g. 2024 - 2026>"}}],
  "education": [{{"org": "<institution>", "role": "<degree>", "years": "..."}}],
  "skills": ["..."]}}
experience: paid work and internships only, newest first, max 5.
education: degrees and diplomas only. Leave out secondary school (10th, 12th,
high school). Max 3.
skills: the 12 technologies most central to this person's work, most important
first, each written the usual way (e.g. "PostgreSQL", "React").

RESUME:
{text}"""

EMPTY_DETAILS = {"name": "", "headline": "", "location": "", "experience": [],
                 "education": [], "skills": [], "links": [], "error": ""}


def build_details(text: str, llm) -> dict:
    """Structured fields for the web UI's profile panel."""
    import json
    if llm is None:
        return {**EMPTY_DETAILS, "error": "no model configured"}
    # Errors are returned, not swallowed: the caller needs to know whether this
    # is a real empty result (cacheable) or a failure (must be retried).
    raw = llm.complete(DETAILS_PROMPT.format(text=text[:12000]), max_tokens=900)
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return {**EMPTY_DETAILS, "error": "model returned no JSON"}
    data = json.loads(match.group(0))
    out = {**EMPTY_DETAILS,
           **{k: data.get(k) or EMPTY_DETAILS[k] for k in ("name", "headline", "location")}}
    out["experience"], out["education"] = tidy(_entries(data.get("experience")),
                                               _entries(data.get("education")))
    out["skills"] = [str(s).strip() for s in data.get("skills") or [] if str(s).strip()][:12]
    # Links are copied off the page, never taken from the model, which could
    # produce a plausible handle that is not the candidate's.
    out["links"] = links(text)
    return out


def _entries(raw) -> list[dict]:
    """Model output, coerced to the {org, role, years} shape the UI renders."""
    out = []
    for e in raw if isinstance(raw, list) else []:
        if isinstance(e, dict):
            out.append({k: str(e.get(k) or "").strip() for k in ("org", "role", "years")})
    return out


# Degree words, matched in an entry's role or org. Institution words such as
# "Institute" are left out on purpose: a research internship at one is work.
_DEGREE = re.compile(r"\b(?:b\.?\s?tech|b\.?\s?sc|b\.e\b|bachelor|m\.?\s?tech|m\.?\s?sc|master"
                     r"|mba|ph\.?\s?d|diploma)", re.I)
_SCHOOL = re.compile(r"\b(?:class\s*(?:x|xii|10|12)(?:th)?|1[02]th|matriculation|secondary"
                     r"|high school|cbse|icse|hsc|ssc)\b", re.I)


def tidy(experience: list[dict], education: list[dict]) -> tuple[list[dict], list[dict]]:
    """Work and study apart, school-level entries dropped. A model asked to
    keep them apart still slips a degree into experience now and then, so the
    split is checked again here."""
    work, study = [], []
    for e in experience:
        both = f"{e.get('org', '')} {e.get('role', '')}"
        if _SCHOOL.search(both):
            continue
        (study if _DEGREE.search(both) else work).append(e)
    for e in education:
        if not _SCHOOL.search(f"{e.get('org', '')} {e.get('role', '')}"):
            study.append(e)
    seen, unique = set(), []
    for e in study:
        key = (e.get("org") or e.get("role", "")).lower()[:30]
        if key not in seen:
            seen.add(key)
            unique.append(e)
    return work[:5], unique[:3]


def links(text: str) -> list[dict]:
    """GitHub, LinkedIn and email, as written on the resume."""
    out = []
    m = re.search(r"github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))", text)
    if m:
        out.append({"kind": "github", "label": m.group(1),
                    "url": f"https://github.com/{m.group(1)}"})
    m = re.search(r"linkedin\.com/in/([A-Za-z0-9_-]{2,100})", text)
    if m:
        out.append({"kind": "linkedin", "label": m.group(1),
                    "url": f"https://www.linkedin.com/in/{m.group(1)}"})
    m = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}", text)
    if m:
        out.append({"kind": "email", "label": m.group(0), "url": f"mailto:{m.group(0)}"})
    return out


# Month names as resumes write them, for spotting date ranges.
_MONTHS = r"Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
_RANGE = re.compile(
    rf"((?:{_MONTHS})[a-z]*\.?\s*\d{{4}}|\d{{4}})\s*[-–—to]+\s*"
    rf"((?:{_MONTHS})[a-z]*\.?\s*\d{{4}}|\d{{4}}|Present|Current)",
    re.I,
)
_SECTION = re.compile(
    r"^\s*(EXPERIENCE|EDUCATION|WORK|EMPLOYMENT|PROJECTS|SKILLS|SUMMARY|PROFILE"
    r"|CERTIFICATIONS?|ACHIEVEMENTS?|AWARDS?|PUBLICATIONS?|INTERESTS?|CONTACT"
    r"|TECHNICAL SKILLS|ACTIVITIES|LANGUAGES)\s*:?\s*$",
    re.I,
)


def _unrun(s: str) -> str:
    """PDF extraction drops spaces between words ('shipsAI products'). Restore
    them at lower-to-upper boundaries, which is where they are usually lost."""
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)


def _name_from_handle(text: str) -> str:
    """A profile URL carries the name far more reliably than the header line,
    which PDF extraction often mangles ('ALE X R MORGAN'). A handle like
    linkedin.com/in/alex-morgan is unambiguous."""
    m = re.search(r"(?:linkedin\.com/in/|github\.com/)([A-Za-z][A-Za-z-]{2,40})", text)
    if not m:
        return ""
    parts = [p for p in re.split(r"[-_]", m.group(1)) if len(p) > 1]
    if not parts:
        return ""
    return " ".join(p.capitalize() for p in parts)


def parse_details(text: str) -> dict:
    """Best-effort structured read of a resume with no model involved.

    Deliberately conservative: a field it cannot find stays empty rather than
    being guessed at. Good enough for the profile panel when scoring is off.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    out: dict = {**EMPTY_DETAILS, "experience": [], "education": [], "skills": [], "links": []}
    if not lines:
        return out

    out["name"] = _name_from_handle(text) or lines[0][:60].title()

    # Location: the first line that looks like "City, Region, Country".
    for ln in lines[1:8]:
        head = ln.split("|")[0].strip()
        if 2 <= head.count(",") + 1 <= 4 and "@" not in head and len(head) < 60:
            if not any(ch.isdigit() for ch in head):
                out["location"] = head
                break

    # Headline: the first substantive line of a SUMMARY/PROFILE section.
    for i, ln in enumerate(lines):
        if re.match(r"^\s*(SUMMARY|PROFILE|OBJECTIVE)\s*$", ln, re.I) and i + 1 < len(lines):
            words = re.split(r"[.;]", _unrun(lines[i + 1]))[0].split()
            out["summary_line"] = " ".join(words[:9])
            break

    # Experience: lines carrying a date range. The role tends to sit on the
    # same line (before the dates) and the org on the next.
    seen = set()
    for i, ln in enumerate(lines):
        if _SECTION.match(ln):
            continue
        m = _RANGE.search(ln)
        if not m:
            continue
        years = f"{m.group(1)} - {m.group(2)}"
        role = _unrun(ln[: m.start()]).strip(" ,|·-–—")
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        # The following line is the employer - unless we have run into the next
        # section heading, in which case this entry has no org line.
        org = "" if _SECTION.match(nxt) else nxt.split(",")[0].strip()
        if not role and org:
            role, org = org, ""
        if not role:
            continue
        key = (role[:40], years)
        if key in seen:
            continue
        seen.add(key)
        out["experience"].append({"org": org[:50] or role[:50],
                                  "role": role[:60] if org else "",
                                  "years": years})
        if len(out["experience"]) >= 8:
            break
    out["experience"], out["education"] = tidy(out["experience"], [])
    out["skills"] = _skills(lines)
    out["links"] = links(text)

    for e in out["experience"]:
        if e["role"] and not re.match(r"^b\.?tech|^b\.?sc|^m\.?tech|^bachelor|^master|^diploma",
                                      e["role"], re.I):
            out["headline"] = e["role"]
            break
    if not out["headline"]:
        out["headline"] = out.pop("summary_line", "")
    out.pop("summary_line", None)
    return out


def _skills(lines: list[str]) -> list[str]:
    """Items from the SKILLS section. Category labels ("Languages:") are
    dropped, and long phrases are competencies rather than technologies."""
    out: list[str] = []
    inside = False
    for ln in lines:
        if not inside:
            inside = bool(re.match(r"^(?:technical\s+)?skills\b", ln, re.I)) and len(ln) < 30
            continue
        if _SECTION.match(ln):
            break
        body = ln.split(":", 1)[1] if ":" in ln[:40] else ln
        for item in re.split(r"[,;|]", body):
            item = item.strip(" .-•")
            if 1 <= len(item) <= 24 and item.lower() not in {s.lower() for s in out}:
                out.append(item)
    return out[:12]
