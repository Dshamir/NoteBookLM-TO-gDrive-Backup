"""Container side of the "Reconnect NotebookLM" button (ADR-011).

NotebookLM has no OAuth: its session is a Google *browser* session. The container cannot open Chrome on the
host, so the UI asks a small loopback-only helper running on the host (scripts/reconnect_helper.py) to run the
normal sign-in. This module is the thin, defensive client for that helper:

  * the helper's port + bearer token come from /data/helper.json (written by the helper at start-up; the
    token never reaches the browser - the UI only talks to *this* API);
  * every failure mode (no helper, stale config, helper hung, bad JSON) becomes `HelperUnavailable` with a
    message that tells the user what to run instead.
"""
from __future__ import annotations

import json
import os
from typing import Any

import httpx

from .config import DATA_DIR

HELPER_CONF = DATA_DIR / "helper.json"
HELPER_HOST = os.environ.get("RECONNECT_HELPER_HOST", "host.docker.internal")
TIMEOUT = 5.0
HINT = r"Run `.\nlm2drive helper start` once (or sign in directly with `.\nlm2drive login`)."


class HelperUnavailable(Exception):
    """The host helper is not running / not reachable / misbehaving."""


def read_conf() -> dict[str, Any] | None:
    try:
        raw = json.loads(HELPER_CONF.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    port, token = raw.get("port"), raw.get("token")
    if not isinstance(port, int) or not 0 < port < 65536 or not isinstance(token, str) or not token:
        return None
    return {"port": port, "token": token}


async def call(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    conf = read_conf()
    if conf is None:
        raise HelperUnavailable("The reconnect helper is not running on this PC.")
    url = f"http://{HELPER_HOST}:{conf['port']}{path}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.request(method, url, headers={"Authorization": f"Bearer {conf['token']}"}, json=body)
    except httpx.HTTPError as e:
        HELPER_CONF.unlink(missing_ok=True)
        raise HelperUnavailable("The reconnect helper did not answer (stopped or restarted?).") from e
    if resp.status_code == 401:
        raise HelperUnavailable("The reconnect helper restarted - reload this page and try again.")
    try:
        data = resp.json()
    except ValueError as e:
        raise HelperUnavailable("The reconnect helper sent an unreadable answer.") from e
    if resp.status_code >= 400 or not isinstance(data, dict):
        raise HelperUnavailable(f"The reconnect helper refused the request ({resp.status_code}).")
    return data


async def status() -> dict[str, Any]:
    try:
        return {"available": True, **await call("GET", "/status")}
    except HelperUnavailable as e:
        return {"available": False, "state": "unavailable", "message": str(e), "hint": HINT, "tail": []}


async def start(fresh: bool = False) -> dict[str, Any]:
    return {"available": True, **await call("POST", "/login", {"fresh": fresh})}


async def cancel() -> dict[str, Any]:
    return {"available": True, **await call("POST", "/cancel", {})}
