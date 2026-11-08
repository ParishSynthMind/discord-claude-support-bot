"""SQLite state: handled interactions (idempotency), support cases, moderation reports,
rate-limit hits, daily AI spend and an audit trail. One connection, one lock."""
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS interactions (id TEXT PRIMARY KEY, kind TEXT, received_at REAL);
CREATE TABLE IF NOT EXISTS cases (
  id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT, channel_id TEXT, thread_id TEXT,
  user_id TEXT, question TEXT, answer_status TEXT, status TEXT NOT NULL DEFAULT 'open',
  handed_over_by TEXT, closed_by TEXT, created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT, channel_id TEXT, message_id TEXT,
  author_id TEXT, reporter_id TEXT, source TEXT, reason TEXT, excerpt TEXT,
  category TEXT, severity TEXT, status TEXT NOT NULL DEFAULT 'open', mod_message_id TEXT,
  resolved_by TEXT, created_at REAL, updated_at REAL, UNIQUE(message_id, reporter_id));
CREATE TABLE IF NOT EXISTS hits (bucket TEXT, user_id TEXT, at REAL);
CREATE INDEX IF NOT EXISTS hits_idx ON hits(bucket, user_id, at);
CREATE TABLE IF NOT EXISTS spend (
  day TEXT PRIMARY KEY, reserved REAL NOT NULL DEFAULT 0, spent REAL NOT NULL DEFAULT 0,
  calls INTEGER NOT NULL DEFAULT 0, tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS trail (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, event TEXT, detail TEXT);
"""

CASE_OPEN, CASE_HANDED_OVER, CASE_CLOSED = "open", "handed_over", "closed"


def utc_day(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), timezone.utc).strftime("%Y-%m-%d")


class Store:
    def __init__(self, path: str = ":memory:", clock=time.time):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()
        self.clock = clock

    def _x(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def _all(self, sql, args=()) -> List[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def _one(self, sql, args=()) -> Optional[dict]:
        rows = self._all(sql, args)
        return rows[0] if rows else None

    # idempotency -------------------------------------------------------------
    def claim_interaction(self, interaction_id: str, kind: str) -> bool:
        """True the first time an interaction id is seen, False for a repeat delivery."""
        cur = self._x("INSERT OR IGNORE INTO interactions(id, kind, received_at) VALUES (?,?,?)",
                      (interaction_id, kind, self.clock()))
        return cur.rowcount == 1

    # trail -------------------------------------------------------------------
    def log(self, event: str, **detail) -> None:
        self._x("INSERT INTO trail(at, event, detail) VALUES (?,?,?)",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), event, json.dumps(detail, sort_keys=True)))

    def trail(self, limit: int = 200) -> List[dict]:
        return self._all("SELECT * FROM trail ORDER BY id DESC LIMIT ?", (limit,))[::-1]

    # cases -------------------------------------------------------------------
    def open_case(self, guild_id, channel_id, user_id, question) -> int:
        now = self.clock()
        cur = self._x("INSERT INTO cases(guild_id, channel_id, user_id, question, created_at, updated_at) "
                      "VALUES (?,?,?,?,?,?)", (guild_id, channel_id, user_id, question, now, now))
        return cur.lastrowid

    def case(self, case_id: int) -> Optional[dict]:
        return self._one("SELECT * FROM cases WHERE id=?", (case_id,))

    def case_by_thread(self, thread_id: str) -> Optional[dict]:
        return self._one("SELECT * FROM cases WHERE thread_id=?", (thread_id,))

    def update_case(self, case_id: int, **fields) -> None:
        fields["updated_at"] = self.clock()
        cols = ", ".join(f"{k}=?" for k in fields)
        self._x(f"UPDATE cases SET {cols} WHERE id=?", (*fields.values(), case_id))

    def move_case(self, case_id: int, to: str, allowed_from: tuple, **fields) -> bool:
        """Atomic status change; False if the case was not in one of allowed_from (e.g. a double click)."""
        fields.update(status=to, updated_at=self.clock())
        cols = ", ".join(f"{k}=?" for k in fields)
        marks = ",".join("?" * len(allowed_from))
        cur = self._x(f"UPDATE cases SET {cols} WHERE id=? AND status IN ({marks})",
                      (*fields.values(), case_id, *allowed_from))
        return cur.rowcount == 1

    def cases(self, status: Optional[str] = None) -> List[dict]:
        if status:
            return self._all("SELECT * FROM cases WHERE status=? ORDER BY id", (status,))
        return self._all("SELECT * FROM cases ORDER BY id")

    # reports -----------------------------------------------------------------
    def add_report(self, **r) -> Optional[int]:
        """None if this reporter already reported this message."""
        now = self.clock()
        r.setdefault("created_at", now)
        r.setdefault("updated_at", now)
        cols = ", ".join(r)
        cur = self._x(f"INSERT OR IGNORE INTO reports({cols}) VALUES ({','.join('?' * len(r))})", tuple(r.values()))
        return cur.lastrowid if cur.rowcount == 1 else None

    def report(self, report_id: int) -> Optional[dict]:
        return self._one("SELECT * FROM reports WHERE id=?", (report_id,))

    def update_report(self, report_id: int, **fields) -> None:
        fields["updated_at"] = self.clock()
        cols = ", ".join(f"{k}=?" for k in fields)
        self._x(f"UPDATE reports SET {cols} WHERE id=?", (*fields.values(), report_id))

    def resolve_report(self, report_id: int, status: str, by: str) -> bool:
        cur = self._x("UPDATE reports SET status=?, resolved_by=?, updated_at=? WHERE id=? AND status='open'",
                      (status, by, self.clock(), report_id))
        return cur.rowcount == 1

    def reports(self, status: Optional[str] = None) -> List[dict]:
        if status:
            return self._all("SELECT * FROM reports WHERE status=? ORDER BY id", (status,))
        return self._all("SELECT * FROM reports ORDER BY id")

    def open_reports_for_message(self, message_id: str) -> List[dict]:
        return self._all("SELECT * FROM reports WHERE message_id=? AND status='open'", (message_id,))

    # rate limits ---------------------------------------------------------------
    def hit(self, bucket: str, user_id: str, limit: int, window_s: int) -> Optional[float]:
        """Record one use. Returns None when allowed, or the seconds until the next slot frees up."""
        now = self.clock()
        with self.lock:
            self.db.execute("DELETE FROM hits WHERE bucket=? AND at<?", (bucket, now - window_s))
            rows = self.db.execute("SELECT at FROM hits WHERE bucket=? AND user_id=? ORDER BY at",
                                   (bucket, user_id)).fetchall()
            if len(rows) >= limit:
                self.db.commit()
                return max(1.0, rows[0]["at"] + window_s - now)
            self.db.execute("INSERT INTO hits(bucket, user_id, at) VALUES (?,?,?)", (bucket, user_id, now))
            self.db.commit()
            return None

    # spend -------------------------------------------------------------------
    def spend_today(self) -> dict:
        day = utc_day(self.clock())
        return self._one("SELECT * FROM spend WHERE day=?", (day,)) or \
            {"day": day, "reserved": 0.0, "spent": 0.0, "calls": 0, "tokens_in": 0, "tokens_out": 0}

    def reserve(self, amount: float, cap: float) -> bool:
        """Hold a worst-case amount before a call. False if it would break the daily cap."""
        day = utc_day(self.clock())
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO spend(day) VALUES (?)", (day,))
            cur = self.db.execute("UPDATE spend SET reserved = reserved + ? WHERE day=? AND spent + reserved + ? <= ?",
                                  (amount, day, amount, cap + 1e-9))
            self.db.commit()
            return cur.rowcount == 1

    def settle(self, reserved: float, actual: float, tokens_in: int, tokens_out: int) -> None:
        day = utc_day(self.clock())
        self._x("UPDATE spend SET reserved = MAX(0, reserved - ?), spent = spent + ?, calls = calls + 1, "
                "tokens_in = tokens_in + ?, tokens_out = tokens_out + ? WHERE day=?",
                (reserved, actual, tokens_in, tokens_out, day))
