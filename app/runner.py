"""Archive pipeline: one notebook at a time  EXPORT → UPLOAD → CLOUD VERIFY → (DELETE).

Safety rules (enforced in `may_delete`):
  * a notebook is deleted from NotebookLM only when its Drive copy has just been verified file-by-file
    (md5 + size against the local manifest) AND the export was complete;
  * shared (not owned) notebooks are never touched;
  * deleting requires the run option `delete`, the setting `delete_after_verify` and the one-time typed
    confirmation (`delete_confirmed`) — except for the throwaway notebook created by the e2e self-test.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from . import nlm_service as nlm
from .archive import (MANIFEST_NAME, manifest_with_self, notebook_folder_name, render_catalog, year_dir)
from .config import STAGING_DIR, Settings, SettingsStore
from .gdrive import DriveClient, DriveError
from .state import (CLOUD_VERIFIED, DELETED, DELETING, EXPORTED, EXPORTING, FAILED, NEW, UPLOADED,
                    UPLOADING, StateDB, now_iso)

NOTEBOOK_ATTEMPTS = 3          # whole-notebook retries inside one run
EXPORT_ATTEMPTS = 2            # extra tries when some content failed to download
MAX_CONSECUTIVE_FAILURES = 5   # then auto-pause (usually expired auth / rate limiting)
NB_FIELDS = ("id", "title", "emoji", "created_at", "year", "sources_count", "is_owner")


class Stopped(Exception):
    """Raised at a checkpoint when the user pressed Stop."""


# ----------------------------------------------------------------------------- event bus

class EventBus:
    """Fan-out of events to SSE subscribers; publish() is safe to call from worker threads."""

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def _deliver(self, ev: dict) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass

    def publish(self, ev: dict) -> None:
        loop = self._loop
        if loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._deliver(ev)
        else:
            loop.call_soon_threadsafe(self._deliver, ev)


# ----------------------------------------------------------------------------- policy

def may_delete(row: dict, opts: dict, s: Settings) -> tuple[bool, str]:
    """Single source of truth for the deletion decision. Returns (allowed, reason)."""
    if not row:
        return False, "unknown notebook"
    if not row.get("is_owner"):
        return False, "shared notebook (not owner)"
    if row.get("stage") != CLOUD_VERIFIED:
        return False, f"not cloud-verified (stage {row.get('stage')})"
    if not row.get("complete"):
        return False, "export incomplete — kept in NotebookLM"
    if not row.get("drive_folder_id"):
        return False, "no Drive folder recorded"
    if row["id"] in (opts.get("force_delete_ids") or ()):
        return True, "e2e self-test notebook"
    if opts.get("dry_run"):
        return False, "dry run"
    if not opts.get("delete", False) or not s.delete_after_verify:
        return False, "deletion disabled for this run"
    if not s.delete_confirmed:
        return False, "deletion not confirmed yet (type the confirmation in the UI)"
    return True, "verified in Drive"


# ----------------------------------------------------------------------------- runner

class Runner:
    def __init__(self, db: StateDB, settings: SettingsStore, drive: DriveClient, bus: EventBus,
                 client_factory: Callable[[], Any] = nlm.open_client, staging: Path = STAGING_DIR,
                 sleep: Callable[[float], Any] = asyncio.sleep):
        self.db, self.settings, self.drive, self.bus = db, settings, drive, bus
        self.client_factory = client_factory
        self.staging = staging
        self._sleep = sleep
        self._task: asyncio.Task | None = None
        self._stop = False
        self._resume = asyncio.Event()
        self._resume.set()
        self._lock = threading.Lock()
        self.status: dict[str, Any] = self._idle_status()

    # ------------------------------------------------------------------ status
    @staticmethod
    def _idle_status() -> dict:
        return {"state": "idle", "run_id": None, "dry_run": False, "delete": False, "total": 0,
                "done": 0, "archived": 0, "deleted": 0, "kept": 0, "failed": 0, "bytes_done": 0,
                "bytes_exported": 0, "started_at": None, "started_ts": None, "eta_sec": None,
                "current": None, "message": None}

    def snapshot(self) -> dict:
        with self._lock:
            st = dict(self.status)
            st["current"] = dict(st["current"]) if st["current"] else None
        if st["state"] in ("running", "paused") and st["started_ts"] and st["done"]:
            per = (time.time() - st["started_ts"]) / st["done"]
            st["eta_sec"] = int(per * max(st["total"] - st["done"], 0))
        return st

    def _set(self, **kw) -> None:
        with self._lock:
            self.status.update(kw)

    def _bump(self, key: str, n: int = 1) -> None:
        with self._lock:
            self.status[key] = (self.status.get(key) or 0) + n

    def _current(self, row: dict | None, stage: str | None = None, detail: str | None = None) -> None:
        with self._lock:
            if row is None:
                self.status["current"] = None
            else:
                cur = self.status.get("current") or {}
                if cur.get("id") != row["id"]:
                    cur = {"id": row["id"], "title": row.get("title"), "bytes": 0, "bytes_total": 0}
                if stage:
                    cur["stage"] = stage
                if detail is not None:
                    cur["detail"] = detail
                self.status["current"] = cur
        self.bus.publish({"type": "status"})

    def log(self, level: str, message: str, nb_id: str | None = None) -> None:
        ev = self.db.add_event(level, message, self.status.get("run_id"), nb_id)
        self.bus.publish({"type": "event", "event": ev})

    def _stage(self, nb_id: str, stage: str, **cols) -> None:
        self.db.set_stage(nb_id, stage, **cols)
        row = self.db.get(nb_id)
        self.bus.publish({"type": "notebook", "row": row})
        if row:
            self._current(row, stage)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ------------------------------------------------------------------ controls
    def recover_interrupted(self) -> None:
        n = self.db.reset_interrupted()
        if n:
            self.db.add_event("warn", f"{n} notebook(s) were interrupted mid-archive and will restart")

    async def start(self, opts: dict) -> int:
        if self.running:
            raise RuntimeError("A run is already in progress")
        s = self.settings.get()
        opts = {"dry_run": bool(opts.get("dry_run")),
                "delete": bool(opts.get("delete", s.delete_after_verify)),
                "limit": int(opts["limit"]) if opts.get("limit") else None,
                "ids": list(opts.get("ids") or []) or None,
                "force_delete_ids": list(opts.get("force_delete_ids") or []),
                "e2e": bool(opts.get("e2e"))}
        run_id = self.db.start_run(opts)
        self._stop = False
        self._resume.set()
        self.status = self._idle_status()
        self._set(state="running", run_id=run_id, dry_run=opts["dry_run"], delete=opts["delete"],
                  started_at=now_iso(), started_ts=time.time())
        self._task = asyncio.create_task(self._run(run_id, opts))
        return run_id

    def pause(self) -> None:
        if self.running:
            self._resume.clear()
            self._set(state="paused")
            self.log("info", "Paused — the current step finishes, then the run waits")

    def resume(self) -> None:
        if self.running:
            self._resume.set()
            self._set(state="running", message=None)
            self.log("info", "Resumed")

    def stop(self) -> None:
        if self.running:
            self._stop = True
            self._resume.set()
            self._set(state="stopping")
            self.log("info", "Stopping after the current step…")

    async def _checkpoint(self) -> None:
        if self._stop:
            raise Stopped()
        if not self._resume.is_set():
            self.bus.publish({"type": "status"})
            await self._resume.wait()
            if self._stop:
                raise Stopped()

    # ------------------------------------------------------------------ main loop
    async def _run(self, run_id: int, opts: dict) -> None:
        final = "completed"
        cm = client = None
        try:
            s = self.settings.get()
            if not opts["dry_run"] and not self.drive.connected():
                raise DriveError("Google Drive is not connected")
            if opts.get("e2e"):
                cm = self.client_factory()
                client = await cm.__aenter__()
                self.log("info", "E2E self-test: creating a throwaway notebook…")
                nb = await nlm.create_test_notebook(client)
                self.db.upsert_one(nb, selected=False)
                opts["ids"] = [nb["id"]]
                opts["force_delete_ids"] = [nb["id"]]
                self.log("ok", f"Created test notebook '{nb['title']}'", nb["id"])
            queue = self.db.work_queue(opts.get("ids"))
            if opts.get("limit"):
                queue = queue[: opts["limit"]]
            self._set(total=len(queue))
            mode = "DRY RUN" if opts["dry_run"] else ("archive + delete" if opts["delete"] else "archive only")
            self.log("info", f"Run #{run_id} started — {len(queue)} notebook(s), {mode}")
            if not queue:
                self.log("warn", "Nothing to do: no selected notebooks pending")
                return

            if opts["dry_run"]:
                for row in queue:
                    await self._checkpoint()
                    created = nlm._parse_iso(row["created_at"])
                    target = f"{s.drive_archive_path}/{year_dir(created)}/" \
                             f"{notebook_folder_name(created, row['title'], row['id'])}"
                    fake = dict(row, stage=CLOUD_VERIFIED, complete=1, drive_folder_id="(dry)")
                    ok, why = may_delete(fake, dict(opts, dry_run=False), s)
                    self.log("info", f"would archive → My Drive/{target} · delete afterwards: "
                                     f"{'yes' if ok else 'no (' + why + ')'}", row["id"])
                    self._bump("done")
                return

            if cm is None:
                cm = self.client_factory()
                client = await cm.__aenter__()
            opened = time.time()
            consecutive = 0
            for i, row in enumerate(queue):
                await self._checkpoint()
                s = self.settings.get()
                if time.time() - opened > s.auth_refresh_hours * 3600:
                    self.log("info", "Refreshing NotebookLM session…")
                    await cm.__aexit__(None, None, None)
                    cm = self.client_factory()
                    client = await cm.__aenter__()
                    opened = time.time()
                result = await self._process_with_retries(client, row["id"], opts)
                self._bump("done")
                consecutive = consecutive + 1 if result == FAILED else 0
                if consecutive >= MAX_CONSECUTIVE_FAILURES:
                    self.pause()
                    self._set(message=f"{consecutive} failures in a row — auto-paused. "
                                      "Check the NotebookLM login / Drive, then Resume.")
                    self.log("error", self.status["message"])
                    consecutive = 0
                if i < len(queue) - 1 and s.inter_notebook_delay_sec:
                    await self._sleep(s.inter_notebook_delay_sec)
        except Stopped:
            final = "stopped"
            self.log("warn", "Run stopped by user")
        except Exception as e:  # noqa: BLE001
            final = "error"
            self.log("error", f"Run aborted: {type(e).__name__}: {e}"[:500])
        finally:
            if cm is not None:
                try:
                    await cm.__aexit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass
            st = self.snapshot()
            totals = {k: st[k] for k in ("total", "done", "archived", "deleted", "kept", "failed", "bytes_done")}
            self.db.finish_run(run_id, final, totals)
            self.log("ok" if final == "completed" else "warn",
                     f"Run #{run_id} {final}: {totals['archived']} archived, {totals['deleted']} deleted, "
                     f"{totals['kept']} kept, {totals['failed']} failed")
            self._set(state="idle", current=None, finished=final)
            self.bus.publish({"type": "run_finished", "status": final})

    async def _process_with_retries(self, client, nb_id: str, opts: dict) -> str:
        last: Exception | None = None
        for attempt in range(1, NOTEBOOK_ATTEMPTS + 1):
            row = self.db.get(nb_id)
            try:
                return await self.process(client, row, opts)
            except Stopped:
                cur = self.db.get(nb_id)
                if cur and cur["stage"] in (EXPORTING, EXPORTED, UPLOADING, UPLOADED):
                    self._stage(nb_id, NEW, error="stopped — will restart")
                raise
            except Exception as e:  # noqa: BLE001
                last = e
                msg = f"{type(e).__name__}: {e}"[:400]
                if attempt < NOTEBOOK_ATTEMPTS:
                    self.log("warn", f"attempt {attempt} failed ({msg}) — retrying", nb_id)
                    await self._sleep(5 * attempt * attempt)
                    await self._checkpoint()
        cur = self.db.get(nb_id) or {}
        msg = f"{type(last).__name__}: {last}"[:400]
        keep = cur.get("stage") in (CLOUD_VERIFIED, DELETING)  # archive is fine, only delete failed
        if keep:
            self.db.update(nb_id, error=msg, attempts=(cur.get("attempts") or 0) + 1)
        else:
            self._stage(nb_id, FAILED, error=msg, attempts=(cur.get("attempts") or 0) + 1)
        self._bump("failed")
        self.log("error", f"FAILED: {msg}", nb_id)
        return FAILED

    # ------------------------------------------------------------------ one notebook
    async def process(self, client, row: dict, opts: dict) -> str:
        s = self.settings.get()
        nb_id = row["id"]
        self._current(row, row["stage"], "")
        created = nlm._parse_iso(row["created_at"])
        ydir = year_dir(created)
        folder_name = notebook_folder_name(created, row["title"], nb_id)
        rel_folder = f"{ydir}/{folder_name}"
        root_parts = [p.strip() for p in s.drive_archive_path.replace("\\", "/").split("/") if p.strip()]

        stage = row["stage"]
        if stage == DELETING:  # interrupted while deleting
            if await nlm.get_notebook(client, nb_id) is None:
                self._stage(nb_id, DELETED, deleted_at=now_iso(), present=0, error=None)
                self._bump("deleted")
                self.log("ok", "Deletion confirmed (was interrupted earlier)", nb_id)
                return DELETED
            self.db.set_stage(nb_id, CLOUD_VERIFIED)
            stage = CLOUD_VERIFIED
        if stage == CLOUD_VERIFIED:
            prev = json.loads(row.get("stats") or "{}")
            archived_n = prev.get("archived_sources_count")
            if archived_n is not None and row.get("sources_count") is not None \
                    and archived_n != row["sources_count"]:
                self.log("warn", f"Notebook changed since archiving ({archived_n} → {row['sources_count']} "
                                 "sources) — re-archiving", nb_id)
            else:
                return await self._finish(client, nb_id, opts, s, root_parts, reverify=True)

        # ---- export ------------------------------------------------------
        await self._checkpoint()
        dest = self.staging / nb_id
        self._stage(nb_id, EXPORTING, folder=rel_folder, error=None, complete=0)
        self.log("info", f"Exporting '{row['title']}'", nb_id)
        nb = {k: row.get(k) for k in NB_FIELDS}
        complete = True
        stats: dict = {}
        for attempt in range(1, EXPORT_ATTEMPTS + 1):
            try:
                stats = await nlm.export_notebook(
                    client, nb, dest, include_media=s.include_media, include_chat=s.include_chat,
                    progress=lambda m: self._current(row, EXPORTING, m), check_cancel=self._checkpoint)
                complete = bool(stats.get("complete", True))
                break
            except nlm.IncompleteExport as e:
                if attempt < EXPORT_ATTEMPTS:
                    self.log("warn", f"Some content failed ({e}) — retrying export", nb_id)
                    await self._sleep(5)
                    continue
                complete = False
                stats = self._stats_from_dest(dest)
                stats["problems"] = [str(e)]
                self.log("warn", f"Export incomplete: {e}. Archiving what exists; notebook will be KEPT.", nb_id)
        manifest = json.loads((dest / MANIFEST_NAME).read_text(encoding="utf-8"))
        expected = manifest_with_self(dest, manifest)
        total_bytes = sum(m["size"] for m in expected.values())
        stats["archived_sources_count"] = row.get("sources_count")
        self._bump("bytes_exported", total_bytes)
        self._stage(nb_id, EXPORTED, files=len(expected), bytes=total_bytes,
                    stats=json.dumps(stats, default=str), complete=0)

        # ---- upload ------------------------------------------------------
        await self._checkpoint()
        self._stage(nb_id, UPLOADING)
        with self._lock:
            if self.status.get("current"):
                self.status["current"].update(bytes=0, bytes_total=total_bytes)
        self.log("info", f"Uploading {len(expected)} files ({total_bytes / 1048576:.1f} MB) → "
                         f"My Drive/{'/'.join(root_parts)}/{rel_folder}", nb_id)
        folder_id = await asyncio.to_thread(self._upload_tree, dest, expected, root_parts + [ydir],
                                            folder_name, s.upload_concurrency)
        self._stage(nb_id, UPLOADED, drive_folder_id=folder_id)

        # ---- cloud verification -----------------------------------------
        self._current(row, UPLOADED, "verifying in Drive")
        exp_md5 = {rel: {"md5": m["md5"], "size": m["size"]} for rel, m in expected.items()}
        v = await asyncio.to_thread(self.drive.verify_folder, folder_id, exp_md5)
        if not v["ok"]:
            raise DriveError(f"cloud verification failed: {v['detail']} {v['missing'][:3]} {v['mismatched'][:3]}")
        self._stage(nb_id, CLOUD_VERIFIED, complete=int(complete),
                    cloud_detail=json.dumps({"verify": v, "expected": exp_md5}))
        self._bump("archived")
        self.log("ok", f"Verified in Drive: {v['detail']}", nb_id)
        if not s.keep_staging:
            shutil.rmtree(dest, ignore_errors=True)
        return await self._finish(client, nb_id, opts, s, root_parts, reverify=False)

    async def _finish(self, client, nb_id: str, opts: dict, s: Settings, root_parts: list[str],
                      reverify: bool) -> str:
        row = self.db.get(nb_id)
        ok, why = may_delete(row, opts, s)
        if not ok:
            if not row.get("complete"):
                self._bump("kept")
            self.log("info", f"Kept in NotebookLM: {why}", nb_id)
            await asyncio.to_thread(self._update_catalog, root_parts)
            return row["stage"]
        if reverify:
            detail = json.loads(row.get("cloud_detail") or "{}")
            expected = detail.get("expected")
            if not expected:
                self._stage(nb_id, NEW, error="no verification record — re-archiving")
                raise DriveError("missing verification record; will re-archive")
            v = await asyncio.to_thread(self.drive.verify_folder, row["drive_folder_id"], expected)
            if not v["ok"]:
                self._stage(nb_id, NEW, error=f"Drive copy changed: {v['detail']}")
                raise DriveError(f"re-verification failed ({v['detail']}); will re-archive")
        await self._checkpoint()
        self._stage(nb_id, DELETING)
        await nlm.delete_notebook(client, nb_id)
        self._stage(nb_id, DELETED, deleted_at=now_iso(), present=0, error=None)
        self._bump("deleted")
        self.log("ok", "Deleted from NotebookLM (after Drive verification)", nb_id)
        await asyncio.to_thread(self._update_catalog, root_parts)
        return DELETED

    # ------------------------------------------------------------------ helpers (threads)
    def _upload_tree(self, root: Path, expected: dict, year_parts: list[str], folder_name: str,
                     concurrency: int) -> str:
        d = self.drive
        year_id = d.ensure_path(year_parts)
        if d.trash_child_folder(year_id, folder_name):
            self.log("warn", f"Replaced a previous partial upload of '{folder_name}' (moved to Drive trash)")
        folder_id = d.ensure_folder(year_id, folder_name)
        dirs: set[str] = set()
        for rel in expected:
            p = PurePosixPath(rel).parent
            while str(p) not in (".", ""):
                dirs.add(str(p))
                p = p.parent
        dir_ids = {".": folder_id}
        for dpath in sorted(dirs, key=lambda x: (x.count("/"), x)):
            pp = PurePosixPath(dpath)
            dir_ids[dpath] = d.ensure_folder(dir_ids[str(pp.parent)], pp.name)

        def on_bytes(n: int) -> None:
            with self._lock:
                self.status["bytes_done"] += n
                cur = self.status.get("current")
                if cur:
                    cur["bytes"] = cur.get("bytes", 0) + n

        def up(rel: str) -> None:
            pp = PurePosixPath(rel)
            res = d.upload_file(root / rel, dir_ids[str(pp.parent)], pp.name, progress=on_bytes)
            exp = expected[rel]
            if res.get("md5Checksum") and res["md5Checksum"] != exp["md5"]:
                raise DriveError(f"md5 mismatch after upload: {rel}")
            if int(res.get("size", exp["size"]) or 0) != exp["size"]:
                raise DriveError(f"size mismatch after upload: {rel}")

        with ThreadPoolExecutor(max_workers=max(1, int(concurrency))) as pool:
            futs = {pool.submit(up, rel): rel for rel in sorted(expected)}
            for f in as_completed(futs):
                if self._stop:
                    for other in futs:
                        other.cancel()
                if f.cancelled():
                    continue
                f.result()
                self.bus.publish({"type": "status"})
        if self._stop:
            raise Stopped()
        return folder_id

    def _update_catalog(self, root_parts: list[str]) -> None:
        try:
            csv_text, md_text = render_catalog(self.db.list_notebooks())
            root_id = self.drive.ensure_path(root_parts)
            self.drive.put_text(root_id, "_catalog.md", md_text, "text/markdown")
            self.drive.put_text(root_id, "_catalog.csv", csv_text, "text/csv")
        except Exception as e:  # noqa: BLE001 — catalog is a convenience, never fail a notebook for it
            self.db.add_event("warn", f"catalog update failed: {e}"[:300], self.status.get("run_id"))

    @staticmethod
    def _stats_from_dest(dest: Path) -> dict:
        try:
            meta = json.loads((dest / "metadata.json").read_text(encoding="utf-8"))
            return dict(meta.get("stats") or {})
        except Exception:  # noqa: BLE001
            return {}
