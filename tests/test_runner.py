"""Pipeline tests with a fake NotebookLM client and in-memory Drive."""
from __future__ import annotations

import asyncio

import pytest

from app import nlm_service as nlm
from app.config import SettingsStore
from app.nlm_service import NotebookLMAuthError, notebook_to_dict
from app.runner import EventBus, Runner, may_delete
from app.state import CLOUD_VERIFIED, DELETED, DELETING, FAILED, NEW, StateDB

from .conftest import AUTH_ERR, FakeDrive, FakeNLM, make_nb


def notebooks():
    return [make_nb("nb2024aaaa", "Alpha: research/notes?", 2024),
            make_nb("nb2025bbbb", "Beta", 2025, sources=3),
            make_nb("nb2023cccc", "Old one", 2023),
            make_nb("nb2025shrd", "Shared with me", 2025, owner=False)]


async def setup(tmp_path, client: FakeNLM, drive: FakeDrive, **settings):
    db = StateDB(tmp_path / "state.sqlite")
    st = SettingsStore(tmp_path / "config.json")
    st.update(inter_notebook_delay_sec=0, **settings)
    bus = EventBus()
    bus.bind(asyncio.get_running_loop())

    async def nosleep(_s):
        return None

    runner = Runner(db, st, drive, bus, client_factory=client.factory(), staging=tmp_path / "staging", sleep=nosleep)
    db.upsert_listing([notebook_to_dict(nb) for nb in client.data.values()], st.get().years)
    return runner, db, st


async def run(runner: Runner, **opts):
    await runner.start(opts)
    await runner._task
    return runner.snapshot()


# --------------------------------------------------------------------------------------------

async def test_listing_selects_only_owned_in_scope(tmp_path):
    _, db, _ = await setup(tmp_path, FakeNLM(notebooks()), FakeDrive())
    sel = {r["id"] for r in db.list_notebooks() if r["selected"]}
    assert sel == {"nb2024aaaa", "nb2025bbbb"}


async def test_archive_only_uploads_browsable_folders_and_keeps_notebooks(tmp_path):
    client, drive = FakeNLM(notebooks()), FakeDrive()
    runner, db, _ = await setup(tmp_path, client, drive)
    st = await run(runner, delete=False)
    assert st["archived"] == 2 and st["deleted"] == 0 and st["failed"] == 0
    assert client.deleted == []
    row = db.get("nb2024aaaa")
    assert row["stage"] == CLOUD_VERIFIED and row["complete"] == 1
    path = drive.path_of(row["drive_folder_id"])
    assert path == "NotebookLM/Archive/2024/2024-03-04 Alpha research notes [nb2024aa]"
    files = drive.list_tree(row["drive_folder_id"])
    for expected in ("README.md", "metadata.json", "manifest.json", "sources/01 - Source 0.md",
                     "sources/index.json", "notes/My note.md", "studio/index.json"):
        assert expected in files, expected
    # untouched notebooks
    assert db.get("nb2023cccc")["stage"] == NEW
    assert db.get("nb2025shrd")["stage"] == NEW
    # catalog written at archive root
    root = drive.ensure_path(["NotebookLM", "Archive"])
    assert drive.find_child(root, "_catalog.md", folder=False)
    # staging cleaned
    assert not (tmp_path / "staging" / "nb2024aaaa").exists()


async def test_delete_requires_typed_confirmation(tmp_path):
    client, drive = FakeNLM(notebooks()), FakeDrive()
    runner, db, _ = await setup(tmp_path, client, drive, delete_confirmed=False)
    st = await run(runner, delete=True)
    assert st["archived"] == 2 and client.deleted == []


