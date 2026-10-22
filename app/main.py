"""nlm2drive web API + static UI (FastAPI). Bound to localhost by docker-compose."""
from __future__ import annotations

import asyncio
import csv
import io
import json
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import nlm_service as nlm
from . import reconnect
from .config import DB_PATH, STAGING_DIR, SettingsStore, ensure_dirs
from .gdrive import DriveClient
from .runner import EventBus, Runner
from .state import StateDB

STATIC = Path(__file__).parent / "static"
DELETE_PHRASE = "DELETE AFTER BACKUP"

ensure_dirs()
db = StateDB(DB_PATH)
settings = SettingsStore()
drive = DriveClient()
bus = EventBus()
runner = Runner(db, settings, drive, bus)

_cache: dict[str, tuple[float, Any]] = {}
_reconnect_seen: dict[str, Any] = {"finished_at": None}


def _cached(key: str, ttl: float):
    hit = _cache.get(key)
    return hit[1] if hit and time.time() - hit[0] < ttl else None


def _store(key: str, value: Any) -> Any:
    _cache[key] = (time.time(), value)
    return value


@asynccontextmanager
async def lifespan(_app: FastAPI):
    bus.bind(asyncio.get_running_loop())
    runner.recover_interrupted()
    yield
    if runner.running:
        runner.stop()


app = FastAPI(title="nlm2drive", lifespan=lifespan)


# ----------------------------------------------------------------------------- models

class SelectBody(BaseModel):
    ids: list[str]
    selected: bool = True


class YearsBody(BaseModel):
    years: list[int]
    only: bool = True


class CutoffBody(BaseModel):
    before: str
    only: bool = True


class PresetBody(BaseModel):
    preset: str
    only: bool = True


class RunBody(BaseModel):
    dry_run: bool = False
    delete: bool | None = None
    limit: int | None = None
    ids: list[str] | None = None


class SettingsBody(BaseModel):
    years: list[int] | None = None
    include_media: bool | None = None
    include_chat: bool | None = None
    delete_after_verify: bool | None = None
    drive_archive_path: str | None = None
    upload_concurrency: int | None = None
    inter_notebook_delay_sec: float | None = None
    auth_refresh_hours: float | None = None
    keep_staging: bool | None = None


class ConfirmBody(BaseModel):
    phrase: str


# ----------------------------------------------------------------------------- helpers

def _row_out(r: dict) -> dict:
    r = dict(r)
    for k in ("stats", "cloud_detail"):
        if r.get(k):
            try:
                r[k] = json.loads(r[k])
            except ValueError:
                pass
    if isinstance(r.get("cloud_detail"), dict):
        r["cloud_detail"].pop("expected", None)  # large; not needed by the UI
    return r


def _run_auth_view() -> dict | None:
    """While a run owns the NotebookLM session, never open a second client on the same cookie file
    (two clients rotating __Secure-1PSIDTS concurrently can get the whole Google session revoked).
    Report what the run itself knows instead."""
    if not runner.running:
        return None
    st = runner.snapshot()
    last = _cache.get("auth", (0, {}))[1] or {}
    if st.get("needs_auth"):
        return {**last, "session": True, "ok": False, "error": "Session expired — sign in again (run is waiting)",
                "run_owned": True}
    return {**last, "session": True, "ok": True, "error": None, "run_owned": True,
            "email": last.get("email")}


def _known_present_ids() -> list[str]:
    return [r["id"] for r in db.list_notebooks() if r.get("present") and r.get("stage") != "DELETED"]


async def _auth_status(force: bool = False) -> dict:
    if (view := _run_auth_view()) is not None:
        runner.nudge()  # a manual re-check while the run waits for a sign-in = look right now
        return view
    if not force and (hit := _cached("auth", 30)):
        return hit
    st, items = await nlm.account_status_with_items(_known_present_ids())
    if st.get("ok") and items is not None:
        db.upsert_listing(items, settings.get().years)
        bus.publish({"type": "refresh"})
    return _store("auth", st)


async def _drive_status(force: bool = False) -> dict:
    if not force and (hit := _cached("drive", 120)):
        return hit
    return _store("drive", await asyncio.to_thread(drive.status))


# ----------------------------------------------------------------------------- core API

@app.get("/api/health")
async def health():
    return {"ok": True}


