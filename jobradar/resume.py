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
