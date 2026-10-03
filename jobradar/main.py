"""jobradar CLI.

  scan     fetch every source, diff against the DB, score new jobs
  digest   email everything not yet reported (this is what cron runs weekly)
  run      scan + digest in one shot
  list     print recent finds in the terminal
  test     send a sample email to prove SMTP works
"""
from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
import sys
import time
from pathlib import Path

import yaml

from . import alerts as alerts_mod
from . import digest as digest_mod
from . import sources
from .budget import Budget
from .cursor import CursorError
from .llm import RateLimited, make_scorer
from .localmatch import LocalScorer
from .resume import build_profile, extract_text
from .relevance import apply_cap as apply_relevance_cap
from .seniority import apply_cap
from .store import Store

ROOT = Path(__file__).resolve().parent.parent
PROFILE_CACHE = ROOT / ".resume_profile.json"


def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"No config at {path}. Copy config.example.yaml to config.yaml and edit it.")
    return yaml.safe_load(path.read_text())


def get_scorer(cfg: dict):
    spec = cfg.get("llm", {})
    provider = spec.get("provider", "none")
    if provider == "local":
        # The keyword matcher works off the resume itself, not a model name.
        path = Path(cfg["resume_path"])
        return make_scorer("local", extract_text(path if path.is_absolute()
                                                 else ROOT / path))
    return make_scorer(provider, spec.get("model", ""), spec.get("model_params"))


def get_profile(cfg: dict, scorer, force: bool = False) -> str:
    resume_path = ROOT / cfg["resume_path"] if not Path(cfg["resume_path"]).is_absolute() \
        else Path(cfg["resume_path"])
    stamp = resume_path.stat().st_mtime if resume_path.exists() else 0
    summarizer = scorer if (scorer is not None and hasattr(scorer, "complete")) else None
    # The cache key includes whether a model wrote it. Without this, a raw-text
    # profile written during a `provider: none` run gets reused by a later
    # model run - and raw text truncates mid-resume, hiding the experience
    # section that says how senior the candidate actually is.
    kind = "model" if summarizer else "raw"
    if PROFILE_CACHE.exists() and not force:
        cached = json.loads(PROFILE_CACHE.read_text())
        if cached.get("mtime") == stamp and cached.get("kind") == kind:
            return cached["profile"]
    text = extract_text(resume_path)
    try:
        profile = build_profile(text, summarizer)
    except Exception:  # noqa: BLE001
        # Out of quota or provider down. A summary is a nicety; the run's job
        # is finding openings. Fall back to the resume text and do not cache
        # it, so a proper summary is built once the provider recovers.
        return build_profile(text, None)
    PROFILE_CACHE.write_text(json.dumps({"mtime": stamp, "kind": kind, "profile": profile}))
    return profile


def passes_filters(job: dict, f: dict) -> bool:
    title = job["title"].lower()
    inc = [s.lower() for s in f.get("title_include") or []]
    exc = [s.lower() for s in f.get("title_exclude") or []]
    locs = [s.lower() for s in f.get("locations") or []]
    if inc and not any(s in title for s in inc):
        return False
    if any(s in title for s in exc):
        return False
    if locs and not any(s in job["location"].lower() for s in locs):
        return False
    return True
