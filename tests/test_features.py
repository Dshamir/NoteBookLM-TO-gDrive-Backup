"""Tests for extended features: presets, catalog export, in-modal content, and restore."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import main


def test_select_preset_api(tmp_path):
    client = TestClient(main.app)
    main.db.upsert_listing([
        {"id": "nb_p1", "title": "Empty draft", "created_at": "2026-01-01T00:00:00+00:00", "sources_count": 0, "year": 2026, "is_owner": True},
        {"id": "nb_p2", "title": "With sources", "created_at": "2026-02-01T00:00:00+00:00", "sources_count": 4, "year": 2026, "is_owner": True},
    ], [])

    res = client.post("/api/notebooks/select-preset", json={"preset": "empty", "only": True})
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["count"] == 1


def test_export_catalog_csv_and_json():
    client = TestClient(main.app)
    main.db.upsert_listing([
        {"id": "nb_exp1", "title": "Catalog item", "created_at": "2026-03-01T00:00:00+00:00", "sources_count": 2, "year": 2026, "is_owner": True},
    ], [])

    # Test CSV export
    res_csv = client.get("/api/export/csv")
    assert res_csv.status_code == 200
    assert "text/csv" in res_csv.headers["content-type"]
    assert "Catalog item" in res_csv.text
    assert "nb_exp1" in res_csv.text

    # Test JSON export
    res_json = client.get("/api/export/json")
    assert res_json.status_code == 200
    assert "application/json" in res_json.headers["content-type"]
    data = res_json.json()
    assert "notebooks" in data
    assert any(n["id"] == "nb_exp1" for n in data["notebooks"])


def test_notebook_content_endpoint():
    client = TestClient(main.app)
    main.db.upsert_listing([
        {"id": "nb_cnt1", "title": "Detailed notebook", "created_at": "2026-03-01T00:00:00+00:00", "sources_count": 1, "year": 2026, "is_owner": True},
    ], [])

    res = client.get("/api/notebooks/nb_cnt1/content")
    assert res.status_code == 200
    data = res.json()
    assert data["id"] == "nb_cnt1"
    assert data["title"] == "Detailed notebook"
    assert "sources" in data
    assert "notes" in data


def test_restore_already_present():
    client = TestClient(main.app)
    main.db.upsert_listing([
        {"id": "nb_pres1", "title": "Still here", "created_at": "2026-03-01T00:00:00+00:00", "sources_count": 1, "year": 2026, "is_owner": True},
    ], [])

    res = client.post("/api/notebooks/nb_pres1/restore")
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["already_present"] is True
    assert "notebooklm.google.com" in data["url"]


def test_select_preset_invalid():
    client = TestClient(main.app)
    res = client.post("/api/notebooks/select-preset", json={"preset": "drop_tables", "only": True})
    assert res.status_code == 400
    assert "Unknown preset" in res.json()["error"]


def test_restore_refuses_shared_notebook():
    client = TestClient(main.app)
    main.db.upsert_listing([
        {"id": "nb_shared1", "title": "Shared Doc", "created_at": "2026-03-01T00:00:00+00:00", "sources_count": 1, "year": 2026, "is_owner": False},
    ], [])

    res = client.post("/api/notebooks/nb_shared1/restore")
    assert res.status_code == 403
    assert "Invariant 2" in res.json()["error"]


def test_staging_path_traversal_refused():
    client = TestClient(main.app)
    res_content = client.get("/api/notebooks/evil..notebook/content")
    assert res_content.status_code == 400
    assert res_content.json()["error"] == "Invalid notebook ID"

    res_audio = client.get("/api/notebooks/evil..notebook/audio")
    assert res_audio.status_code == 400
    assert res_audio.json()["error"] == "Invalid notebook ID"


def test_audit_endpoint(monkeypatch):
    client = TestClient(main.app)

    # When drive is disconnected -> 400
    monkeypatch.setattr(main.drive, "connected", lambda: False)
    res = client.post("/api/audit")
    assert res.status_code == 400

    # When drive is connected -> audits intact vs missing
    monkeypatch.setattr(main.drive, "connected", lambda: True)
    monkeypatch.setattr(main.drive, "folder_exists", lambda fid: fid == "f_intact")

    main.db.upsert_listing([
        {"id": "nb_aud1", "title": "Audited 1", "created_at": "2026-03-01T00:00:00+00:00", "sources_count": 1, "year": 2026, "is_owner": True},
        {"id": "nb_aud2", "title": "Audited 2", "created_at": "2026-03-02T00:00:00+00:00", "sources_count": 1, "year": 2026, "is_owner": True},
        {"id": "nb_aud3", "title": "Audited 3", "created_at": "2026-03-03T00:00:00+00:00", "sources_count": 1, "year": 2026, "is_owner": True},
    ], [])
    main.db.set_stage("nb_aud1", "CLOUD_VERIFIED", drive_folder_id="f_intact")
    main.db.set_stage("nb_aud2", "DELETED", drive_folder_id="f_missing")
    main.db.set_stage("nb_aud3", "DELETED", drive_folder_id=None)

    res2 = client.post("/api/audit")
    assert res2.status_code == 200
    data = res2.json()
    assert data["total"] == 3
    assert data["verified"] == 1
    assert data["missing"] == 2
    assert len(data["issues"]) == 2

