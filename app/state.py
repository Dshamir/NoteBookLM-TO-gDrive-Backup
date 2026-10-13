"""SQLite persistence: notebooks (with archive stage), runs and events."""
from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Stage machine --------------------------------------------------------------
NEW = "NEW"
EXPORTING = "EXPORTING"
EXPORTED = "EXPORTED"
UPLOADING = "UPLOADING"
UPLOADED = "UPLOADED"
CLOUD_VERIFIED = "CLOUD_VERIFIED"
DELETING = "DELETING"
DELETED = "DELETED"
FAILED = "FAILED"
SKIPPED = "SKIPPED"

STAGES = [NEW, EXPORTING, EXPORTED, UPLOADING, UPLOADED, CLOUD_VERIFIED, DELETING, DELETED, FAILED, SKIPPED]
# Stages that mean "a verified copy exists in Google Drive".
ARCHIVED_STAGES = {CLOUD_VERIFIED, DELETING, DELETED}

SCHEMA = """
CREATE TABLE IF NOT EXISTS notebooks (
    id TEXT PRIMARY KEY,
    title TEXT,
    emoji TEXT,
    created_at TEXT,
    year INTEGER,
    sources_count INTEGER,
    is_owner INTEGER,
    selected INTEGER DEFAULT 0,
    stage TEXT DEFAULT 'NEW',
    folder TEXT,
    drive_folder_id TEXT,
    complete INTEGER DEFAULT 0,
    files INTEGER DEFAULT 0,
    bytes INTEGER DEFAULT 0,
    stats TEXT,
    error TEXT,
    attempts INTEGER DEFAULT 0,
    present INTEGER DEFAULT 1,
    cloud_detail TEXT,
    deleted_at TEXT,
    updated_at TEXT,
    last_seen_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT,
    finished_at TEXT,
    status TEXT,
    options TEXT,
    totals TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    run_id INTEGER,
    notebook_id TEXT,
    level TEXT,
    message TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id);
CREATE INDEX IF NOT EXISTS idx_nb_stage ON notebooks(stage);
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class StateDB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    # -- helpers -------------------------------------------------------------
    def _q(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]

    def _x(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            if sql.lstrip().upper().startswith(("UPDATE", "DELETE")):
                return cur.rowcount  # lastrowid is stale connection state for these
            return cur.lastrowid or cur.rowcount

    # -- notebooks -----------------------------------------------------------
    def upsert_listing(self, items: list[dict], years: list[int]) -> None:
        """Merge a fresh notebook listing. New in-scope owned notebooks are auto-selected."""
        ts = now_iso()
        seen = set()
        with self._lock:
            for it in items:
                seen.add(it["id"])
                row = self._conn.execute("SELECT id FROM notebooks WHERE id=?", (it["id"],)).fetchone()
                if row:
                    self._conn.execute(
                        """UPDATE notebooks SET title=?, emoji=?, created_at=?, year=?, sources_count=?,
                           is_owner=?, present=1, last_seen_at=?
                           WHERE id=?""",
                        (it["title"], it.get("emoji"), it.get("created_at"), it.get("year"),
                         it.get("sources_count"), int(bool(it.get("is_owner", True))), ts, it["id"]),
                    )
                else:
                    auto = int(bool(it.get("is_owner", True)) and it.get("year") in years)
                    self._conn.execute(
                        """INSERT INTO notebooks (id,title,emoji,created_at,year,sources_count,is_owner,
                           selected,stage,present,updated_at,last_seen_at)
                           VALUES (?,?,?,?,?,?,?,?, 'NEW', 1, ?, ?)""",
                        (it["id"], it["title"], it.get("emoji"), it.get("created_at"), it.get("year"),
                         it.get("sources_count"), int(bool(it.get("is_owner", True))), auto, ts, ts),
                    )
            # Notebooks that vanished from the account (deleted elsewhere or by us)
            if seen:
                marks = ",".join("?" * len(seen))
                self._conn.execute(f"UPDATE notebooks SET present=0 WHERE id NOT IN ({marks})", tuple(seen))

    def list_notebooks(self) -> list[dict]:
        return self._q("SELECT * FROM notebooks ORDER BY created_at DESC")

    def get(self, nb_id: str) -> dict | None:
        rows = self._q("SELECT * FROM notebooks WHERE id=?", (nb_id,))
        return rows[0] if rows else None

    def set_selected(self, ids: list[str], selected: bool) -> None:
        if not ids:
            return
        marks = ",".join("?" * len(ids))
        # Shared (not owned) notebooks can never be selected.
        self._x(f"UPDATE notebooks SET selected=? WHERE id IN ({marks}) AND is_owner=1",
                (int(selected), *ids))

    def select_years(self, years: list[int], only: bool = True) -> None:
        with self._lock:
            if only:
                self._conn.execute("UPDATE notebooks SET selected=0")
            if years:
                marks = ",".join("?" * len(years))
                self._conn.execute(
                    f"UPDATE notebooks SET selected=1 WHERE is_owner=1 AND present=1 AND year IN ({marks})",
                    tuple(years))

    def select_cutoff(self, before_iso: str, only: bool = True) -> None:
        """Select owned, present notebooks created on or before `before_iso` (e.g. '2026-03-01')."""
        prefix = before_iso.strip()[:10]
        if not prefix:
            return
        with self._lock:
            if only:
                self._conn.execute("UPDATE notebooks SET selected=0")
            self._conn.execute(
                """UPDATE notebooks SET selected=1
                   WHERE is_owner=1 AND present=1 AND created_at IS NOT NULL
                   AND SUBSTR(created_at, 1, 10) <= ?""",
                (prefix,),
            )

    def select_preset(self, preset: str, only: bool = True) -> int:
        """Select owned, present notebooks matching a preset ('empty', 'stale', 'oldest25', 'oldest50')."""
        with self._lock:
            if only:
                self._conn.execute("UPDATE notebooks SET selected=0")
            if preset == "empty":
                self._conn.execute(
                    """UPDATE notebooks SET selected=1
                       WHERE is_owner=1 AND present=1 AND (sources_count=0 OR sources_count IS NULL)"""
                )
            elif preset == "stale":
                self._conn.execute(
                    """UPDATE notebooks SET selected=1
                       WHERE is_owner=1 AND present=1 AND created_at IS NOT NULL
                       AND SUBSTR(created_at, 1, 10) <= date('now', '-180 days')"""
                )
            elif preset == "oldest25":
                self._conn.execute(
                    """UPDATE notebooks SET selected=1
                       WHERE id IN (
                           SELECT id FROM notebooks WHERE is_owner=1 AND present=1
                           ORDER BY created_at ASC LIMIT 25
                       )"""
                )
            elif preset == "oldest50":
                self._conn.execute(
                    """UPDATE notebooks SET selected=1
                       WHERE id IN (
                           SELECT id FROM notebooks WHERE is_owner=1 AND present=1
                           ORDER BY created_at ASC LIMIT 50
                       )"""
                )
            row = self._conn.execute("SELECT COUNT(*) n FROM notebooks WHERE selected=1").fetchone()
            return int(row["n"] if row else 0)

    def update(self, nb_id: str, **cols) -> None:
        if not cols:
            return
        cols["updated_at"] = now_iso()
        sets = ", ".join(f"{k}=?" for k in cols)
        self._x(f"UPDATE notebooks SET {sets} WHERE id=?", (*cols.values(), nb_id))

    def set_stage(self, nb_id: str, stage: str, **cols) -> None:
        assert stage in STAGES, stage
        self.update(nb_id, stage=stage, **cols)

    def by_stage(self, *stages: str) -> list[dict]:
        marks = ",".join("?" * len(stages))
        return self._q(f"SELECT * FROM notebooks WHERE stage IN ({marks}) ORDER BY created_at", stages)

    def work_queue(self, ids: list[str] | None = None) -> list[dict]:
        """Selected (or explicitly requested), present, owned notebooks not yet deleted."""
        if ids:
            marks = ",".join("?" * len(ids))
            return self._q(
                f"""SELECT * FROM notebooks WHERE id IN ({marks}) AND present=1 AND is_owner=1
                    AND stage NOT IN ('DELETED','SKIPPED') ORDER BY created_at""", ids)
        return self._q(
            """SELECT * FROM notebooks WHERE selected=1 AND present=1 AND is_owner=1
               AND stage NOT IN ('DELETED','SKIPPED')
               ORDER BY created_at""")

    def upsert_one(self, item: dict, selected: bool = False) -> None:
        """Insert/refresh a single notebook without touching the presence flag of others."""
        ts = now_iso()
        with self._lock:
            self._conn.execute(
                """INSERT INTO notebooks (id,title,emoji,created_at,year,sources_count,is_owner,
                   selected,stage,present,updated_at,last_seen_at)
                   VALUES (?,?,?,?,?,?,?,?, 'NEW', 1, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET title=excluded.title, sources_count=excluded.sources_count,
                   present=1, last_seen_at=excluded.last_seen_at""",
                (item["id"], item["title"], item.get("emoji"), item.get("created_at"), item.get("year"),
                 item.get("sources_count"), int(bool(item.get("is_owner", True))), int(selected), ts, ts))

    def reset_interrupted(self) -> int:
        """After a crash/restart, half-done exports/uploads restart from scratch (DELETING is re-checked)."""
        return self._x(
            """UPDATE notebooks SET stage='NEW', error='interrupted — will restart'
               WHERE stage IN ('EXPORTING','EXPORTED','UPLOADING','UPLOADED')""")

    def reset_auth_failures(self) -> int:
        """FAILED only because the Google session had expired (not a real failure) -> back in the queue.

        Never touches CLOUD_VERIFIED/DELETING rows: their Drive copy is intact and they re-verify normally.
        """
        return self._x(
            """UPDATE notebooks SET stage='NEW', error='session expired — will retry',
                      attempts=MAX(COALESCE(attempts,0)-1,0), updated_at=?
               WHERE stage='FAILED' AND (
                     LOWER(error) LIKE '%unauthenticated%' OR LOWER(error) LIKE '%csrf token%'
                  OR LOWER(error) LIKE '%authentication expired%' OR LOWER(error) LIKE '%session expired%')""",
            (now_iso(),))

    def counts(self) -> dict:
        rows = self._q("SELECT stage, COUNT(*) n FROM notebooks GROUP BY stage")
        by_stage = {r["stage"]: r["n"] for r in rows}
        tot = self._q(
            """SELECT
                 SUM(CASE WHEN present=1 THEN 1 ELSE 0 END) present,
                 SUM(CASE WHEN selected=1 AND present=1 THEN 1 ELSE 0 END) selected,
                 SUM(CASE WHEN year=2024 AND present=1 THEN 1 ELSE 0 END) y2024,
                 SUM(CASE WHEN year=2025 AND present=1 THEN 1 ELSE 0 END) y2025,
                 SUM(CASE WHEN year=2026 AND present=1 THEN 1 ELSE 0 END) y2026,
                 SUM(CASE WHEN stage='DELETED' THEN bytes ELSE 0 END) bytes_archived_deleted,
                 SUM(bytes) bytes_total
               FROM notebooks""")[0]
        year_rows = self._q("SELECT year, COUNT(*) n FROM notebooks WHERE present=1 GROUP BY year")
        by_year = {str(r["year"]): r["n"] for r in year_rows if r.get("year") is not None}
        return {"by_stage": by_stage, "by_year": by_year, **{k: (v or 0) for k, v in tot.items()}}

    # -- runs / events -------------------------------------------------------
    def start_run(self, options: dict) -> int:
        return self._x("INSERT INTO runs (started_at,status,options) VALUES (?,?,?)",
                       (now_iso(), "running", json.dumps(options)))

    def finish_run(self, run_id: int, status: str, totals: dict) -> None:
        self._x("UPDATE runs SET finished_at=?, status=?, totals=? WHERE id=?",
                (now_iso(), status, json.dumps(totals), run_id))

    def runs(self, limit: int = 20) -> list[dict]:
        rows = self._q("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r["options"] = json.loads(r["options"] or "{}")
            r["totals"] = json.loads(r["totals"] or "{}")
        return rows

    def add_event(self, level: str, message: str, run_id: int | None = None,
                  notebook_id: str | None = None) -> dict:
        ev = {"ts": now_iso(), "run_id": run_id, "notebook_id": notebook_id,
              "level": level, "message": message}
        ev["id"] = self._x("INSERT INTO events (ts,run_id,notebook_id,level,message) VALUES (?,?,?,?,?)",
                           (ev["ts"], run_id, notebook_id, level, message))
        return ev

    def events(self, limit: int = 300, notebook_id: str | None = None) -> list[dict]:
        if notebook_id:
            rows = self._q("SELECT * FROM events WHERE notebook_id=? ORDER BY id DESC LIMIT ?",
                           (notebook_id, limit))
        else:
            rows = self._q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        return list(reversed(rows))