async def test_delete_after_cloud_verification(tmp_path):
    client, drive = FakeNLM(notebooks()), FakeDrive()
    runner, db, _ = await setup(tmp_path, client, drive, delete_confirmed=True)
    st = await run(runner, delete=True)
    assert sorted(client.deleted) == ["nb2024aaaa", "nb2025bbbb"]
    assert st["deleted"] == 2
    row = db.get("nb2025bbbb")
    assert row["stage"] == DELETED and row["deleted_at"]
    assert "sources/03 - Source 2.md" in drive.list_tree(row["drive_folder_id"])
    # never touches shared or out-of-scope notebooks
    assert "nb2025shrd" in client.data and "nb2023cccc" in client.data


async def test_incomplete_export_is_archived_but_never_deleted(tmp_path):
    client = FakeNLM(notebooks(), fail_fulltext={"nb2025bbbb-s1"})
    drive = FakeDrive()
    runner, db, _ = await setup(tmp_path, client, drive, delete_confirmed=True)
    st = await run(runner, delete=True)
    row = db.get("nb2025bbbb")
    assert row["stage"] == CLOUD_VERIFIED and row["complete"] == 0
    assert "nb2025bbbb" not in client.deleted and "nb2024aaaa" in client.deleted
    assert st["kept"] == 1


async def test_transient_md5_mismatch_is_retried(tmp_path):
    client, drive = FakeNLM(notebooks()), FakeDrive()
    drive.corrupt_next_upload = True
    runner, db, _ = await setup(tmp_path, client, drive, delete_confirmed=True)
    await run(runner, delete=True, ids=["nb2024aaaa"])
    assert db.get("nb2024aaaa")["stage"] == DELETED


async def test_failed_cloud_verification_never_deletes(tmp_path):
    client, drive = FakeNLM(notebooks()), FakeDrive()
    drive.drop_after_upload = "README.md"
    runner, db, _ = await setup(tmp_path, client, drive, delete_confirmed=True)
    st = await run(runner, delete=True)
    assert client.deleted == []
    assert st["failed"] == 2
    assert db.get("nb2024aaaa")["stage"] == FAILED


async def test_dry_run_changes_nothing(tmp_path):
    client, drive = FakeNLM(notebooks()), FakeDrive()
    runner, db, _ = await setup(tmp_path, client, drive, delete_confirmed=True)
    st = await run(runner, dry_run=True, delete=True)
    assert st["done"] == 2 and client.deleted == []
    assert all(r["stage"] == NEW for r in db.list_notebooks())
    assert list(drive.nodes) == ["root"]


async def test_limit(tmp_path):
    client, drive = FakeNLM(notebooks()), FakeDrive()
    runner, db, _ = await setup(tmp_path, client, drive)
    st = await run(runner, delete=False, limit=1)
    assert st["total"] == 1 and st["archived"] == 1


async def test_resume_interrupted_delete(tmp_path):
    client, drive = FakeNLM(notebooks()), FakeDrive()
    runner, db, _ = await setup(tmp_path, client, drive, delete_confirmed=True)
    await run(runner, delete=False, ids=["nb2024aaaa"])
    db.set_stage("nb2024aaaa", DELETING)
    client.data.pop("nb2024aaaa")  # delete went through but the app crashed before recording it
    await run(runner, delete=True, ids=["nb2024aaaa"])
    assert db.get("nb2024aaaa")["stage"] == DELETED


async def test_previously_verified_copy_is_reverified_before_delete(tmp_path):
    client, drive = FakeNLM(notebooks()), FakeDrive()
    runner, db, st = await setup(tmp_path, client, drive)
    await run(runner, delete=False, ids=["nb2024aaaa"])
    folder = db.get("nb2024aaaa")["drive_folder_id"]
    readme = drive.list_tree(folder)["README.md"]["id"]
    drive.nodes[readme]["trashed"] = True  # someone removed a file from the archive meanwhile
    st.update(delete_confirmed=True)
    await run(runner, delete=True, ids=["nb2024aaaa"])
    row = db.get("nb2024aaaa")
    assert row["stage"] == DELETED
    assert "README.md" in drive.list_tree(row["drive_folder_id"])  # re-archived before deleting


