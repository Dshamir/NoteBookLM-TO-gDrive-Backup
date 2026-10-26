"""NotebookLM access via notebooklm-py: list, full export to a staging folder, delete.

Only read/download/delete endpoints are used — never AI-generation endpoints — so archiving does
not consume NotebookLM usage quota.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .archive import build_manifest, human_bytes, safe_filename, safe_name, unique_path
from .config import NOTEBOOKLM_HOME

ProgressFn = Callable[[str], Awaitable[None] | None]


class NotebookLMAuthError(RuntimeError):
    """The NotebookLM Google session is missing, expired or revoked — a human must sign in again."""


class IncompleteExport(RuntimeError):
    """A piece of content that exists could not be exported — never delete such a notebook."""


_AUTH_MARKERS = (
    "unauthenticated", "authentication expired", "csrf token", "snlm0e", "run 'notebooklm login'",
    "missing required cookies", "cookiemismatch", "redirected to sign-in", "no notebooklm session",
)
RPC_UNAUTHENTICATED = 16  # gRPC status code


def is_auth_error(exc: BaseException | None) -> bool:
    """True when `exc` (or anything in its cause chain) means 'the Google session is no longer valid'.

    Deliberately narrow: rate limits, 'resource exhausted' and plain network errors are NOT auth errors.
    """
    seen = 0
    while exc is not None and seen < 8:
        if isinstance(exc, NotebookLMAuthError):
            return True
        if type(exc).__name__ in ("AuthError", "AuthExtractionError", "HeadlessReauthError",
                                  "HeadlessLoginRequiredError"):
            return True
        if getattr(exc, "rpc_code", None) == RPC_UNAUTHENTICATED:
            return True
        text = str(exc).lower()
        if any(m in text for m in _AUTH_MARKERS):
            return True
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return False


def _reraise_if_auth(exc: BaseException) -> None:
    """Inside the exporter's tolerant `except` blocks: an expired session must abort, not degrade."""
    if is_auth_error(exc):
        raise NotebookLMAuthError(f"{type(exc).__name__}: {exc}"[:300]) from exc


def _iso(dt: Any) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.isoformat(timespec="seconds")
    return str(dt)


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _enum_val(v: Any) -> Any:
    return getattr(v, "value", v)


def storage_file() -> Path:
    return NOTEBOOKLM_HOME / "profiles" / "default" / "storage_state.json"


def has_session() -> bool:
    return storage_file().exists() or (NOTEBOOKLM_HOME / "storage_state.json").exists() \
        or bool(os.environ.get("NOTEBOOKLM_AUTH_JSON"))


def storage_mtime() -> float:
    """mtime of the saved session file (0 if none) — changes when the user signs in again."""
    for p in (storage_file(), NOTEBOOKLM_HOME / "storage_state.json"):
        try:
            return p.stat().st_mtime
        except OSError:
            continue
    return 0.0


def open_client():
    """Async context manager for an authenticated client (keepalive rotates cookies on long runs)."""
    from notebooklm import NotebookLMClient
    if not has_session():
        raise NotebookLMAuthError("No NotebookLM session. Run ./nlm2drive.sh login (or import cookies in the UI).")
    return NotebookLMClient.from_storage(keepalive=900)


def notebook_to_dict(nb: Any) -> dict:
    created = getattr(nb, "created_at", None)
    return {
        "id": nb.id,
        "title": nb.title or "Untitled notebook",
        "emoji": getattr(nb, "emoji", None),
        "created_at": _iso(created),
        "modified_at": _iso(getattr(nb, "modified_at", None)),
        "last_viewed_at": _iso(getattr(nb, "last_viewed_at", None)),
        "year": created.year if isinstance(created, datetime) else None,
        "sources_count": getattr(nb, "sources_count", None),
        "is_owner": bool(getattr(nb, "is_owner", True)),
        "role": str(_enum_val(getattr(nb, "role", "")) or ""),
    }


async def _maybe(cb: ProgressFn | None, msg: str) -> None:
    if cb:
        r = cb(msg)
        if asyncio.iscoroutine(r):
            await r


# ---------------------------------------------------------------- listing

