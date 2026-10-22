# AGENTS.md: continuity notes for maintainers and AI coding agents

Read this first when resuming work on this project.

## What this is
`nlm2drive` archives NotebookLM notebooks to Google Drive as browsable folders, verifies every file in Drive (MD5), then deletes the notebook from NotebookLM to stay under the 500-notebook limit. It is a Dockerized FastAPI app with a vanilla-JS UI, orchestrated by `nlm2drive.sh` / `nlm2drive.cmd`.

- Owner: GitHub `Dshamir`. Primary platform: Windows 11 + Docker Desktop + Git Bash. Google Workspace account.
- Start with: `README.md`, then `docs/ARCHITECTURE.md` and `docs/DECISIONS.md`.

## Invariants (do not break)
1. **Deletion happens only through `app/runner.py::may_delete`**, and only after a fresh Drive verification (md5 + size of every manifest file) with `complete == 1`. Keep `tests/test_runner.py::test_may_delete_policy` and the deletion tests green; extend them when changing the policy.
2. **Shared (non-owner) notebooks are never selected, archived or deleted.**
3. **No AI-generation endpoints** (no `generate_*`, no chat `ask`). Only list/read/download/delete, so no user quota is consumed. The e2e test creates a notebook with text sources only.
4. **Secrets stay in `data/`** (git-ignored): `storage_state.json`, `browser_profile/`, `credentials.json`, `token.json`. Never log cookie values or tokens.
5. **UI binds to `127.0.0.1` only.**
6. **Do not reintroduce a `G:` bind mount** for archiving (ADR-002: Docker Desktop can't see the Drive virtual drive, so verification would be false).
7. Pin `notebooklm-py` (`requirements.txt` + `NOTEBOOKLM_PY_VERSION`). It uses undocumented Google RPCs.
8. **An expired Google session is a run-level pause, never a notebook failure** (ADR-010). Keep `nlm.is_auth_error` / `SessionExpired` / `Runner._recover_session` intact; never retry or mark FAILED on auth errors; exporter `except` blocks must call `_reraise_if_auth`.
9. **One NotebookLM client per cookie file.** While a run is active, API endpoints must not open another client (`_run_auth_view`; `/api/refresh` returns 409).
10. **The reconnect helper stays a loopback-only, token-protected, single-purpose service** (ADR-011): binds `127.0.0.1`, bearer token from `data/helper.json`, rejects any `Origin` and foreign `Host`, only ever runs `scripts/login.ps1`. The browser must never talk to it directly (the UI calls `/api/reconnect*`, which refuses cross-site origins, refuses while a run holds a *working* session, and `/api/run` refuses while a sign-in is in progress). `storage_state.json` is written atomically (temp + rename). Keep `tests/test_reconnect.py` green.

## How to work on it
```powershell
.\nlm2drive check     # ALL gates: ruff + mypy + pytest in the container (+ live smoke if the stack is up)
.\nlm2drive test      # build image + pytest in container (fake NotebookLM + in-memory Drive)
.\nlm2drive lint | typecheck | smoke
.\nlm2drive up        # rebuild + restart (static files are baked into the image); also starts the reconnect helper
.\nlm2drive logs
.\nlm2drive helper status|start|stop|install|uninstall   # host helper behind the UI's Reconnect button
```
- Pipeline used for every change (the owner asked for it): CODE → commit → docs → lint → typecheck → tests → smoke → adversarial review ("roast") → repair → commit → push.
- `ruff` config and the *justified* ignores live in `pyproject.toml`; do not widen them silently.
- Tests import `app.main` safely: `tests/conftest.py` points `DATA_DIR`/`NOTEBOOKLM_HOME` at a temp dir *before* any app import. Keep that.
- The host Python is 3.8 (too old). Run everything in the container, or use `uv`/`uvx` with `--python 3.12`.
- Docker CLI: `C:\Program Files\Docker\Docker\resources\bin\docker.exe` (not on PATH by default).
- In PowerShell, native stderr shows up as red `NativeCommandError` text even on success; check the real exit status.
- Line endings: `*.sh` LF, `*.cmd`/`*.ps1` CRLF (`.gitattributes`).
- PowerShell 5.1 gotchas seen here: `>` writes UTF-16; `[IO.File]::ReadAllText` + `WriteAllText` with a UTF-8 no-BOM encoding is the safe round trip; heredocs break, write scripts to `scratch/` and run them; a `docker run` that is piped through `Select-Object` only prints at the end.
- `grep_search` fails on this host; use `Select-String`. Run `Remove-Item Env:GITHUB_TOKEN` before `git push` / `gh`.

## notebooklm-py 0.8.4 API facts (verified by introspection)
- `NotebookLMClient.from_storage(keepalive=900)` is an async context manager. Storage is `$NOTEBOOKLM_HOME/profiles/default/storage_state.json`.
- Notebook fields: `id, title, emoji, created_at, modified_at, last_viewed_at, sources_count, is_owner, role`.
- `notebooks.list/get/get_or_none/get_summary/create/delete`; `sources.list/get_fulltext(output_format='markdown'|'text')/add_text(..., wait=True)`; `notes.list/create`; `mind_maps.list/get_tree`; `chat.get_history(nb, limit)` returns `[(q, a)]`.
- `artifacts.list`; `artifacts.download_{audio,video,report,slide_deck,infographic,quiz,flashcards,data_table,mind_map}(nb_id, path, artifact_id, **kw)`. Quiz/flashcards take `output_format='json'|'markdown'`; slides take `output_format='pdf'`.
- `notebooklm login` is unreliable (ADR-007). Use `scripts/nlm_login.py`.
- NotebookLM is also served at `notebook.google.com`; cookies exist for both hosts.

## Current state (2026-10-22)
- v1.0.1 (session-expiry fix) on top of v1.0.0, working end to end.
- 2026-10-22: MRU Tombstone Reconciliation & Auto-Sync (ADR-012, 83 tests, ruff, mypy, smoke all green):
  - **MRU Tombstone Reconciliation**: Google's `ListRecentlyViewedProjects` (`wXbhsf`) RPC has a 500-slot MRU buffer that retains deleted notebook tombstones, pushing older live notebooks past slot 500 when new notebooks are created. `nlm_service.list_notebooks` now probes any previously-present notebook omitted by `list()` via `get_or_none(id)` before marking it absent, and `delete_notebook` calls `remove_from_recent(id)` before deletion.
  - **Auto-Sync on Page Load**: `GET /api/auth/status` upserts the live listing into SQLite (`state.db`) and emits a `"refresh"` event so opening or reloading the dashboard automatically surfaces newly created notebooks.
  - **Windowless Host Helper**: `scripts/helper.ps1` launches the helper via WMI (`Win32_Process.Create`) with `Win32_ProcessStartup(ShowWindow=0)` (`SW_HIDE`) and uses `wscript.exe //B //NoLogo` for the Windows logon task so no console window ever flashes.
  - **Adversarial Hardening**: Invariant 2 enforced on restore (HTTP 403 for shared notebooks), path traversal prevented (`_safe_staging_dir` on content/audio/restore), preset validation (`ALLOWED_PRESETS`), bounded parallel Drive archive auditor (`asyncio.Semaphore(8)`).
  - **Extended UI Suite**: Topbar Quota Gauge (`418 / 500 Notebooks · 84%`), Quick Cleanup Presets (`Empty Drafts`, `Stale (>6 mo)`, `Oldest 25`, `Oldest 50`), 1-Click Restore to NotebookLM, Catalog Export (CSV/JSON), Drive Integrity Auditor, In-Modal Content Preview & HTML5 Audio Player, and Desktop Notifications.
- Account mix: 418 present notebooks in 2026 (1 shared), 85 deleted/archived. Drive connected, Google session authenticated.

## Open ideas / backlog
- Optional scheduled runs (e.g. monthly: archive notebooks older than N months).
- Upstream: report the `login` landing-page false positive to notebooklm-py.
- Optionally save original-file metadata for uploaded PDFs (the originals themselves are not retrievable from NotebookLM).
- Durable NotebookLM credential so no human sign-in is ever needed (master token / Firefox cookies / `NOTEBOOKLM_REFRESH_CMD`): deliberately NOT done, security trade-off, owner has not decided (ADR-010 "Not done").
- Verify on the next long run whether waiting for the `PSIDRTS` cookies at login prevents the ~2 h revocation.
- Freeing more slots from 2026 can now be done via the date cutoff selector (e.g. before March 2026), then the owner pressing **Archive & delete** (the agent never starts deleting runs on its own).
- Housekeeping not done: switch the commit email to the GitHub noreply address; upload the banner as the repo social preview (manual, GitHub settings).
- `scripts/helper.ps1 start` launches the helper through WMI (`ShowWindow=0`) so it survives the caller, never flashes a console window, and never blocks captured output; keep that if you touch the launcher.