async def test_interrupted_stages_reset_on_startup(tmp_path):
    client, drive = FakeNLM(notebooks()), FakeDrive()
    runner, db, _ = await setup(tmp_path, client, drive)
    db.set_stage("nb2024aaaa", "UPLOADING")
    runner.recover_interrupted()
    assert db.get("nb2024aaaa")["stage"] == NEW


@pytest.mark.parametrize("row,opts,confirmed,expected", [
    ({"id": "a", "is_owner": 1, "stage": CLOUD_VERIFIED, "complete": 1, "drive_folder_id": "f"}, {"delete": True}, True, True),
    ({"id": "a", "is_owner": 1, "stage": CLOUD_VERIFIED, "complete": 1, "drive_folder_id": "f"}, {"delete": True}, False, False),
    ({"id": "a", "is_owner": 1, "stage": CLOUD_VERIFIED, "complete": 0, "drive_folder_id": "f"}, {"delete": True}, True, False),
    ({"id": "a", "is_owner": 1, "stage": "UPLOADED", "complete": 1, "drive_folder_id": "f"}, {"delete": True}, True, False),
    ({"id": "a", "is_owner": 0, "stage": CLOUD_VERIFIED, "complete": 1, "drive_folder_id": "f"}, {"delete": True}, True, False),
    ({"id": "a", "is_owner": 1, "stage": CLOUD_VERIFIED, "complete": 1, "drive_folder_id": "f"}, {"delete": False}, True, False),
    ({"id": "a", "is_owner": 1, "stage": CLOUD_VERIFIED, "complete": 1, "drive_folder_id": "f"}, {"delete": True, "dry_run": True}, True, False),
    ({"id": "a", "is_owner": 1, "stage": CLOUD_VERIFIED, "complete": 1, "drive_folder_id": "f"}, {"delete": False, "force_delete_ids": ["a"]}, False, True),
    ({"id": "a", "is_owner": 0, "stage": CLOUD_VERIFIED, "complete": 1, "drive_folder_id": "f"}, {"force_delete_ids": ["a"]}, True, False),
])
def test_may_delete_policy(row, opts, confirmed, expected):
    from app.config import Settings
    ok, _ = may_delete(row, opts, Settings(delete_confirmed=confirmed))
    assert ok is expected


# ---------------------------------------------------------------------------- expired Google session
# Regression for the "stuck at metadata" incident: the NotebookLM session was revoked mid-run.

