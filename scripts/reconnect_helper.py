"""Host-side "Reconnect NotebookLM" helper (ADR-011).

Why this exists: NotebookLM has no OAuth/public API - its session is a Google *browser* session, and the
Docker container cannot open Chrome on your desktop. This tiny stdlib-only HTTP service runs on the
Windows host, and when the web UI's "Reconnect NotebookLM" button is pressed it launches the exact same
sign-in as `.\\nlm2drive login` (scripts/login.ps1 -> scripts/nlm_login.py). The new cookies land in
data/notebooklm/, and the paused run notices the changed file and carries on by itself.

Security model (the UI can delete notebooks, so this is a loopback-only, single-purpose service):
  * binds 127.0.0.1 only;
  * every request needs `Authorization: Bearer <token>`; the token is random per start and stored in
    data/helper.json (git-ignored; readable by the container through the /data mount, never by web pages);
  * requests carrying an `Origin` header are rejected (every cross-site browser request has one);
  * the Host header must be 127.0.0.1 / localhost / host.docker.internal (DNS-rebinding defence);
  * one login at a time, hard time limit, and no cookie values are ever returned or logged.

usage: python reconnect_helper.py --data-dir <host path of data/notebooklm> [--conf <helper.json>]
       [--login-script <login.ps1>] [--version <notebooklm-py version>] [--browser auto|chrome|msedge|chromium]
"""
from __future__ import annotations

import argparse
import collections
import hmac
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

DEFAULT_PORT = 8766
ALLOWED_HOSTNAMES = {"127.0.0.1", "localhost", "host.docker.internal"}
MAX_LOGIN_SEC = 15 * 60
MAX_BODY = 4096
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _now() -> float:
    return time.time()


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