@app.get("/api/summary")
async def summary():
    s = settings.get()
    counts = db.counts()
    return {"status": runner.snapshot(), "counts": counts, "settings": s.__dict__,
            "limit": s.notebook_limit, "delete_phrase": DELETE_PHRASE,
            "auth": _cached("auth", 3600), "drive": _cached("drive", 3600)}


@app.get("/api/notebooks")
async def notebooks():
    return [_row_out(r) for r in db.list_notebooks()]


@app.post("/api/notebooks/select")
async def select(body: SelectBody):
    db.set_selected(body.ids, body.selected)
    return {"ok": True}


@app.post("/api/notebooks/select-years")
async def select_years(body: YearsBody):
    db.select_years(body.years, body.only)
    return {"ok": True}


@app.post("/api/notebooks/select-cutoff")
async def select_cutoff(body: CutoffBody):
    db.select_cutoff(body.before, body.only)
    return {"ok": True}


ALLOWED_PRESETS = {"empty", "stale", "oldest25", "oldest50"}


@app.post("/api/notebooks/select-preset")
async def select_preset(body: PresetBody):
    if body.preset not in ALLOWED_PRESETS:
        raise HTTPException(400, f"Unknown preset '{body.preset}'. Allowed: {', '.join(sorted(ALLOWED_PRESETS))}")
    count = db.select_preset(body.preset, body.only)
    return {"ok": True, "count": count}


@app.post("/api/refresh")
async def refresh():
    if runner.running:
        raise HTTPException(409, "A run is in progress — it owns the NotebookLM session. Refresh the list after it finishes.")
    try:
        async with nlm.open_client() as client:
            items = await nlm.list_notebooks(client, _known_present_ids())
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Could not list notebooks: {type(e).__name__}: {e}"[:400]) from e
    db.upsert_listing(items, settings.get().years)
    st = _cached("auth", 3600) or {}
    _store("auth", {**st, "session": True, "ok": True, "notebooks": len(items), "error": None})
    db.add_event("info", f"Listing refreshed: {len(items)} notebooks in the account")
    bus.publish({"type": "refresh"})
    return {"ok": True, "count": len(items)}


async def _refuse_during_sign_in() -> None:
    snap = await reconnect.status()
    if snap.get("state") == "running":
        raise HTTPException(409, "A NotebookLM sign-in is in progress - finish it in the browser window first.")


@app.post("/api/run")
async def run(body: RunBody):
    await _refuse_during_sign_in()
    opts = body.model_dump()
    if opts["delete"] is None:
        opts["delete"] = settings.get().delete_after_verify
    try:
        run_id = await runner.start(opts)
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True, "run_id": run_id}


@app.post("/api/test-e2e")
async def test_e2e():
    await _refuse_during_sign_in()
    try:
        run_id = await runner.start({"e2e": True, "delete": True})
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True, "run_id": run_id}


@app.post("/api/pause")
async def pause():
    runner.pause()
    return runner.snapshot()


@app.post("/api/resume")
async def resume():
    runner.resume()
    return runner.snapshot()


@app.post("/api/stop")
async def stop():
    runner.stop()
    return runner.snapshot()


@app.post("/api/notebooks/{nb_id}/retry")
async def retry(nb_id: str):
    row = db.get(nb_id)
    if not row:
        raise HTTPException(404, "unknown notebook")
    if row["stage"] == "FAILED":
        db.set_stage(nb_id, "NEW", error=None)
    db.set_selected([nb_id], True)
    return {"ok": True}


def _safe_staging_dir(nb_id: str) -> Path:
    if not nb_id or "/" in nb_id or "\\" in nb_id or ".." in nb_id:
        raise HTTPException(400, "Invalid notebook ID")
    target = (STAGING_DIR / nb_id).resolve()
    if not target.is_relative_to(STAGING_DIR.resolve()):
        raise HTTPException(400, "Invalid notebook ID")
    return target


