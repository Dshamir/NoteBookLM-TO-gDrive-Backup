"""Reconnect NotebookLM: host helper (scripts/reconnect_helper.py) + container API (app/reconnect.py, /api/reconnect*).

The helper runs for real, in-process on a loopback port, with a *fake* sign-in command instead of Chrome, so the
whole chain (UI API -> bearer-token HTTP -> subprocess -> cookie file) is exercised without any browser.
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import main, reconnect
from scripts import reconnect_helper as rh

TOKEN = "t0k3n-for-tests"

FAKE_LOGIN = r'''
import json, pathlib, sys, time
storage, mode_file, flavor = sys.argv[1:4]
mode = pathlib.Path(mode_file).read_text().strip()
print("\x1b[32mlaunching sign-in\x1b[0m", flush=True)
if mode == "hang":
    time.sleep(60)
if mode == "fail":
    sys.exit(1)
if mode == "ok":
    p = pathlib.Path(storage)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"flavor": flavor}))
'''


@pytest.fixture
def make_helper(tmp_path):
    """Factory: start a real helper server with a fake login command. Returns a handle with .port/.token/.job."""
    started = []

    def factory(mode="ok", max_sec=30):
        storage = tmp_path / "nlm" / "profiles" / "default" / "storage_state.json"
        mode_file = tmp_path / "mode.txt"
        mode_file.write_text(mode)
        script = tmp_path / "fake_login.py"
        script.write_text(FAKE_LOGIN)
        job = rh.LoginJob(lambda fresh: [sys.executable, str(script), str(storage), str(mode_file), "fresh" if fresh else "plain"],
                          storage, max_sec)
        shut = threading.Event()
        server = ThreadingHTTPServer(("127.0.0.1", 0), rh.make_handler(job, TOKEN, shut.set))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        h = SimpleNamespace(port=server.server_address[1], token=TOKEN, job=job, storage=storage, shut=shut, server=server)
        started.append(h)
        return h

    yield factory
    for h in started:
        h.job.cancel()
        h.server.shutdown()
        h.server.server_close()


def req(h, method, path, *, token=True, host=None, origin=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", h.port, timeout=10)
    headers = {}
    if token is True:
        headers["Authorization"] = f"Bearer {h.token}"
    elif token:
        headers["Authorization"] = f"Bearer {token}"
    if host:
        headers["Host"] = host
    if origin:
        headers["Origin"] = origin
    payload = None
    if body is not None:
        payload = json.dumps(body)
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    r = conn.getresponse()
    data = json.loads(r.read() or b"{}")
    conn.close()
    return r.status, data


def wait_state(h, want=("success", "failed"), timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        snap = h.job.snapshot()
        if snap["state"] in want:
            return snap
        time.sleep(0.05)
    raise AssertionError(f"helper stuck in {h.job.snapshot()}")


# ----------------------------------------------------------------------------- helper: security


def test_helper_requires_the_bearer_token(make_helper):
    h = make_helper()
    assert req(h, "GET", "/status", token=False)[0] == 401
    assert req(h, "GET", "/status", token="wrong")[0] == 401
    assert req(h, "POST", "/login", token="wrong", body={})[0] == 401
    assert h.job.snapshot()["state"] == "idle"  # nothing was launched


def test_helper_refuses_foreign_host_header_dns_rebinding(make_helper):
    h = make_helper()
    assert req(h, "GET", "/status", host="evil.example")[0] == 403
    assert req(h, "GET", "/status", host=f"evil.example:{h.port}")[0] == 403
    assert req(h, "GET", "/status", host=f"localhost:{h.port}")[0] == 200
    assert req(h, "GET", "/status", host=f"host.docker.internal:{h.port}")[0] == 200


def test_helper_refuses_any_browser_origin(make_helper):
    """Browsers always attach Origin on cross-site requests; even our own UI origin must go through the API."""
    h = make_helper()
    assert req(h, "POST", "/login", origin="https://evil.example", body={})[0] == 403
    assert req(h, "POST", "/login", origin="http://localhost:8765", body={})[0] == 403
    assert h.job.snapshot()["state"] == "idle"


def test_helper_unknown_paths_and_bad_bodies(make_helper):
    h = make_helper(mode="ok")
    assert req(h, "GET", "/nope")[0] == 404
    assert req(h, "POST", "/nope", body={})[0] == 404
    conn = http.client.HTTPConnection("127.0.0.1", h.port, timeout=10)
    conn.request("POST", "/login", body="{not json", headers={"Authorization": f"Bearer {TOKEN}", "Content-Length": "9"})
    assert conn.getresponse().status in (200, 202)  # garbage body = defaults, never a crash
    conn.close()
    wait_state(h)


def test_helper_shutdown_endpoint(make_helper):
    h = make_helper()
    assert req(h, "POST", "/shutdown", body={})[0] == 200
    assert h.shut.wait(3)


# ----------------------------------------------------------------------------- helper: login job


def test_login_success_needs_clean_exit_and_a_fresh_cookie_file(make_helper):
    h = make_helper(mode="ok")
    status, snap = req(h, "POST", "/login", body={})
    assert status == 202 and snap["state"] == "running" and snap["already_running"] is False
    done = wait_state(h)
    assert done["state"] == "success" and "continue by itself" in done["message"]
    assert json.loads(h.storage.read_text())["flavor"] == "plain"
    assert any("launching sign-in" in line for line in done["tail"])
    assert not any("\x1b" in line for line in done["tail"])  # ANSI colour codes stripped


def test_exit_zero_without_writing_cookies_is_a_failure(make_helper):
    h = make_helper(mode="nofile")
    req(h, "POST", "/login", body={})
    done = wait_state(h)
    assert done["state"] == "failed" and "did not complete" in done["message"]


def test_a_stale_cookie_file_from_before_does_not_count_as_success(make_helper):
    h = make_helper(mode="nofile")
    h.storage.parent.mkdir(parents=True)
    h.storage.write_text("{}")
    old = time.time() - 3600
    os.utime(h.storage, (old, old))
    req(h, "POST", "/login", body={})
    assert wait_state(h)["state"] == "failed"


def test_nonzero_exit_is_a_failure(make_helper):
    h = make_helper(mode="fail")
    req(h, "POST", "/login", body={})
    assert wait_state(h)["state"] == "failed"


def test_fresh_flag_is_forwarded(make_helper):
    h = make_helper(mode="ok")
    req(h, "POST", "/login", body={"fresh": True})
    assert wait_state(h)["state"] == "success"
    assert json.loads(h.storage.read_text())["flavor"] == "fresh"


def test_double_click_is_single_flight_and_cancel_works(make_helper):
    h = make_helper(mode="hang")
    s1, a = req(h, "POST", "/login", body={})
    s2, b = req(h, "POST", "/login", body={})
    assert (s1, a["already_running"]) == (202, False)
    assert (s2, b["already_running"], b["state"]) == (200, True, "running")
    status, c = req(h, "POST", "/cancel", body={})
    assert status == 200 and c["cancelled"] is True
    done = wait_state(h)
    assert done["state"] == "failed" and done["message"] == "Cancelled."
    # cancelling again when nothing runs is harmless
    assert req(h, "POST", "/cancel", body={})[1]["cancelled"] is False
    # and a new sign-in can start afterwards
    h.job._build_cmd = lambda fresh: [sys.executable, "-c", "pass"]
    assert req(h, "POST", "/login", body={})[0] == 202
    wait_state(h)


def test_a_sign_in_nobody_finishes_is_killed_by_the_time_limit(make_helper):
    h = make_helper(mode="hang", max_sec=0.6)
    req(h, "POST", "/login", body={})
    done = wait_state(h, timeout=15)
    assert done["state"] == "failed" and "Timed out" in done["message"]


def test_missing_login_script_reports_instead_of_crashing(tmp_path):
    job = rh.LoginJob(lambda fresh: ["/definitely/not/a/real/binary"], tmp_path / "s.json", 5)
    assert job.start() is True
    end = time.time() + 5
    while job.snapshot()["state"] == "running" and time.time() < end:
        time.sleep(0.05)
    snap = job.snapshot()
    assert snap["state"] == "failed" and "Could not start" in snap["message"]


def test_default_login_command_shape(tmp_path):
    cmd = rh.default_login_cmd(tmp_path / "login.ps1", tmp_path / "nlm", "0.8.4", "chrome")(True)
    assert cmd[0] == "powershell.exe" and "-File" in cmd and cmd[-1] == "-Fresh"
    assert cmd[cmd.index("-Version") + 1] == "0.8.4" and cmd[cmd.index("-Browser") + 1] == "chrome"
    assert "-Fresh" not in rh.default_login_cmd(tmp_path / "login.ps1", tmp_path / "nlm", "", "auto")(False)


def test_atomic_write_json_leaves_no_temp_file(tmp_path):
    rh.atomic_write_json(tmp_path / "sub" / "helper.json", {"a": 1})
    assert json.loads((tmp_path / "sub" / "helper.json").read_text()) == {"a": 1}
    assert [p.name for p in (tmp_path / "sub").iterdir()] == ["helper.json"]


# ----------------------------------------------------------------------------- container API


@pytest.fixture
def api(make_helper, tmp_path, monkeypatch):
    """TestClient wired to a real helper (conf file + loopback host), plus a handle to the helper."""
    conf = tmp_path / "helper.json"
    monkeypatch.setattr(reconnect, "HELPER_CONF", conf)
    monkeypatch.setattr(reconnect, "HELPER_HOST", "127.0.0.1")
    monkeypatch.setitem(main._reconnect_seen, "finished_at", None)

    def connect(mode="ok", **kw):
        h = make_helper(mode=mode, **kw)
        conf.write_text(json.dumps({"port": h.port, "token": h.token}))
        return h

    client = TestClient(main.app)
    client.connect = connect  # type: ignore[attr-defined]
    client.conf = conf  # type: ignore[attr-defined]
    return client


def test_status_says_unavailable_with_a_hint_when_no_helper(api):
    r = api.get("/api/reconnect/status")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False and body["state"] == "unavailable"
    assert "helper start" in body["hint"] and "nlm2drive login" in body["hint"]


def test_start_without_helper_is_503_with_instructions(api):
    r = api.post("/api/reconnect", json={})
    assert r.status_code == 503
    assert "helper start" in r.json()["error"]


def test_start_with_a_dead_helper_port_is_503_not_a_500(api, tmp_path):
    api.conf.write_text(json.dumps({"port": 1, "token": "x"}))  # nothing listens on port 1
    r = api.post("/api/reconnect", json={})
    assert r.status_code == 503 and "did not answer" in r.json()["error"]


def test_stale_token_after_a_helper_restart_is_explained(api):
    h = api.connect()
    api.conf.write_text(json.dumps({"port": h.port, "token": "old-token"}))
    r = api.post("/api/reconnect", json={})
    assert r.status_code == 503 and "restarted" in r.json()["error"]


def test_garbage_helper_conf_is_treated_as_missing(api):
    for junk in ("not json", "[]", '{"port": "x", "token": "y"}', '{"port": 99999, "token": "y"}', '{"port": 80}'):
        api.conf.write_text(junk)
        assert reconnect.read_conf() is None
    assert api.get("/api/reconnect/status").json()["available"] is False


def test_full_reconnect_flow_through_the_api(api):
    h = api.connect(mode="ok")
    idle = api.get("/api/reconnect/status").json()
    assert idle["available"] is True and idle["state"] == "idle"

    started = api.post("/api/reconnect", json={})
    assert started.status_code == 200 and started.json()["state"] == "running"
    wait_state(h)

    first = api.get("/api/reconnect/status").json()
    assert first["state"] == "success"
    n_events = len([e for e in main.db.events(200) if "completed through the Reconnect button" in e["message"]])
    assert n_events == 1
    api.get("/api/reconnect/status")  # polling again must NOT repeat the side effects
    assert len([e for e in main.db.events(200) if "completed through the Reconnect button" in e["message"]]) == 1


def test_second_sign_in_is_recognised_as_new(api):
    h = api.connect(mode="ok")
    api.post("/api/reconnect", json={})
    wait_state(h)
    api.get("/api/reconnect/status")
    time.sleep(0.01)
    api.post("/api/reconnect", json={})
    wait_state(h)
    api.get("/api/reconnect/status")
    assert len([e for e in main.db.events(500) if "completed through the Reconnect button" in e["message"]]) >= 2


def test_cross_site_requests_are_refused(api):
    h = api.connect(mode="hang")
    bad = api.post("/api/reconnect", json={}, headers={"Origin": "https://evil.example"})
    assert bad.status_code == 403
    assert api.post("/api/reconnect/cancel", headers={"Origin": "http://evil.localhost.example"}).status_code == 403
    assert api.post("/api/reconnect", json={}, headers={"Origin": "http://127.0.0.1:8765"}).status_code == 200
    assert h.job.snapshot()["state"] == "running"  # only the same-machine request launched anything
    assert api.post("/api/reconnect/cancel", headers={"Origin": "http://localhost:8765"}).status_code == 200


def test_double_click_through_the_api_reports_already_running(api):
    api.connect(mode="hang")
    assert api.post("/api/reconnect", json={}).json()["already_running"] is False
    assert api.post("/api/reconnect", json={}).json()["already_running"] is True
    api.post("/api/reconnect/cancel")


def test_reconnect_is_refused_while_a_run_holds_a_working_session(api, monkeypatch):
    h = api.connect(mode="ok")
    monkeypatch.setattr(type(main.runner), "running", property(lambda self: True))
    monkeypatch.setitem(main.runner.status, "needs_auth", False)
    r = api.post("/api/reconnect", json={})
    assert r.status_code == 409 and "working NotebookLM session" in r.json()["error"]
    assert h.job.snapshot()["state"] == "idle"
    # ...but when the run is waiting for a sign-in the button is exactly what is needed
    monkeypatch.setitem(main.runner.status, "needs_auth", True)
    assert api.post("/api/reconnect", json={}).status_code == 200
    wait_state(h)


def test_fresh_flag_reaches_the_login_command(api):
    h = api.connect(mode="ok")
    api.post("/api/reconnect", json={"fresh": True})
    assert wait_state(h)["state"] == "success"
    assert json.loads(h.storage.read_text())["flavor"] == "fresh"


def test_ui_ships_the_reconnect_controls():
    from pathlib import Path
    static = Path(main.STATIC)
    html, js = (static / "index.html").read_text(encoding="utf-8"), (static / "app.js").read_text(encoding="utf-8")
    for el in ("btn-reconnect", "btn-reconnect-banner", "reconnect-status", "reconnect-banner-status"):
        assert f'id="{el}"' in html
    assert "/api/reconnect" in js and "/api/reconnect/status" in js

# ----------------------------------------------------------------------------- attack-phase regressions


def test_cancel_that_arrives_before_the_process_exists_still_kills_it(tmp_path):
    job = rh.LoginJob(lambda fresh: [sys.executable, "-c", "import time; time.sleep(60)"], tmp_path / "s.json", 30)
    job.state, job.started_at, job._cancelled = "running", time.time(), True  # Cancel won the race against Popen
    t = threading.Thread(target=job._run, args=(False,))
    t.start()
    t.join(10)
    assert not t.is_alive(), "login subprocess was left running after Cancel"
    assert job.snapshot()["message"] == "Cancelled."


def test_oversized_request_body_is_ignored_and_helper_stays_healthy(make_helper):
    h = make_helper(mode="ok")
    conn = http.client.HTTPConnection("127.0.0.1", h.port, timeout=10)
    conn.request("POST", "/login", headers={"Authorization": f"Bearer {TOKEN}", "Content-Length": "9999999"})
    assert conn.getresponse().status in (200, 202)  # body never read: defaults, no crash, no hang
    conn.close()
    wait_state(h)
    assert req(h, "GET", "/status")[0] == 200


def test_runs_are_refused_while_a_sign_in_is_rewriting_the_cookie_file(api):
    h = api.connect(mode="hang")
    api.post("/api/reconnect", json={})
    r = api.post("/api/run", json={})
    assert r.status_code == 409 and "sign-in is in progress" in r.json()["error"]
    assert api.post("/api/test-e2e").status_code == 409
    api.post("/api/reconnect/cancel")
    wait_state(h)
