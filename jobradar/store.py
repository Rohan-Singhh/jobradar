"""SQLite state. The whole point: remember what we have already seen, so a
later run can tell 'this is NEW' from 'this was here last week'."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    company     TEXT NOT NULL,
    title       TEXT NOT NULL,
    url         TEXT,
    location    TEXT,
    source      TEXT,
    posted_at   REAL,
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    closed_at   REAL,
    score       INTEGER,
    reason      TEXT,
    notified    INTEGER NOT NULL DEFAULT 0,
    domain      TEXT,
    breakdown   TEXT,
    alerted     INTEGER NOT NULL DEFAULT 0,
    description TEXT
);
CREATE INDEX IF NOT EXISTS idx_first_seen ON jobs(first_seen);
CREATE TABLE IF NOT EXISTS runs (
    ts INTEGER PRIMARY KEY, new_count INTEGER, closed_count INTEGER, errors TEXT
);
"""


class Store:
    def __init__(self, path: str | Path = "jobradar.db"):
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()
        self.last_upsert_ts = time.time()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(jobs)")}
        for col, ddl in (("domain", "TEXT"), ("breakdown", "TEXT"),
                         ("alerted", "INTEGER NOT NULL DEFAULT 0"),
                         ("description", "TEXT")):
            if col not in have:
                self.db.execute(f"ALTER TABLE jobs ADD COLUMN {col} {ddl}")

    # -- ingest -------------------------------------------------------------
    def upsert(self, jobs: list[dict]) -> list[dict]:
        """Insert jobs, returning only the ones never seen before."""
        now = time.time()
        self.last_upsert_ts = now
        fresh = []
        for j in jobs:
            row = self.db.execute("SELECT id FROM jobs WHERE id = ?", (j["id"],)).fetchone()
            if row:
                self.db.execute(
                    "UPDATE jobs SET last_seen = ?, closed_at = NULL WHERE id = ?", (now, j["id"])
                )
            else:
                self.db.execute(
                    """INSERT INTO jobs (id, company, title, url, location, source,
                                         posted_at, first_seen, last_seen, domain, description)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (j["id"], j["company"], j["title"], j["url"], j["location"],
                     j["source"], j["posted_at"], now, now, j.get("domain", ""),
                     # Trimmed: enough for search to work, not so much that the
                     # database balloons with boilerplate benefits sections.
                     (j.get("description") or "")[:1500]),
                )
                fresh.append(j)
        self.db.commit()
        return fresh

    def stats(self) -> dict:
        row = self.db.execute(
            "SELECT COUNT(*) total, SUM(closed_at IS NULL) open FROM jobs"
        ).fetchone()
        return {"total": row["total"] or 0, "open": row["open"] or 0}