async def account_status_with_items(known_present_ids: Iterable[str] = ()) -> tuple[dict, list[dict] | None]:
    st: dict[str, Any] = {"session": has_session(), "ok": False, "email": None, "notebooks": None, "error": None}
    if not st["session"]:
        st["error"] = "No session — press Reconnect to sign in"
        return st, None
    try:
        async with open_client() as client:
            try:
                st["email"] = await client.get_account_email()
            except Exception:
                pass
            items = await list_notebooks(client, known_present_ids)
            st.update(ok=True, notebooks=len(items))
            return st, items
    except Exception as e:
        st["error"] = f"{type(e).__name__}: {e}"[:400]
        return st, None


async def account_status(known_present_ids: Iterable[str] = ()) -> dict:
    st, _ = await account_status_with_items(known_present_ids)
    return st


async def list_notebooks(client, known_present_ids: Iterable[str] = ()) -> list[dict]:
    items = [notebook_to_dict(nb) for nb in await client.notebooks.list()]
    seen = {it["id"] for it in items}
    missing = [nid for nid in known_present_ids if nid not in seen]
    if missing and hasattr(client.notebooks, "get_or_none"):
        sem = asyncio.Semaphore(5)

        async def _probe_missing(nid: str) -> dict | None:
            async with sem:
                try:
                    nb = await client.notebooks.get_or_none(nid)
                    return notebook_to_dict(nb) if nb else None
                except Exception as e:  # noqa: BLE001
                    if is_auth_error(e):
                        raise
                    return None

        recovered = await asyncio.gather(*[_probe_missing(nid) for nid in missing])
        for it in recovered:
            if it and it["id"] not in seen:
                items.append(it)
                seen.add(it["id"])
    return items


async def probe(client) -> int:
    """Cheap read-only call proving the session really works. Raises NotebookLMAuthError if not."""
    try:
        return len(await client.notebooks.list())
    except Exception as e:  # noqa: BLE001
        if is_auth_error(e):
            raise NotebookLMAuthError(f"{type(e).__name__}: {e}"[:300]) from e
        raise


# ---------------------------------------------------------------- export

ARTIFACT_PLAN: dict[str, tuple[str, list[tuple[str, str, dict[str, str]]], bool]] = {
    # kind value : (subfolder, [(download method, extension, kwargs)], is_media)
    "audio": ("audio", [("download_audio", ".m4a", {})], True),
    "video": ("video", [("download_video", ".mp4", {})], True),
    "report": ("reports", [("download_report", ".md", {})], False),
    "slide_deck": ("slides", [("download_slide_deck", ".pdf", {"output_format": "pdf"}),
                              ("download_slide_deck", ".pptx", {"output_format": "pptx"})], False),
    "infographic": ("infographics", [("download_infographic", ".png", {})], False),
    "quiz": ("quizzes", [("download_quiz", ".json", {"output_format": "json"}),
                         ("download_quiz", ".md", {"output_format": "markdown"})], False),
    "flashcards": ("flashcards", [("download_flashcards", ".json", {"output_format": "json"}),
                                  ("download_flashcards", ".md", {"output_format": "markdown"})], False),
    "data_table": ("data-tables", [("download_data_table", ".csv", {})], False),
    "mind_map": ("mind-maps", [("download_mind_map", ".json", {})], False),
}


