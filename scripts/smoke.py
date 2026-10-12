"""Live smoke test: hit a RUNNING nlm2drive instance and check it is wired together.

Read-only and quota-free — it never starts a run, never calls NotebookLM/Drive.
Run inside the container (`./nlm2drive smoke`) or point it anywhere: python smoke.py [base_url]
Exit code 0 = all checks passed.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765").rstrip("/")
failures: list[str] = []


def get(path: str) -> tuple[int, bytes, str]:
    try:
        with urllib.request.urlopen(BASE + path, timeout=15) as r:  # noqa: S310 - fixed http base
            return r.status, r.read(), r.headers.get("content-type", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("content-type", "")


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def json_get(path: str) -> tuple[int, object]:
    code, body, _ = get(path)
    try:
        return code, json.loads(body)
    except ValueError:
        return code, None


print(f"smoke → {BASE}")

code, data = json_get("/api/health")
check("health", code == 200 and data == {"ok": True}, f"{code} {data}")

code, summ = json_get("/api/summary")
keys = {"status", "counts", "settings", "limit", "delete_phrase"}
check("summary shape", code == 200 and isinstance(summ, dict) and keys <= set(summ), f"{code}")
if isinstance(summ, dict):
    st = summ.get("status", {})
    check("status has run/auth fields", {"state", "needs_auth"} <= set(st), str(sorted(st)))
    check("delete safety phrase intact", summ.get("delete_phrase") == "DELETE AFTER BACKUP")

for path in ("/api/notebooks", "/api/runs", "/api/events/history", "/api/settings"):
    code, data = json_get(path)
    check(f"GET {path}", code == 200 and data is not None, str(code))

code, data = json_get("/api/reconnect/status")
check("reconnect status shape", code == 200 and isinstance(data, dict) and "available" in data and "state" in data, f"{code} {data}")
if isinstance(data, dict):
    print(f"       (reconnect helper: {'running' if data.get('available') else 'not running - optional'})")


code, body, ctype = get("/")
check("index.html served", code == 200 and b"nlm2drive" in body.lower() and "html" in ctype, f"{code} {ctype}")
html = body.decode("utf-8", "replace")
for asset in ("app.js", "styles.css"):
    check(f"{asset} referenced by index.html", asset in html)
    c, b = get("/" + asset)[:2]
    check(f"asset {asset}", c == 200 and len(b) > 500, str(c))
check("reconnect button in UI", 'id="btn-reconnect"' in html and 'id="btn-reconnect-banner"' in html)

code = get("/api/does-not-exist")[0]
check("unknown API → 404", code == 404, str(code))

print("PASS" if not failures else f"FAILED: {', '.join(failures)}")
sys.exit(1 if failures else 0)
