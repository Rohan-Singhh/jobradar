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
        try:
            from pypdf import PdfReader
        except ImportError as e:
            raise RuntimeError("pip install pypdf to read PDF resumes") from e
        return "\n".join(page.extract_text() or "" for page in PdfReader(str(p)).pages)
    return p.read_text(encoding="utf-8", errors="replace")


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
  "experience": [{{"org": "...", "role": "...", "years": "<e.g. 2024 - 2026>"}}]}}
List experience newest first, education included, max 6 entries.

RESUME:
{text}"""


def build_details(text: str, llm) -> dict:
    """Structured fields for the web UI's profile panel."""
    import json
    import re
    fallback = {"name": "", "headline": "", "location": "", "experience": [], "error": ""}
    if llm is None:
        return {**fallback, "error": "no model configured"}
    # Errors are returned, not swallowed: the caller needs to know whether this
    # is a real empty result (cacheable) or a failure (must be retried).
    raw = llm.complete(DETAILS_PROMPT.format(text=text[:12000]), max_tokens=700)
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return {**fallback, "error": "model returned no JSON"}
    data = json.loads(match.group(0))
    return {**fallback, **{k: data.get(k, fallback[k]) for k in fallback if k != "error"}}


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
    out: dict = {"name": "", "headline": "", "location": "", "experience": [], "error": ""}
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
        if len(out["experience"]) >= 6:
            break

    for e in out["experience"]:
        if e["role"] and not re.match(r"^b\.?tech|^b\.?sc|^m\.?tech|^bachelor|^master|^diploma",
                                      e["role"], re.I):
            out["headline"] = e["role"]
            break
    if not out["headline"]:
        out["headline"] = out.pop("summary_line", "")
    out.pop("summary_line", None)
    return out
