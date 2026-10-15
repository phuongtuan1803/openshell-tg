"""SQLite state shared by the CLI and the approval service.

Holds only what OpenShell does not: which folder owns which sandbox,
which Telegram message shows which proposal, and four timestamps per
proposal for response-time metrics. No secrets are stored here.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sandboxes (
    name        TEXT PRIMARY KEY,
    folder      TEXT NOT NULL,
    agent       TEXT NOT NULL,
    idle_stop   INTEGER NOT NULL,
    created_at  REAL NOT NULL,
    last_used   REAL NOT NULL,
    stopped_at  REAL,
    keep_until  REAL,
    reminded_at REAL
);
CREATE TABLE IF NOT EXISTS requests (
    sandbox     TEXT NOT NULL,
    chunk_id    TEXT NOT NULL,
    summary     TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '{}',
    risky       INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'pending',
    reason      TEXT,
    count       INTEGER NOT NULL DEFAULT 1,
    message_id  INTEGER,
    t0 REAL, t1 REAL, t2 REAL, t3 REAL,
    PRIMARY KEY (sandbox, chunk_id)
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    path = Path(base) / "openshell-tg"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


@dataclass
class Sandbox:
    id: int
    name: str
    folder: str
    agent: str
    idle_stop: int
    created_at: float
    last_used: float
    stopped_at: float | None
    keep_until: float | None
    reminded_at: float | None


@dataclass
class Request:
    id: int
    sandbox: str
    chunk_id: str
    summary: str
    detail: str
    risky: bool
    status: str
    reason: str | None
    count: int
    message_id: int | None
    t0: float | None
    t1: float | None
    t2: float | None
    t3: float | None


class Store:
    def __init__(self, path: Path | None = None):
        self.path = path or state_dir() / "state.db"
        self.db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    # sandboxes

    def save_sandbox(self, name: str, folder: str, agent: str, idle_stop: int) -> None:
        now = time.time()
        self.db.execute(
            """INSERT INTO sandboxes (name, folder, agent, idle_stop, created_at, last_used)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET folder=excluded.folder, agent=excluded.agent,
                 idle_stop=excluded.idle_stop, last_used=excluded.last_used,
                 stopped_at=NULL, reminded_at=NULL""",
            (name, folder, agent, idle_stop, now, now),
        )

    def sandboxes(self) -> list[Sandbox]:
        rows = self.db.execute("SELECT rowid AS id, * FROM sandboxes ORDER BY name").fetchall()
        return [Sandbox(**dict(r)) for r in rows]

    def sandbox(self, name: str) -> Sandbox | None:
        row = self.db.execute("SELECT rowid AS id, * FROM sandboxes WHERE name=?", (name,)).fetchone()
        return Sandbox(**dict(row)) if row else None

    def sandbox_by_id(self, sid: int) -> Sandbox | None:
        row = self.db.execute("SELECT rowid AS id, * FROM sandboxes WHERE rowid=?", (sid,)).fetchone()
        return Sandbox(**dict(row)) if row else None

    def touch(self, name: str) -> None:
        self.db.execute("UPDATE sandboxes SET last_used=? WHERE name=?", (time.time(), name))

    def mark_stopped(self, name: str) -> None:
        self.db.execute("UPDATE sandboxes SET stopped_at=? WHERE name=?", (time.time(), name))

    def mark_reminded(self, name: str) -> None:
        self.db.execute("UPDATE sandboxes SET reminded_at=? WHERE name=?", (time.time(), name))

    def keep(self, name: str, seconds: float) -> None:
        self.db.execute(
            "UPDATE sandboxes SET keep_until=?, reminded_at=NULL WHERE name=?",
            (time.time() + seconds, name),
        )

    def forget_sandbox(self, name: str) -> None:
        self.db.execute("DELETE FROM sandboxes WHERE name=?", (name,))

    # requests

    def upsert_request(self, sandbox: str, chunk_id: str, summary: str, detail: str, risky: bool,
                       t0: float) -> bool:
        """Record a pending proposal. Returns True if it is new."""
        cur = self.db.execute(
            """INSERT OR IGNORE INTO requests (sandbox, chunk_id, summary, detail, risky, t0)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (sandbox, chunk_id, summary, detail, int(risky), t0),
        )
        return cur.rowcount == 1

    def request(self, sandbox: str, chunk_id: str) -> Request | None:
        row = self.db.execute(
            "SELECT rowid AS id, * FROM requests WHERE sandbox=? AND chunk_id=?", (sandbox, chunk_id)
        ).fetchone()
        return _request(row) if row else None

    def request_by_id(self, rid: int) -> Request | None:
        row = self.db.execute("SELECT rowid AS id, * FROM requests WHERE rowid=?", (rid,)).fetchone()
        return _request(row) if row else None

    def group(self, req: "Request") -> list["Request"]:
        """Open requests shown on the same card (same sandbox and summary)."""
        rows = self.db.execute(
            "SELECT rowid AS id, * FROM requests WHERE sandbox=? AND summary=? AND status='pending'",
            (req.sandbox, req.summary),
        ).fetchall()
        return [_request(r) for r in rows]

    def open_requests(self) -> list[Request]:
        rows = self.db.execute("SELECT rowid AS id, * FROM requests WHERE status='pending' ORDER BY t0").fetchall()
        return [_request(r) for r in rows]

    def find_duplicate(self, sandbox: str, summary: str) -> Request | None:
        row = self.db.execute(
            "SELECT rowid AS id, * FROM requests WHERE sandbox=? AND summary=? AND status='pending' "
            "AND message_id IS NOT NULL ORDER BY t0 LIMIT 1",
            (sandbox, summary),
        ).fetchone()
        return _request(row) if row else None

    def bump(self, sandbox: str, chunk_id: str) -> int:
        self.db.execute(
            "UPDATE requests SET count=count+1 WHERE sandbox=? AND chunk_id=?", (sandbox, chunk_id)
        )
        return self.request(sandbox, chunk_id).count

    def set_message(self, sandbox: str, chunk_id: str, message_id: int) -> None:
        self.db.execute(
            "UPDATE requests SET message_id=?, t1=? WHERE sandbox=? AND chunk_id=?",
            (message_id, time.time(), sandbox, chunk_id),
        )

    def close_request(self, sandbox: str, chunk_id: str, status: str, reason: str | None = None,
                      tapped: bool = False) -> None:
        now = time.time()
        self.db.execute(
            """UPDATE requests SET status=?, reason=?, t2=CASE WHEN ? THEN ? ELSE t2 END
               WHERE sandbox=? AND chunk_id=?""",
            (status, reason, int(tapped), now, sandbox, chunk_id),
        )

    def mark_loaded(self, sandbox: str, chunk_id: str) -> None:
        self.db.execute(
            "UPDATE requests SET t3=? WHERE sandbox=? AND chunk_id=?", (time.time(), sandbox, chunk_id)
        )

    def stats(self, days: int = 7) -> dict:
        since = time.time() - days * 86400
        rows = self.db.execute("SELECT rowid AS id, * FROM requests WHERE t0>=?", (since,)).fetchall()
        reqs = [_request(r) for r in rows]
        by_status: dict[str, int] = {}
        for r in reqs:
            by_status[r.status] = by_status.get(r.status, 0) + 1
        response = sorted(r.t2 - r.t1 for r in reqs if r.t1 and r.t2)
        waited = sum((r.t3 or r.t2) - r.t0 for r in reqs if r.t0 and (r.t3 or r.t2))
        return {
            "total": len(reqs),
            "by_status": by_status,
            "median": _percentile(response, 50),
            "p90": _percentile(response, 90),
            "waited": waited,
        }

    # meta

    def get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def put(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def _request(row: sqlite3.Row) -> Request:
    d = dict(row)
    d["risky"] = bool(d["risky"])
    return Request(**d)


def _percentile(values: list[float], pct: int) -> float | None:
    if not values:
        return None
    index = min(len(values) - 1, max(0, round(pct / 100 * len(values)) - 1))
    return values[index]
