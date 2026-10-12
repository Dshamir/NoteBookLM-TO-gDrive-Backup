"""Archive filesystem helpers: Windows/Drive-safe naming, hashing, manifest, copy and local verification."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import unicodedata
from datetime import datetime
from pathlib import Path

INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
MANIFEST_NAME = "manifest.json"

FOLDER_TITLE_MAX = 60
FILE_NAME_MAX = 90


def safe_name(name: str | None, max_len: int = FILE_NAME_MAX, fallback: str = "untitled") -> str:
    """Make a single path component valid on Windows + Google Drive and bounded in length."""
    s = unicodedata.normalize("NFC", (name or "").strip())
    s = INVALID_CHARS.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = s.rstrip(". ")
    if not s:
        s = fallback
    if s.split(".")[0].upper() in RESERVED:
        s = f"_{s}"
    if len(s) > max_len:
        s = s[:max_len].rstrip(". ")
    return s or fallback


def safe_filename(stem: str | None, ext: str, max_len: int = FILE_NAME_MAX) -> str:
    ext = ext if ext.startswith(".") or not ext else f".{ext}"
    return safe_name(stem, max_len=max_len - len(ext)) + ext


def unique_path(directory: Path, filename: str) -> Path:
    """Return directory/filename, adding ' (2)', ' (3)'… when it already exists."""
    p = directory / filename
    if not p.exists():
        return p
    stem, ext = os.path.splitext(filename)
    i = 2
    while True:
        cand = directory / f"{stem} ({i}){ext}"
        if not cand.exists():
            return cand
        i += 1


def notebook_folder_name(created_at: datetime | None, title: str | None, nb_id: str) -> str:
    date = created_at.strftime("%Y-%m-%d") if created_at else "undated"
    return f"{date} {safe_name(title, FOLDER_TITLE_MAX)} [{nb_id[:8]}]"


def year_dir(created_at: datetime | None) -> str:
    return str(created_at.year) if created_at else "undated"


# -- hashing / manifest -----------------------------------------------------

def hash_file(path: Path, chunk: int = 1024 * 1024) -> tuple[str, str, int]:
    # md5 is Google Drive's own file checksum (what we compare against) - integrity, not security.
    sha, md5, size = hashlib.sha256(), hashlib.md5(usedforsecurity=False), 0
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            sha.update(b)
            md5.update(b)
            size += len(b)
    return sha.hexdigest(), md5.hexdigest(), size


def build_manifest(root: Path, notebook: dict) -> dict:
    files: dict[str, dict] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.name != MANIFEST_NAME:
            rel = p.relative_to(root).as_posix()
            sha, md5, size = hash_file(p)
            files[rel] = {"sha256": sha, "md5": md5, "size": size}
    manifest = {
        "notebook": notebook,
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "file_count": len(files),
        "total_bytes": sum(f["size"] for f in files.values()),
        "files": files,
    }
    (root / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest


def manifest_with_self(root: Path, manifest: dict) -> dict:
    """Files to verify = manifest entries + manifest.json itself."""
    files = dict(manifest["files"])
    sha, md5, size = hash_file(root / MANIFEST_NAME)
    files[MANIFEST_NAME] = {"sha256": sha, "md5": md5, "size": size}
    return files


def copy_tree(src: Path, dest: Path, on_file=None) -> None:
    """Copy staging -> archive. The destination folder is ours (id-suffixed), so a stale partial copy is replaced."""
    if dest.exists():
        shutil.rmtree(dest)
    for p in sorted(src.rglob("*")):
        target = dest / p.relative_to(src)
        if p.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, target)
        if on_file:
            on_file(target, p.stat().st_size)


def verify_local(dest: Path, expected: dict) -> list[str]:
    """Re-read every archived file and compare sha256 + size. Returns a list of problems (empty = OK)."""
    problems = []
    for rel, meta in expected.items():
        p = dest / rel
        if not p.is_file():
            problems.append(f"missing: {rel}")
            continue
        sha, _md5, size = hash_file(p)
        if size != meta["size"] or sha != meta["sha256"]:
            problems.append(f"mismatch: {rel}")
    return problems


# -- catalog ----------------------------------------------------------------

def render_catalog(rows: list[dict]) -> tuple[str, str]:
    """Build (_catalog.csv, _catalog.md) text from DB rows of archived notebooks."""
    rows = [r for r in rows if r.get("folder") and r.get("stage") in ("CLOUD_VERIFIED", "DELETING", "DELETED")]
    rows.sort(key=lambda r: (r.get("created_at") or ""), reverse=True)
    cols = ["created_at", "title", "sources_count", "files", "bytes", "stage", "deleted_at", "folder", "id"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)

    lines = ["# NotebookLM Archive", "",
             f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} — {len(rows)} notebooks_", "",
             "| Created | Notebook | Sources | Files | Size | Status |",
             "|---|---|---:|---:|---:|---|"]
    for r in rows:
        link = r["folder"].replace(" ", "%20")
        title = (r.get("title") or "Untitled").replace("|", "/")
        status = ("deleted from NotebookLM " + (r.get("deleted_at") or "")[:10]) if r.get("stage") == "DELETED" \
            else "archived (still in NotebookLM)"
        lines.append(f"| {(r.get('created_at') or '')[:10]} | [{title}]({link}/README.md) "
                     f"| {r.get('sources_count') or 0} | {r.get('files') or 0} | {human_bytes(r.get('bytes') or 0)} | {status} |")
    return buf.getvalue(), "\n".join(lines) + "\n"


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