@app.post("/api/notebooks/{nb_id}/restore")
async def restore_notebook(nb_id: str):
    await _refuse_during_sign_in()
    if runner.running:
        raise HTTPException(409, "A run is in progress — wait until it finishes to restore.")
    _safe_staging_dir(nb_id)
    row = db.get(nb_id)
    if not row:
        raise HTTPException(404, "Unknown notebook")
    if not row.get("is_owner"):
        raise HTTPException(403, "Shared notebooks cannot be restored (Invariant 2)")
    if row.get("present"):
        return {
            "ok": True,
            "already_present": True,
            "id": nb_id,
            "title": row["title"],
            "url": f"https://notebooklm.google.com/notebook/{nb_id}",
        }

    sources: list[dict[str, str]] = []
    st_dir = _safe_staging_dir(nb_id) / "Sources"
    if st_dir.exists():
        for p in sorted(st_dir.glob("*.md")):
            try:
                sources.append({"title": p.stem, "content": p.read_text(encoding="utf-8")})
            except Exception:
                pass

    try:
        async with nlm.open_client() as client:
            new_nb = await client.notebooks.create(title=row["title"] or "Restored Notebook")
            added = 0
            for src in sources:
                try:
                    await client.sources.add_text(new_nb.id, title=src["title"], content=src["content"], wait=True)
                    added += 1
                except Exception:
                    pass
    except Exception as e:
        raise HTTPException(502, f"Could not restore notebook in NotebookLM: {e}") from e

    db.upsert_one({
        "id": new_nb.id,
        "title": new_nb.title,
        "is_owner": True,
        "sources_count": added,
        "year": datetime.now(UTC).year,
    })
    db.add_event("ok", f"Restored notebook '{row['title']}' as {new_nb.id} in NotebookLM ({added} sources re-uploaded)",
                 notebook_id=new_nb.id)
    bus.publish({"type": "refresh"})
    return {
        "ok": True,
        "already_present": False,
        "new_id": new_nb.id,
        "title": new_nb.title,
        "url": f"https://notebooklm.google.com/notebook/{new_nb.id}",
        "sources_restored": added,
    }


@app.get("/api/notebooks/{nb_id}/content")
async def notebook_content(nb_id: str):
    nb_dir = _safe_staging_dir(nb_id)
    row = db.get(nb_id)
    if not row:
        raise HTTPException(404, "Unknown notebook")
    manifest_file = nb_dir / "manifest.json"
    manifest = None
    if manifest_file.exists():
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    sources: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    audio_file: str | None = None

    if nb_dir.exists():
        src_dir = nb_dir / "Sources"
        if src_dir.exists():
            for p in sorted(src_dir.glob("*.*")):
                if p.is_file():
                    sources.append({"name": p.name, "size": p.stat().st_size})
        note_dir = nb_dir / "Notes"
        if note_dir.exists():
            for p in sorted(note_dir.glob("*.*")):
                if p.is_file():
                    notes.append({"name": p.name, "size": p.stat().st_size})
        for ext in (".wav", ".mp3", ".m4a"):
            for p in nb_dir.rglob(f"*{ext}"):
                if p.is_file():
                    audio_file = p.name
                    break

    stats = None
    if row.get("stats"):
        try:
            stats = json.loads(row["stats"])
        except Exception:
            pass

    return {
        "id": nb_id,
        "title": row["title"],
        "has_staging": nb_dir.exists(),
        "manifest": manifest,
        "sources": sources,
        "notes": notes,
        "audio_file": audio_file,
        "stats": stats,
    }


@app.get("/api/notebooks/{nb_id}/audio")
async def notebook_audio(nb_id: str):
    nb_dir = _safe_staging_dir(nb_id)
    if nb_dir.exists():
        for ext, mime in ((".wav", "audio/wav"), (".mp3", "audio/mpeg"), (".m4a", "audio/mp4")):
            for p in nb_dir.rglob(f"*{ext}"):
                resolved = p.resolve()
                if resolved.is_file() and resolved.is_relative_to(nb_dir.resolve()):
                    return FileResponse(resolved, media_type=mime, filename=resolved.name)
    raise HTTPException(404, "No local audio file found for this notebook")


@app.get("/api/export/csv")
async def export_csv():
    rows = db.list_notebooks()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([
        "id", "title", "emoji", "created_at", "year", "sources_count", "is_owner",
        "selected", "stage", "complete", "files", "bytes", "drive_folder_id", "folder",
        "error", "deleted_at", "updated_at"
    ])
    for r in rows:
        writer.writerow([
            r.get("id", ""), r.get("title", ""), r.get("emoji", ""), r.get("created_at", ""),
            r.get("year", ""), r.get("sources_count", ""), r.get("is_owner", ""),
            r.get("selected", ""), r.get("stage", ""), r.get("complete", ""),
            r.get("files", ""), r.get("bytes", ""), r.get("drive_folder_id", ""),
            r.get("folder", ""), r.get("error", ""), r.get("deleted_at", ""), r.get("updated_at", "")
        ])
    return Response(
        content=out.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="notebooklm-archive-catalog.csv"'}
    )


