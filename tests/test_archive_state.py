"""Naming, manifest and state DB tests."""
from __future__ import annotations

import json
from datetime import datetime

from app.archive import build_manifest, notebook_folder_name, render_catalog, safe_filename, safe_name
from app.state import StateDB


def test_safe_name_windows_and_drive():
    assert safe_name('a<b>c:d"e/f\\g|h?i*j') == "a b c d e f g h i j"
    assert safe_name("CON") == "_CON"
    assert safe_name("  trailing dots... ") == "trailing dots"
    assert safe_name("") == "untitled"
    assert len(safe_name("x" * 500)) <= 90


def test_safe_filename_keeps_extension_within_limit():
    n = safe_filename("y" * 300, ".json")
    assert n.endswith(".json") and len(n) <= 90


def test_notebook_folder_name():
    name = notebook_folder_name(datetime(2024, 5, 6), "My: Notebook?", "abcdef1234567")
    assert name == "2024-05-06 My Notebook [abcdef12]"
    assert notebook_folder_name(None, None, "zzzzzzzzzz").startswith("undated untitled")


def test_manifest(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "sub" / "b.md").write_text("world")
    m = build_manifest(tmp_path, {"id": "x"})
    assert m["file_count"] == 2 and m["total_bytes"] == 10
    assert m["files"]["a.txt"]["md5"] == "5d41402abc4b2a76b9719d911017c592"
    assert json.loads((tmp_path / "manifest.json").read_text())["files"]["sub/b.md"]["size"] == 5


def test_catalog_only_lists_archived():
    rows = [{"id": "1", "title": "A", "folder": "2024/A [1]", "stage": "DELETED", "created_at": "2024-01-01", "bytes": 10},
            {"id": "2", "title": "B", "folder": None, "stage": "NEW"}]
    csv_text, md = render_catalog(rows)
    assert "A" in md and "| B" not in md and csv_text.count("\n") == 2


def test_state_selection_rules(tmp_path):
    db = StateDB(tmp_path / "s.sqlite")
    items = [{"id": "own24", "title": "a", "year": 2024, "is_owner": True},
             {"id": "own23", "title": "b", "year": 2023, "is_owner": True},
             {"id": "shr25", "title": "c", "year": 2025, "is_owner": False}]
    db.upsert_listing(items, [2024, 2025])
    assert {r["id"] for r in db.list_notebooks() if r["selected"]} == {"own24"}
    db.set_selected(["shr25", "own23"], True)
    assert {r["id"] for r in db.list_notebooks() if r["selected"]} == {"own24", "own23"}
    db.select_years([2025], only=True)
    assert [r["id"] for r in db.list_notebooks() if r["selected"]] == []
    # a notebook that vanished from the account is marked not present
    db.upsert_listing(items[:1], [2024, 2025])
    assert db.get("own23")["present"] == 0 and db.get("own24")["present"] == 1


def test_select_cutoff(tmp_path):
    db = StateDB(tmp_path / "s.sqlite")
    items = [
        {"id": "nb1", "title": "Jan 2026", "created_at": "2026-01-15T10:00:00+00:00", "year": 2026, "is_owner": True},
        {"id": "nb2", "title": "Mar 2026", "created_at": "2026-03-20T10:00:00+00:00", "year": 2026, "is_owner": True},
        {"id": "nb3", "title": "Apr 2026 (shared)", "created_at": "2026-01-10T10:00:00+00:00", "year": 2026, "is_owner": False},
        {"id": "nb4", "title": "Jun 2026", "created_at": "2026-06-01T10:00:00+00:00", "year": 2026, "is_owner": True},
    ]
    db.upsert_listing(items, [])
    # Initially none selected
    assert not any(r["selected"] for r in db.list_notebooks())
    # Cutoff at 2026-03-01 should select only nb1 (nb3 is shared so never selected)
    db.select_cutoff("2026-03-01", only=True)
    sel = {r["id"] for r in db.list_notebooks() if r["selected"]}
    assert sel == {"nb1"}
    # Cutoff at 2026-04-01 should select nb1 and nb2
    db.select_cutoff("2026-04-01", only=True)
    sel = {r["id"] for r in db.list_notebooks() if r["selected"]}
    assert sel == {"nb1", "nb2"}