class LoginJob:
    """Runs one sign-in subprocess at a time and exposes a small status snapshot."""

    def __init__(self, build_cmd: Callable[[bool], Sequence[str]], storage: Path, max_sec: float = MAX_LOGIN_SEC) -> None:
        self._build_cmd = build_cmd
        self._storage = storage
        self._max_sec = max_sec
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._cancelled = False
        self._timed_out = False
        self._tail: collections.deque[str] = collections.deque(maxlen=14)
        self.state = "idle"  # idle | running | success | failed
        self.message = "Ready."
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.fresh = False

    # ------------------------------------------------------------------ control
    def start(self, fresh: bool = False) -> bool:
        """Start a login; False when one is already running (single-flight, so double clicks are harmless)."""
        with self._lock:
            if self.state == "running":
                return False
            self.state, self.message = "running", "Opening the browser - sign in to Google there..."
            self.started_at, self.finished_at, self.fresh = _now(), None, fresh
            self._cancelled = self._timed_out = False
            self._tail.clear()
        threading.Thread(target=self._run, args=(fresh,), daemon=True, name="login-job").start()
        return True

    def cancel(self) -> bool:
        with self._lock:
            if self.state != "running":
                return False
            self._cancelled = True
        self._kill()
        return True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            end = self.finished_at or _now()
            return {
                "state": self.state, "message": self.message, "fresh": self.fresh,
                "started_at": self.started_at, "finished_at": self.finished_at,
                "elapsed": round(end - self.started_at, 1) if self.started_at else 0,
                "tail": list(self._tail),
            }

    # ------------------------------------------------------------------ internals
    def _push(self, line: str) -> None:
        line = _ANSI.sub("", line).strip()
        if line:
            with self._lock:
                self._tail.append(line[:200])

    def _kill(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            if os.name == "nt":  # kill the whole tree: powershell -> uv -> python -> the browser window
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=20, check=False)
            else:
                proc.kill()
        except Exception:  # noqa: BLE001
            proc.kill()

    def _finish(self, ok: bool, message: str) -> None:
        with self._lock:
            self.state = "success" if ok else "failed"
            self.message = message
            self.finished_at = _now()
            self._proc = None

    def _run(self, fresh: bool) -> None:
        started = _now()
        timer: threading.Timer | None = None
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            proc = subprocess.Popen(
                list(self._build_cmd(fresh)), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace", creationflags=flags, stdin=subprocess.DEVNULL)
        except OSError as e:
            self._finish(False, f"Could not start the sign-in: {e}")
            return
        self._proc = proc
        if self._cancelled:  # Cancel arrived before the process handle existed (it was a no-op then)
            self._kill()
        # A user who walks away must not leave a browser + subprocess hanging forever.
        timer = threading.Timer(self._max_sec, self._on_timeout)
        timer.daemon = True
        timer.start()
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                self._push(line)
            rc = proc.wait()
        except Exception as e:  # noqa: BLE001
            self._finish(False, f"Sign-in process error: {type(e).__name__}")
            return
        finally:
            if timer:
                timer.cancel()
        # Success needs BOTH a clean exit and a cookie file written after we started
        # (login.ps1 additionally verifies the session with `notebooklm auth check --test`).
        try:
            fresh_file = self._storage.stat().st_mtime >= started - 1
        except OSError:
            fresh_file = False
        if self._cancelled:
            self._finish(False, "Cancelled.")
        elif self._timed_out:
            self._finish(False, f"Timed out after {int(self._max_sec // 60)} min - press Reconnect to try again.")
        elif rc == 0 and fresh_file:
            self._finish(True, "Signed in - the run will continue by itself.")
        else:
            self._finish(False, "Sign-in did not complete (window closed, wrong account, or the session check failed).")

    def _on_timeout(self) -> None:
        self._timed_out = True
        self._kill()


def make_handler(job: LoginJob, token: str, on_shutdown: Callable[[], None]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "nlm2drive-helper"
        protocol_version = "HTTP/1.1"
        timeout = 15  # a client that stalls mid-request must not hold a thread (and the port) forever

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - quiet: never log headers/tokens
            return

        def _send(self, code: int, body: dict[str, Any]) -> None:
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _guard(self) -> bool:
            host = (self.headers.get("Host") or "").lower()
            hostname = host.rsplit(":", 1)[0] if ":" in host else host
            if hostname not in ALLOWED_HOSTNAMES:
                self._send(403, {"error": "bad host"})
                return False
            if self.headers.get("Origin") is not None:
                self._send(403, {"error": "browser requests are not accepted"})
                return False
            given = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
            if not hmac.compare_digest(given.encode(), token.encode()):
                self._send(401, {"error": "unauthorized"})
                return False
            return True

        def _body(self) -> dict[str, Any]:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return {}
            if n <= 0 or n > MAX_BODY:
                if n > MAX_BODY:
                    self.close_connection = True  # we did not read the body: the connection is desynced
                return {}
            try:
                data = json.loads(self.rfile.read(n))
            except ValueError:
                return {}
            return data if isinstance(data, dict) else {}

        def do_GET(self) -> None:  # noqa: N802
            if not self._guard():
                return
            if self.path == "/status":
                self._send(200, job.snapshot())
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._guard():
                return
            body = self._body()
            if self.path == "/login":
                started = job.start(fresh=bool(body.get("fresh")))
                self._send(202 if started else 200, {**job.snapshot(), "already_running": not started})
            elif self.path == "/cancel":
                self._send(200, {**job.snapshot(), "cancelled": job.cancel()})
            elif self.path == "/shutdown":
                self._send(200, {"ok": True})
                on_shutdown()
            else:
                self._send(404, {"error": "not found"})

    return Handler


class QuietServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that does not print a traceback when a client simply drops the connection."""

    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        super().handle_error(request, client_address)


def default_login_cmd(login_script: Path, data_dir: Path, version: str, browser: str) -> Callable[[bool], list[str]]:
    def build(fresh: bool) -> list[str]:
        cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(login_script),
               "-DataDir", str(data_dir), "-Browser", browser]
        if version:
            cmd += ["-Version", version]
        if fresh:
            cmd += ["-Fresh"]
        return cmd
    return build


def main(argv: Sequence[str] | None = None) -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", required=True, help="host path of data/notebooklm (NOTEBOOKLM_HOME)")
    ap.add_argument("--conf", default=None, help="where to write {port, token, pid} (default: <data-dir>/../helper.json)")
    ap.add_argument("--login-script", default=str(here / "login.ps1"))
    ap.add_argument("--version", default="")
    ap.add_argument("--browser", default="auto", choices=["auto", "chrome", "msedge", "chromium"])
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--max-sec", type=int, default=MAX_LOGIN_SEC)
    args = ap.parse_args(argv)

    data_dir = Path(args.data_dir).resolve()
    conf = Path(args.conf) if args.conf else data_dir.parent / "helper.json"
    storage = data_dir / "profiles" / "default" / "storage_state.json"
    token = secrets.token_urlsafe(32)
    job = LoginJob(default_login_cmd(Path(args.login_script), data_dir, args.version, args.browser), storage, args.max_sec)

    server_box: list[ThreadingHTTPServer] = []

    def shutdown() -> None:
        threading.Thread(target=lambda: server_box[0].shutdown(), daemon=True).start()

    try:
        server = QuietServer(("127.0.0.1", args.port), make_handler(job, token, shutdown))
    except OSError as e:
        print(f"cannot listen on 127.0.0.1:{args.port}: {e} (already running?)", file=sys.stderr)
        return 3
    server_box.append(server)
    atomic_write_json(conf, {"port": args.port, "token": token, "pid": os.getpid(), "started": _now()})
    print(f"nlm2drive reconnect helper listening on 127.0.0.1:{args.port} (conf: {conf})", flush=True)
    if sys.platform == "win32":
        try:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        job.cancel()
        server.server_close()
        try:  # only remove the config if it is still ours (a newer helper may have replaced it)
            if json.loads(conf.read_text(encoding="utf-8")).get("token") == token:
                conf.unlink()
        except (OSError, ValueError):
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