async def export_notebook(client, nb: dict, dest: Path, *, include_media: bool = True,
                          include_chat: bool = True, progress: ProgressFn | None = None,
                          check_cancel: Callable[[], Awaitable[None]] | None = None) -> dict:
    """Export everything about one notebook into `dest` (wiped first). Returns stats.

    Raises IncompleteExport when existing content could not be saved.
    """
    nb_id = nb["id"]
    if await asyncio.to_thread(dest.exists):
        await asyncio.to_thread(shutil.rmtree, dest)
    await asyncio.to_thread(dest.mkdir, parents=True)
    stats: dict[str, Any] = {"sources": 0, "sources_failed": [], "notes": 0, "mind_maps": 0,
                             "artifacts": {}, "artifacts_skipped": [], "chat_turns": 0, "warnings": []}
    problems: list[str] = []

    async def cancel_point():
        if check_cancel:
            await check_cancel()

    # ---- metadata ---------------------------------------------------------
    await _maybe(progress, "metadata")
    summary = None
    try:
        summary = await client.notebooks.get_summary(nb_id)
    except Exception as e:
        _reraise_if_auth(e)
        stats["warnings"].append(f"summary unavailable: {e}")
    meta = dict(nb)
    meta["summary"] = summary
    meta["exported_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    meta["exporter"] = "nlm2drive (notebooklm-py)"

    # ---- sources ----------------------------------------------------------
    await cancel_point()
    sources = await client.sources.list(nb_id)
    src_dir = dest / "sources"
    src_dir.mkdir()
    src_index = []
    for i, s in enumerate(sources, 1):
        await cancel_point()
        await _maybe(progress, f"source {i}/{len(sources)}: {s.title or s.id}")
        kind = str(_enum_val(getattr(s, "kind", "")) or "")
        entry = {"index": i, "id": s.id, "title": s.title, "type": kind, "url": getattr(s, "url", None),
                 "drive_document_id": getattr(s, "drive_document_id", None),
                 "created_at": _iso(getattr(s, "created_at", None)),
                 "status": str(_enum_val(getattr(s, "status", "")) or ""),
                 "word_count": getattr(s, "word_count", None), "file": None, "error": None}
        try:
            ft = None
            try:
                ft = await client.sources.get_fulltext(nb_id, s.id, output_format="markdown")
            except Exception:
                ft = await client.sources.get_fulltext(nb_id, s.id, output_format="text")
            content = getattr(ft, "content", None) or ""
            path = unique_path(src_dir, safe_filename(f"{i:02d} - {s.title or 'Untitled source'}", ".md"))
            header = ["---", f"title: {json.dumps(s.title or '')}", f"type: {kind}",
                      f"source_id: {s.id}", f"created_at: {entry['created_at'] or ''}"]
            if entry["url"]:
                header.append(f"url: {entry['url']}")
            if entry["drive_document_id"]:
                header.append(f"drive_document_id: {entry['drive_document_id']}")
            header += ["---", "", f"# {s.title or 'Untitled source'}", ""]
            path.write_text("\n".join(header) + content + "\n", encoding="utf-8")
            entry["file"] = path.name
            entry["chars"] = len(content)
            stats["sources"] += 1
        except Exception as e:
            _reraise_if_auth(e)
            entry["error"] = f"{type(e).__name__}: {e}"[:300]
            is_broken = bool(getattr(s, "is_error", False)) or bool(getattr(s, "is_processing", False))
            if is_broken:
                stats["warnings"].append(f"source '{s.title}' is in error/processing state; no text to save")
            else:
                problems.append(f"source '{s.title}': {entry['error']}")
                stats["sources_failed"].append(s.id)
        src_index.append(entry)
    (src_dir / "index.json").write_text(json.dumps(src_index, indent=2, ensure_ascii=False), encoding="utf-8")

    # ---- notes ------------------------------------------------------------
    await cancel_point()
    await _maybe(progress, "notes")
    notes_dir = dest / "notes"
    notes_dir.mkdir()
    note_index = []
    notes = await client.notes.list(nb_id)
    for n in notes:
        path = unique_path(notes_dir, safe_filename(n.title or "Untitled note", ".md"))
        body = f"# {n.title or 'Untitled note'}\n\n_created: {_iso(getattr(n, 'created_at', None)) or ''}_\n\n{n.content or ''}\n"
        path.write_text(body, encoding="utf-8")
        note_index.append({"id": n.id, "title": n.title, "created_at": _iso(getattr(n, "created_at", None)),
                           "file": path.name})
        stats["notes"] += 1
    (notes_dir / "index.json").write_text(json.dumps(note_index, indent=2, ensure_ascii=False), encoding="utf-8")

    # ---- studio artifacts -------------------------------------------------
    studio = dest / "studio"
    studio.mkdir()
    art_index = []
    artifacts = await client.artifacts.list(nb_id)
    mind_map_ids_done: set[str] = set()
    for a in artifacts:
        await cancel_point()
        kind = str(_enum_val(getattr(a, "kind", "unknown")))
        entry = {"id": a.id, "title": a.title, "kind": kind, "status": getattr(a, "status_str", None),
                 "created_at": _iso(getattr(a, "created_at", None)), "files": [], "error": None}
        art_index.append(entry)
        if not getattr(a, "is_completed", False):
            entry["error"] = "not completed — nothing to download"
            continue
        plan = ARTIFACT_PLAN.get(kind)
        if plan is None:
            entry["error"] = f"unsupported artifact kind '{kind}'"
            stats["warnings"].append(f"artifact '{a.title}' ({kind}) not supported by exporter")
            continue
        sub, downloads, is_media = plan
        if is_media and not include_media:
            entry["error"] = "skipped (media disabled)"
            stats["artifacts_skipped"].append(a.id)
            continue
        out_dir = studio / sub
        out_dir.mkdir(exist_ok=True)
        for method, ext, kwargs in downloads:
            await _maybe(progress, f"{kind}: {a.title or a.id} ({ext})")
            path = unique_path(out_dir, safe_filename(a.title or kind, ext))
            try:
                saved = await getattr(client.artifacts, method)(nb_id, str(path), a.id, **kwargs)
                saved_p = Path(saved) if saved else path
                if not saved_p.exists() or saved_p.stat().st_size == 0:
                    raise IncompleteExport("download produced no file")
                entry["files"].append(saved_p.relative_to(dest).as_posix())
            except Exception as e:
                _reraise_if_auth(e)
                msg = f"{type(e).__name__}: {e}"[:300]
                if (
                    (ext == ".md" and kind in ("quiz", "flashcards"))
                    or (ext == ".pptx" and kind == "slide_deck")
                ) and entry["files"]:
                    stats["warnings"].append(f"{kind} '{a.title}' {ext} export failed: {msg}")
                    continue
                entry["error"] = msg
                problems.append(f"{kind} '{a.title}': {msg}")
        if kind == "mind_map" and entry["files"]:
            mind_map_ids_done.add(a.id)
        if entry["files"]:
            stats["artifacts"][kind] = stats["artifacts"].get(kind, 0) + 1

    # ---- mind maps (note-backed ones are not always listed as artifacts) --
    try:
        mms = await client.mind_maps.list(nb_id)
    except Exception as e:
        _reraise_if_auth(e)
        mms = []
        stats["warnings"].append(f"mind map listing failed: {e}")
    for m in mms:
        if m.id in mind_map_ids_done:
            continue
        tree = getattr(m, "tree", None)
        if tree is None:
            try:
                tree = await client.mind_maps.get_tree(nb_id, m.id)
            except Exception as e:
                _reraise_if_auth(e)
                problems.append(f"mind map '{m.title}': {e}")
                continue
        out_dir = studio / "mind-maps"
        out_dir.mkdir(exist_ok=True)
        path = unique_path(out_dir, safe_filename(m.title or "Mind map", ".json"))
        path.write_text(json.dumps({"id": m.id, "title": m.title, "kind": str(_enum_val(getattr(m, "kind", ""))),
                                    "created_at": _iso(getattr(m, "created_at", None)), "tree": tree},
                                   indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        art_index.append({"id": m.id, "title": m.title, "kind": "mind_map", "status": "completed",
                          "created_at": _iso(getattr(m, "created_at", None)),
                          "files": [path.relative_to(dest).as_posix()], "error": None})
        stats["mind_maps"] += 1
    (studio / "index.json").write_text(json.dumps(art_index, indent=2, ensure_ascii=False), encoding="utf-8")

    # ---- chat -------------------------------------------------------------
    if include_chat:
        await cancel_point()
        await _maybe(progress, "chat history")
        try:
            history = await client.chat.get_history(nb_id, limit=1000)
            if history:
                chat_dir = dest / "chat"
                chat_dir.mkdir()
                turns = [{"question": q, "answer": a} for q, a in history]
                (chat_dir / "history.json").write_text(json.dumps(turns, indent=2, ensure_ascii=False), encoding="utf-8")
                md = ["# Chat history", ""]
                for i, t in enumerate(turns, 1):
                    md += [f"## {i}. {t['question']}", "", t["answer"] or "", ""]
                (chat_dir / "history.md").write_text("\n".join(md), encoding="utf-8")
                stats["chat_turns"] = len(turns)
        except Exception as e:
            _reraise_if_auth(e)
            stats["warnings"].append(f"chat history unavailable: {e}")

    # ---- README + metadata + manifest ------------------------------------
    meta["stats"] = stats
    (dest / "metadata.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    (dest / "README.md").write_text(render_readme(meta, src_index, note_index, art_index), encoding="utf-8")
    manifest = build_manifest(dest, {k: meta.get(k) for k in ("id", "title", "created_at", "sources_count")})
    stats["files"] = manifest["file_count"] + 1
    stats["bytes"] = manifest["total_bytes"]
    stats["complete"] = not problems and not stats["artifacts_skipped"]
    stats["problems"] = problems
    if problems:
        raise IncompleteExport("; ".join(problems[:5]))
    return stats


def _md_link(rel: str) -> str:
    return rel.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def render_readme(meta: dict, sources: list[dict], notes: list[dict], artifacts: list[dict]) -> str:
    created = (meta.get("created_at") or "")[:10]
    lines = [f"# {meta.get('emoji') or '📓'} {meta.get('title')}", "",
             f"- **Created:** {created}",
             f"- **Notebook ID:** `{meta.get('id')}`",
             f"- **Archived:** {meta.get('exported_at', '')[:16].replace('T', ' ')} UTC by nlm2drive",
             f"- **Sources:** {len(sources)} · **Notes:** {len(notes)} · **Studio items:** {len(artifacts)}", ""]
    if meta.get("summary"):
        lines += ["## Summary", "", str(meta["summary"]).strip(), ""]
    lines += ["## Sources", "", "| # | Source | Type | Origin |", "|---:|---|---|---|"]
    for s in sources:
        title = (s.get("title") or "Untitled").replace("|", "/")
        name = f"[{title}](sources/{_md_link(s['file'])})" if s.get("file") else f"{title} _(no text: {s.get('error') or 'n/a'})_"
        origin = s.get("url") or (f"Drive file `{s['drive_document_id']}`" if s.get("drive_document_id") else "uploaded/pasted")
        lines.append(f"| {s['index']} | {name} | {s.get('type') or ''} | {origin} |")
    lines.append("")
    if notes:
        lines += ["## Notes", ""]
        lines += [f"- [{(n.get('title') or 'Untitled').replace('|', '/')}](notes/{_md_link(n['file'])})" for n in notes]
        lines.append("")
    if artifacts:
        lines += ["## Studio", ""]
        for a in artifacts:
            files = ", ".join(f"[{Path(f).suffix.lstrip('.')}]({_md_link(f)})" for f in a.get("files") or [])
            note = f" — _{a['error']}_" if a.get("error") else ""
            lines.append(f"- **{a.get('kind')}** · {a.get('title') or 'Untitled'} {files}{note}")
        lines.append("")
    if (meta.get("stats") or {}).get("chat_turns"):
        lines += ["## Chat", "", "- [Chat history](chat/history.md)", ""]
    lines += ["---", "_Original uploaded files (e.g. PDFs) are not retrievable from NotebookLM; "
              "their extracted full text is saved under `sources/`._", ""]
    return "\n".join(lines)


async def delete_notebook(client, nb_id: str) -> None:
    if hasattr(client.notebooks, "remove_from_recent"):
        try:
            await client.notebooks.remove_from_recent(nb_id)
        except Exception:  # noqa: BLE001
            pass
    await client.notebooks.delete(nb_id)
    # Confirm it is gone.
    still = await client.notebooks.get_or_none(nb_id)
    if still is not None:
        raise RuntimeError("notebook still present after delete")


async def get_notebook(client, nb_id: str) -> dict | None:
    nb = await client.notebooks.get_or_none(nb_id)
    return notebook_to_dict(nb) if nb is not None else None


async def create_test_notebook(client) -> dict:
    """Throwaway notebook for end-to-end testing (text source + note)."""
    stamp = datetime.now().strftime("%Y-%m-%d %H%M")
    nb = await client.notebooks.create(f"nlm2drive test {stamp}")
    await client.sources.add_text(nb.id, "Test source A", "nlm2drive end-to-end test source A.\n" * 20, wait=True)
    await client.sources.add_text(nb.id, "Test source B", "Second source used to validate exports.\n" * 20, wait=True)
    await client.notes.create(nb.id, "Test note", "This note should appear in the archive.")
    fresh = await client.notebooks.get(nb.id)
    return notebook_to_dict(fresh)


__all__ = ["open_client", "account_status", "account_status_with_items", "list_notebooks", "export_notebook",
           "delete_notebook", "get_notebook", "create_test_notebook", "has_session", "IncompleteExport",
           "NotebookLMAuthError", "is_auth_error", "probe", "storage_mtime",
           "human_bytes", "safe_name", "_parse_iso"]
