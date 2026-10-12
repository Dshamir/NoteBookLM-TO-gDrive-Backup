"""Shared fixtures: isolated DATA_DIR, fake NotebookLM client and in-memory Google Drive."""
from __future__ import annotations

import hashlib
import itertools
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

# Isolate all app state before importing the app.
_TMP = tempfile.mkdtemp(prefix="nlm2drive-test-")
os.environ["DATA_DIR"] = _TMP
os.environ["NOTEBOOKLM_HOME"] = str(Path(_TMP) / "notebooklm")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from app.gdrive import DriveClient  # noqa: E402


# --------------------------------------------------------------------------- fake NotebookLM

def make_nb(nb_id: str, title: str, year: int, owner: bool = True, sources: int = 2):
    return SimpleNamespace(id=nb_id, title=title, emoji="📘", created_at=datetime(year, 3, 4, tzinfo=timezone.utc),
                           modified_at=None, last_viewed_at=None, sources_count=sources, is_owner=owner, role="owner")


AUTH_ERR = "RPCError: The server rejected this request (unauthenticated)."


class FakeNLM:
    def __init__(self, notebooks, fail_fulltext: set[str] | None = None):
        self.data = {nb.id: nb for nb in notebooks}
        self.deleted: list[str] = []
        self.fail_fulltext = fail_fulltext or set()
        self.auth_fail_fulltext: set[str] = set()   # source ids whose text download hits an expired session
        self.session_ok = True                      # False -> every call fails like a revoked Google session
        self.expire_on_export: str | None = None    # session dies while this notebook is being exported
        outer = self

        async def nb_list():
            return list(outer.data.values())

        async def get_summary(nb_id):
            if outer.expire_on_export == nb_id:
                outer.expire_on_export, outer.session_ok = None, False
                raise RuntimeError(AUTH_ERR)
            return f"Summary of {nb_id}"

        async def delete(nb_id):
            outer.deleted.append(nb_id)
            outer.data.pop(nb_id, None)

        async def get_or_none(nb_id):
            return outer.data.get(nb_id)

        async def src_list(nb_id):
            n = outer.data[nb_id].sources_count
            return [SimpleNamespace(id=f"{nb_id}-s{i}", title=f"Source {i}", kind="pasted_text", url=None,
                                    drive_document_id=None, created_at=None, status="ready", word_count=10,
                                    is_error=False, is_processing=False) for i in range(n)]

        async def get_fulltext(nb_id, sid, output_format="markdown"):
            if sid in outer.auth_fail_fulltext:
                raise RuntimeError(AUTH_ERR)
            if sid in outer.fail_fulltext:
                raise RuntimeError("boom")
            return SimpleNamespace(content=f"Full text of {sid}\n")

        async def notes_list(nb_id):
            return [SimpleNamespace(id="n1", title="My note", content="hello", created_at=None)]

        async def empty(*_a, **_k):
            return []

        self.notebooks = SimpleNamespace(list=nb_list, get_summary=get_summary, delete=delete, get_or_none=get_or_none)
        self.sources = SimpleNamespace(list=src_list, get_fulltext=get_fulltext)
        self.notes = SimpleNamespace(list=notes_list)
        self.artifacts = SimpleNamespace(list=empty)
        self.mind_maps = SimpleNamespace(list=empty)
        self.chat = SimpleNamespace(get_history=empty)

        def guard(fn):
            async def inner(*a, **k):
                if not outer.session_ok:
                    raise RuntimeError(AUTH_ERR)
                return await fn(*a, **k)
            return inner

        for ns in (self.notebooks, self.sources, self.notes, self.artifacts, self.mind_maps, self.chat):
            for name, fn in list(vars(ns).items()):
                setattr(ns, name, guard(fn))

    def factory(self):
        client = self

        class CM:
            async def __aenter__(self):
                return client

            async def __aexit__(self, *a):
                return False

        return CM


# --------------------------------------------------------------------------- fake Drive

class FakeDrive:
    """In-memory Drive with the subset of DriveClient used by the runner."""
    verify_folder = DriveClient.verify_folder

    def __init__(self):
        self._ids = itertools.count(1)
        self.nodes: dict[str, dict] = {"root": {"name": "My Drive", "parent": None, "folder": True, "trashed": False}}
        self.corrupt_next_upload = False
        self.drop_after_upload: str | None = None

    def connected(self):
        return True

    def _children(self, parent):
        return [(i, n) for i, n in self.nodes.items() if n["parent"] == parent and not n["trashed"]]

    def find_child(self, parent, name, folder=None):
        for i, n in self._children(parent):
            if n["name"] == name and (folder is None or n["folder"] == folder):
                return {"id": i, **n}
        return None

    def ensure_folder(self, parent, name):
        f = self.find_child(parent, name, folder=True)
        if f:
            return f["id"]
        i = f"f{next(self._ids)}"
        self.nodes[i] = {"name": name, "parent": parent, "folder": True, "trashed": False}
        return i

    def ensure_path(self, parts, parent_id="root"):
        cur = parent_id
        for p in parts:
            cur = self.ensure_folder(cur, p)
        return cur

    def trash_child_folder(self, parent, name):
        f = self.find_child(parent, name, folder=True)
        if not f:
            return False
        self.nodes[f["id"]]["trashed"] = True
        return True

    def upload_file(self, local: Path, parent, name=None, progress=None):
        data = local.read_bytes()
        md5 = hashlib.md5(data).hexdigest()
        if self.corrupt_next_upload:
            md5, self.corrupt_next_upload = "0" * 32, False
        i = f"x{next(self._ids)}"
        self.nodes[i] = {"name": name or local.name, "parent": parent, "folder": False, "trashed": False,
                         "md5": md5, "size": len(data)}
        if self.drop_after_upload and (name or local.name) == self.drop_after_upload:
            self.nodes[i]["trashed"] = True  # simulates a file vanishing before verification
        if progress:
            progress(len(data))
        return {"id": i, "md5Checksum": md5, "size": str(len(data))}

    def put_text(self, parent, name, text, mime="text/markdown"):
        existing = self.find_child(parent, name, folder=False)
        data = text.encode()
        if existing:
            self.nodes[existing["id"]].update(md5=hashlib.md5(data).hexdigest(), size=len(data))
            return existing["id"]
        i = f"t{next(self._ids)}"
        self.nodes[i] = {"name": name, "parent": parent, "folder": False, "trashed": False,
                         "md5": hashlib.md5(data).hexdigest(), "size": len(data)}
        return i

    def list_tree(self, folder_id, prefix=""):
        out = {}
        for i, n in self._children(folder_id):
            rel = f"{prefix}{n['name']}"
            if n["folder"]:
                out.update(self.list_tree(i, rel + "/"))
            else:
                out[rel] = {"id": i, "md5": n["md5"], "size": n["size"]}
        return out

    def path_of(self, node_id):
        parts = []
        while node_id and node_id != "root":
            parts.append(self.nodes[node_id]["name"])
            node_id = self.nodes[node_id]["parent"]
        return "/".join(reversed(parts))


@pytest.fixture
def tmp_data(tmp_path):
    return tmp_path


@pytest.fixture
def fake_drive():
    return FakeDrive()
