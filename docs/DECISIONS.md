# Design decisions (ADR log)

Each entry: context → decision → consequences. Newest last.

## ADR-001 · Build on notebooklm-py instead of the MCP-client proposal (2026-10-11)
**Context.** The original idea was a Python MCP client that called a NotebookLM MCP server and pushed results to Drive with a **service account**. Research into the existing open-source options found:
- `teng-lin/notebooklm-py` (MIT): the most complete Python API, covering list, sources full text, notes, artifacts download, chat history and delete.
- Several MCP servers built on top of similar unofficial RPCs.
- No existing tool that does *archive → verify → delete*.

**Decision.** Use notebooklm-py directly, pinned at `0.8.4`, from our own FastAPI service.

**Why not the proposal.**
- Service accounts have **no Drive storage quota** (uploads to My Drive fail with 403).
- The MCP tool names in the proposal were guesses.
- It had no year filter, no verification and no delete step.
- Its export was thin (no studio media, no chat).

**Consequences.** We depend on undocumented Google endpoints, so the version is pinned and upgrades need testing (OPERATIONS.md).

## ADR-002 · Upload with the Drive API, not to `G:\` via a bind mount (2026-10-11)
**Context.** The plan was to write into `G:\My Drive\…` (Drive for Desktop) through a Docker bind mount. A spike showed Docker Desktop **cannot see the Google Drive virtual drive**:
- the mount appeared as `/dev/sdf ext4`;
- writes landed inside the Docker VM and never reached `G:\`.

Verifying against that mount would have "passed" and then deleted notebooks whose backup didn't exist.

**Decision.** Upload with the Drive API (OAuth Desktop client, user's own quota) into `My Drive/<drive_archive_path>`. Drive for Desktop then shows the files at `G:\My Drive\…`.

**Consequences.**
- Verification is authoritative: Drive's server-side MD5 is compared with our manifest.
- It needs a one-time Google Cloud OAuth client.
- Staging happens on the E: bind mount (`./data/staging`), which does work (9p drvfs).

## ADR-003 · Per-notebook pipeline with a persisted stage machine (2026-10-11)
**Decision.** Process one notebook at a time:
- `NEW → EXPORTING → EXPORTED → UPLOADING → UPLOADED → CLOUD_VERIFIED → DELETING → DELETED`, persisted in SQLite;
- parallelism only inside a notebook (parallel file uploads, default 4).

**Why.**
- It bounds the blast radius.
- It makes crash recovery trivial.
- It respects rate limits (2 s pause between notebooks).
- Progress is easy to reason about.

## ADR-004 · Single deletion gate `may_delete()` (2026-10-11)
**Decision.** All deletion decisions go through `runner.may_delete(row, opts, settings)`. Deletion requires:
- `stage == CLOUD_VERIFIED`, `complete == 1`, owner, and a Drive folder id;
- run option `delete`, setting `delete_after_verify`, and the typed confirmation `delete_confirmed`.

The only exception is the e2e self-test notebook created by the app itself.

**Consequences.** It has a parametrized test matrix (`tests/test_runner.py`). **Any change to deletion logic must go through this function and its tests.**

## ADR-005 · "Complete export" means everything that exists was saved (2026-10-11)
**Decision.**
- A source in error/processing state (no text exists) is a *warning*.
- A source or artifact that exists but failed to download is a *problem*. It raises `IncompleteExport`; after one retry the notebook is archived but kept (`complete=0`).
- Disabling media download also marks the notebook incomplete, so it is never deleted without its audio/video.

## ADR-006 · Local web UI + SSE, vanilla JS (2026-10-11)
**Decision.**
- FastAPI serves a static dashboard.
- Live updates use Server-Sent Events (status snapshot every 0.5 s, plus notebook/event pushes).
- No frontend build step.
- Bound to `127.0.0.1` only, because the UI can delete notebooks.

## ADR-007 · Own login helper instead of `notebooklm login` (2026-10-11)
**Context.** NotebookLM (now also served at `notebook.google.com`) shows a **public landing page** to signed-out visitors. notebooklm-py 0.8.4's `login` treats "page is on the NotebookLM host" as "already logged in". It therefore saved an empty session (only `NID`, `__Host-GAPS`) within 2 seconds, even with `--fresh`.

**Decision.** `scripts/nlm_login.py`:
- opens `accounts.google.com/ServiceLogin?continue=…notebooklm…` in a Playwright persistent Chrome profile;
- waits until the Google session cookies `SID` and `__Secure-1PSIDTS` exist;
- then navigates to NotebookLM itself and writes `storage_state.json` where notebooklm-py expects it.

`login.ps1` then validates with `notebooklm auth check --test`.

**Also.**
- Browser = the Windows default browser (registry `UserChoice` ProgId), overridable with `--browser`.
- Playwright's `--enable-automation` flag is removed so Google allows the sign-in.

## ADR-008 · Windows launcher `nlm2drive.cmd` (2026-10-11)
**Context.** Running `./nlm2drive.sh` from PowerShell opens the "Select an app" dialog.

**Decision.** `nlm2drive.cmd` finds Git Bash (fallback WSL) and forwards the arguments. `.gitattributes` pins `*.sh` to LF and `*.cmd` to CRLF; batch labels break with LF.

## ADR-009 · Drive OAuth app audience: Internal for Workspace (2026-10-11)
**Context.** With the OAuth app in *Testing*, Google blocked sign-in: "Access blocked: … has not completed the Google verification process", Error 403. Testing-mode refresh tokens also expire after 7 days, which would break long runs.

**Decision.**
- Workspace accounts: **Make internal**.
- Personal accounts: **Publish → In production**, and accept the one-time "unverified app" screen.

**Scope.** `https://www.googleapis.com/auth/drive` is needed to find existing folders and verify them; `drive.file` cannot see folders it didn't create.
