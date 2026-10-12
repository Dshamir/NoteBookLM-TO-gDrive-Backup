"""Robust NotebookLM sign-in (host side), used by scripts/login.ps1.

notebooklm-py's `notebooklm login` treats "page is on notebooklm.google.com" as "already logged in";
NotebookLM now serves a public landing page to signed-out visitors, so it saves an empty session
immediately. This helper instead opens Google's sign-in page and waits until the real Google
session cookies exist, then writes the Playwright storage_state where notebooklm-py expects it.

usage: python nlm_login.py [chrome|msedge|chromium] [timeout_seconds] [--fresh]
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

REQUIRED = {"SID", "__Secure-1PSIDTS"}
NLM = "https://notebooklm.google.com/"
LOGIN_URL = "https://accounts.google.com/ServiceLogin?hl=en&continue=https%3A%2F%2Fnotebooklm.google.com%2F"


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    channel = args[0] if args else "chrome"
    timeout = int(args[1]) if len(args) > 1 else 600
    fresh = "--fresh" in sys.argv

    home = Path(os.environ.get("NOTEBOOKLM_HOME", Path.home() / ".notebooklm"))
    profile = home / "profiles" / "default"
    storage = profile / "storage_state.json"
    user_data = profile / "browser_profile"
    if fresh and user_data.exists():
        shutil.rmtree(user_data, ignore_errors=True)
        print("Cleared the saved browser profile (--fresh).")
    user_data.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        kw = dict(user_data_dir=str(user_data), headless=False, no_viewport=True,
                  ignore_default_args=["--enable-automation"],
                  args=["--disable-blink-features=AutomationControlled", "--start-maximized"])
        if channel != "chromium":
            kw["channel"] = channel
        ctx = p.chromium.launch_persistent_context(**kw)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        print(f"Waiting up to {timeout // 60} min for you to sign in to Google in the {channel} window ...", flush=True)

        deadline = time.time() + timeout
        ok = False
        tick = 0
        while time.time() < deadline:
            try:
                names = {c["name"] for c in ctx.cookies() if c.get("domain", "").endswith("google.com")}
                urls = [pg.url for pg in ctx.pages]
            except Exception:  # browser closed by the user
                break
            if REQUIRED <= names:
                ok = True
                break
            tick += 1
            if tick % 5 == 0:
                print(f"  still waiting - signed-in cookies: {sorted(REQUIRED & names) or 'none'}; "
                      f"tabs: {[u[:60] for u in urls]}", flush=True)
            time.sleep(2)

        if not ok:
            try:
                ctx.close()
            except Exception:
                pass
            print("Sign-in not completed (timed out or the window was closed).", file=sys.stderr)
            return 2

        # Google session exists -> open NotebookLM ourselves so its first-party cookies are set.
        print("Google sign-in detected - opening NotebookLM to finish ...", flush=True)
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            pg.goto(NLM, wait_until="domcontentloaded", timeout=60000)
            pg.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        print(f"  landed on: {pg.url[:100]}", flush=True)

        # Google issues the rotation cookies (__Secure-1PSIDRTS / 3PSIDRTS) a little AFTER sign-in. A session
        # saved without them could not be kept alive and was revoked ~2 h into a long run (ADR-010), so give
        # them time to appear (bounded; we still save and warn if they never do).
        want = {"__Secure-1PSIDRTS", "__Secure-3PSIDRTS"}
        deadline = time.time() + 60
        names = set()
        while time.time() < deadline:
            try:
                names = {c["name"] for c in ctx.cookies() if c.get("domain", "").endswith("google.com")}
            except Exception:
                break
            if want <= names:
                break
            print("  waiting for Google's session-rotation cookies ...", flush=True)
            time.sleep(3)
        if want <= names:
            print("  rotation cookies present - long-lived session.", flush=True)
        else:
            print("  WARNING: rotation cookies (__Secure-*PSIDRTS) did not appear; this session may expire "
                  "within a few hours. Re-run login later or keep NotebookLM open in that window longer.",
                  file=sys.stderr, flush=True)
        storage.parent.mkdir(parents=True, exist_ok=True)
        ctx.storage_state(path=str(storage))
        ctx.close()
    print(f"Session saved to {storage}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