@app.get("/api/export/json")
async def export_json():
    rows = [_row_out(r) for r in db.list_notebooks()]
    data = {
        "exported_at": nlm._iso(datetime.now(UTC)),
        "counts": db.counts(),
        "notebooks": rows,
    }
    return Response(
        content=json.dumps(data, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="notebooklm-archive-catalog.json"'}
    )


@app.post("/api/audit")
async def audit_archive():
    if not drive.connected():
        raise HTTPException(400, "Google Drive is not connected.")
    archived = db.by_stage("CLOUD_VERIFIED", "DELETED")
    total = len(archived)
    verified = 0
    missing = 0
    issues: list[dict[str, Any]] = []

    sem = asyncio.Semaphore(8)

    async def _check(nb: dict[str, Any]) -> tuple[dict[str, Any], bool, str | None]:
        fid = nb.get("drive_folder_id")
        if not fid:
            return nb, False, "Missing drive_folder_id"
        async with sem:
            try:
                exists = await asyncio.to_thread(drive.folder_exists, fid)
                if exists:
                    return nb, True, None
                return nb, False, "Folder not found in Drive"
            except Exception as e:
                return nb, False, str(e)

    results = await asyncio.gather(*[_check(nb) for nb in archived]) if archived else []
    for nb, ok, err in results:
        if ok:
            verified += 1
        else:
            missing += 1
            issues.append({"id": nb["id"], "title": nb["title"], "issue": err})

    db.add_event("info", f"Drive archive audit: {verified}/{total} verified intact, {missing} issues")
    return {"total": total, "verified": verified, "missing": missing, "issues": issues[:20]}



@app.get("/api/settings")
async def get_settings():
    return settings.get().__dict__


@app.post("/api/settings")
async def post_settings(body: SettingsBody):
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    if "upload_concurrency" in changes:
        changes["upload_concurrency"] = max(1, min(8, changes["upload_concurrency"]))
    if "drive_archive_path" in changes:
        p = changes["drive_archive_path"].replace("\\", "/").strip("/ ")
        if p.lower().startswith("my drive/"):
            p = p[9:]
        if not p:
            raise HTTPException(400, "Archive path cannot be empty")
        changes["drive_archive_path"] = p
    return settings.update(**changes).__dict__


@app.post("/api/confirm-delete")
async def confirm_delete(body: ConfirmBody):
    if body.phrase.strip().upper() != DELETE_PHRASE:
        raise HTTPException(400, f'Type exactly: {DELETE_PHRASE}')
    s = settings.update(delete_confirmed=True)
    db.add_event("warn", "Automatic deletion after cloud verification was CONFIRMED by the user")
    return s.__dict__


@app.post("/api/revoke-delete")
async def revoke_delete():
    return settings.update(delete_confirmed=False).__dict__


@app.get("/api/runs")
async def runs():
    return db.runs(30)


@app.get("/api/events/history")
async def events_history(notebook_id: str | None = None, limit: int = 300):
    return db.events(limit, notebook_id)


@app.get("/api/events")
async def events(request: Request):
    q = bus.subscribe()

    async def gen():
        try:
            yield f"data: {json.dumps({'type': 'status', 'status': runner.snapshot()})}\n\n"
            last_status = 0.0
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=1.0)
                except TimeoutError:
                    ev = None
                if ev is not None and ev.get("type") != "status":
                    if ev.get("type") == "notebook" and ev.get("row"):
                        ev = {"type": "notebook", "row": _row_out(ev["row"])}
                    yield f"data: {json.dumps(ev, default=str)}\n\n"
                now = time.time()
                if now - last_status >= 0.5:
                    last_status = now
                    yield f"data: {json.dumps({'type': 'status', 'status': runner.snapshot()}, default=str)}\n\n"
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ----------------------------------------------------------------------------- NotebookLM auth

@app.get("/api/auth/status")
async def auth_status(force: bool = False):
    return await _auth_status(force)


@app.post("/api/auth/reload")
async def auth_reload():
    _cache.pop("auth", None)
    return await _auth_status(True)