async def wait_until(pred, timeout=5.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached in time")


async def test_session_expiry_pauses_without_failures_and_resumes_same_notebook(tmp_path):
    client, drive = FakeNLM(notebooks()), FakeDrive()
    client.expire_on_export = "nb2025bbbb"          # dies while exporting the 2nd notebook
    runner, db, _ = await setup(tmp_path, client, drive, delete_confirmed=True)
    runner.auth_poll_sec, runner.auth_recheck_sec = 0.02, 0.0
    await runner.start({"delete": True})
    await wait_until(lambda: runner.snapshot().get("needs_auth"))

    st = runner.snapshot()
    assert st["state"] == "paused" and st["current"] is None      # no stale 'in progress' notebook
    assert st["failed"] == 0                                      # not a notebook failure
    assert db.get("nb2025bbbb")["stage"] == NEW                   # rolled back, not FAILED
    assert db.get("nb2025bbbb")["attempts"] in (0, None)
    assert client.deleted == ["nb2024aaaa"]                       # the finished one stays finished

    # Pressing Resume while still signed out must NOT un-pause the run.
    runner.resume()
    await asyncio.sleep(0.15)
    assert runner.snapshot()["needs_auth"] and runner.snapshot()["state"] == "paused"

    client.session_ok = True                                      # user signs in again
    await asyncio.wait_for(runner._task, 10)
    st = runner.snapshot()
    assert sorted(client.deleted) == ["nb2024aaaa", "nb2025bbbb"]
    assert db.get("nb2025bbbb")["stage"] == DELETED
    assert st["failed"] == 0 and st["needs_auth"] is False and st["finished"] == "completed"


async def test_stop_while_waiting_for_sign_in(tmp_path):
    client, drive = FakeNLM(notebooks()), FakeDrive()
    client.expire_on_export = "nb2024aaaa"
    runner, db, _ = await setup(tmp_path, client, drive, delete_confirmed=True)
    runner.auth_poll_sec = 0.02
    await runner.start({"delete": True})
    await wait_until(lambda: runner.snapshot().get("needs_auth"))
    runner.stop()
    await asyncio.wait_for(runner._task, 5)
    assert runner.snapshot()["finished"] == "stopped" and client.deleted == []
    assert db.get("nb2024aaaa")["stage"] == NEW


async def test_run_does_not_start_on_a_dead_session(tmp_path):
    client, drive = FakeNLM(notebooks()), FakeDrive()
    client.session_ok = False
    runner, db, _ = await setup(tmp_path, client, drive, delete_confirmed=True)
    st = await run(runner, delete=True)
    assert st["finished"] == "error" and st["failed"] == 0 and client.deleted == []
    assert all(r["stage"] == NEW for r in db.list_notebooks())


async def test_expired_session_during_source_download_is_not_an_incomplete_export(tmp_path):
    client, drive = FakeNLM(notebooks()), FakeDrive()
    client.auth_fail_fulltext = {"nb2024aaaa-s0"}
    _, db, _ = await setup(tmp_path, client, drive)
    row = db.get("nb2024aaaa")
    with pytest.raises(NotebookLMAuthError):
        await nlm.export_notebook(client, {k: row.get(k) for k in ("id", "title", "emoji", "created_at", "year",
                                                                   "sources_count", "is_owner")}, tmp_path / "x")


@pytest.mark.parametrize("exc,expected", [
    (RuntimeError(AUTH_ERR), True),
    (ValueError("CSRF token not found in HTML. Final URL: https://notebook.google.com/"), True),
    (RuntimeError("Failed to extract CSRF token (SNlM0e). Page structure may have changed"), True),
    (RuntimeError("Authentication expired. Run 'notebooklm login' to re-authenticate."), True),
    (NotebookLMAuthError("x"), True),
    (RuntimeError("outer"), False),
    (RuntimeError("The server rejected this request (resource exhausted)."), False),
    (RuntimeError("boom"), False),
    (TimeoutError("read timed out"), False),
])
def test_auth_error_classification(exc, expected):
    assert nlm.is_auth_error(exc) is expected


def test_auth_error_found_through_cause_chain():
    try:
        try:
            raise RuntimeError(AUTH_ERR)
        except RuntimeError as inner:
            raise RuntimeError("wrapper") from inner
    except RuntimeError as outer:
        assert nlm.is_auth_error(outer)


def test_auth_failed_rows_are_requeued_but_real_failures_are_not(tmp_path):
    db = StateDB(tmp_path / "s.sqlite")
    db.upsert_listing([notebook_to_dict(n) for n in notebooks()], [2024, 2025])
    db.set_stage("nb2024aaaa", FAILED, error="RPCError: The server rejected this request (unauthenticated).", attempts=1)
    db.set_stage("nb2025bbbb", FAILED, error="DriveError: cloud verification failed", attempts=1)
    db.set_stage("nb2023cccc", CLOUD_VERIFIED, error="RPCError: ... (unauthenticated).")
    assert db.reset_auth_failures() == 1
    assert db.get("nb2024aaaa")["stage"] == NEW and db.get("nb2024aaaa")["attempts"] == 0
    assert db.get("nb2025bbbb")["stage"] == FAILED
    assert db.get("nb2023cccc")["stage"] == CLOUD_VERIFIED   # verified Drive copy untouched