@app.post("/api/auth/import")
async def auth_import(file: UploadFile = File(...)):
    """Accept a Playwright storage_state.json or a browser cookie export (list of cookies)."""
    raw = await file.read()
    try:
        data = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "Not valid JSON") from None
    if isinstance(data, list):
        cookies = []
        for c in data:
            if not isinstance(c, dict) or "name" not in c:
                continue
            same = str(c.get("sameSite", "Lax")).capitalize()
            cookies.append({"name": c["name"], "value": c.get("value", ""), "domain": c.get("domain", ".google.com"),
                            "path": c.get("path", "/"),
                            "expires": float(c.get("expirationDate", c.get("expires", -1)) or -1),
                            "httpOnly": bool(c.get("httpOnly", False)), "secure": bool(c.get("secure", True)),
                            "sameSite": same if same in ("Lax", "Strict", "None") else "Lax"})
        data = {"cookies": cookies, "origins": []}
    if not isinstance(data, dict) or not data.get("cookies"):
        raise HTTPException(400, "No cookies found in file")
    target = nlm.storage_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data), encoding="utf-8")
    _cache.pop("auth", None)
    runner.nudge()  # the run (if waiting for a sign-in) picks the new cookies up immediately
    return await _auth_status(True)


class ReconnectBody(BaseModel):
    fresh: bool = False


def _same_machine_origin(request: Request) -> None:
    """State-changing reconnect calls open a browser window on this PC: refuse cross-site requests (CSRF /
    DNS rebinding). Non-browser clients (curl, the CLI) send no Origin and are allowed."""
    origin = request.headers.get("origin")
    if origin is None:
        return
    if (urlparse(origin).hostname or "") not in ("localhost", "127.0.0.1"):
        raise HTTPException(403, "Cross-site request refused")


@app.get("/api/reconnect/status")
async def reconnect_status():
    snap = await reconnect.status()
    done = snap.get("finished_at")
    if snap.get("state") == "success" and done and done != _reconnect_seen["finished_at"]:
        _reconnect_seen["finished_at"] = done  # act once per successful sign-in
        _cache.pop("auth", None)
        runner.nudge()  # a paused run picks the new session up right away
        db.add_event("ok", "NotebookLM sign-in completed through the Reconnect button")
    return snap


@app.post("/api/reconnect")
async def reconnect_start(request: Request, body: ReconnectBody | None = None):
    _same_machine_origin(request)
    if runner.running and not runner.snapshot().get("needs_auth"):
        # The run owns a working session; re-signing in now would rewrite the cookie file under its feet.
        raise HTTPException(409, "The running archive is using a working NotebookLM session - reconnect is only "
                                 "needed (and offered) when it reports “Sign-in needed”.")
    try:
        snap = await reconnect.start(bool(body and body.fresh))
    except reconnect.HelperUnavailable as e:
        raise HTTPException(503, f"{e} {reconnect.HINT}") from e
    if not snap.get("already_running"):
        db.add_event("info", "Reconnect NotebookLM requested - sign-in browser opened on the host")
    return snap


@app.post("/api/reconnect/cancel")
async def reconnect_cancel(request: Request):
    _same_machine_origin(request)
    try:
        return await reconnect.cancel()
    except reconnect.HelperUnavailable as e:
        raise HTTPException(503, str(e)) from e


# ----------------------------------------------------------------------------- Google Drive OAuth

@app.get("/api/drive/status")
async def drive_status(force: bool = False):
    return await _drive_status(force)


@app.post("/api/drive/credentials")
async def drive_credentials(file: UploadFile = File(...)):
    try:
        drive.save_client(await file.read())
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    _cache.pop("drive", None)
    return await _drive_status(True)


@app.get("/api/drive/connect")
async def drive_connect():
    try:
        return {"url": drive.auth_url()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)) from e


@app.get("/api/drive/callback")
async def drive_callback(state: str = "", code: str = "", error: str = ""):
    if error or not code:
        return RedirectResponse(f"/?drive_error={error or 'no_code'}")
    try:
        await asyncio.to_thread(drive.finish, state, code)
    except Exception as e:  # noqa: BLE001
        msg = str(e).replace("<", "&lt;")[:400]
        return HTMLResponse(f"<h3>Drive connection failed</h3><p>{msg}</p><a href='/'>Back</a>", status_code=400)
    _cache.pop("drive", None)
    await _drive_status(True)
    return RedirectResponse("/?drive=connected")


@app.post("/api/drive/disconnect")
async def drive_disconnect():
    drive.disconnect()
    _cache.pop("drive", None)
    return await _drive_status(True)


# ----------------------------------------------------------------------------- static UI

@app.exception_handler(HTTPException)
async def http_exc(_req: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")
