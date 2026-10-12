# Design decisions (ADR log)

Each entry: context â†’ decision â†’ consequences. Newest last.

## ADR-001 Â· Build on notebooklm-py instead of the MCP-client proposal (2026-10-11)
**Context.** The original idea was a Python MCP client that called a NotebookLM MCP server and pushed results to Drive with a **service account**. Research into the existing open-source options found:
- `teng-lin/notebooklm-py` (MIT): the most complete Python API, covering list, sources full text, notes, artifacts download, chat history and delete.
- Several MCP servers built on top of similar unofficial RPCs.
- No existing tool that does *archive â†’ verify â†’ delete*.

**Decision.** Use notebooklm-py directly, pinned at `0.8.4`, from our own FastAPI service.

**Why not the proposal.**
- Service accounts have **no Drive storage quota** (uploads to My Drive fail with 403).
- The MCP tool names in the proposal were guesses.
- It had no year filter, no verification and no delete step.
- Its export was thin (no studio media, no chat).

**Consequences.** We depend on undocumented Google endpoints, so the version is pinned and upgrades need testing (OPERATIONS.md).

## ADR-002 Â· Upload with the Drive API, not to `G:\` via a bind mount (2026-10-11)
**Context.** The plan was to write into `G:\My Drive\â€¦` (Drive for Desktop) through a Docker bind mount. A spike showed Docker Desktop **cannot see the Google Drive virtual drive**:
- the mount appeared as `/dev/sdf ext4`;
- writes landed inside the Docker VM and never reached `G:\`.

Verifying against that mount would have "passed" and then deleted notebooks whose backup didn't exist.

**Decision.** Upload with the Drive API (OAuth Desktop client, user's own quota) into `My Drive/<drive_archive_path>`. Drive for Desktop then shows the files at `G:\My Drive\â€¦`.

**Consequences.**
- Verification is authoritative: Drive's server-side MD5 is compared with our manifest.
- It needs a one-time Google Cloud OAuth client.
- Staging happens on the E: bind mount (`./data/staging`), which does work (9p drvfs).

## ADR-003 Â· Per-notebook pipeline with a persisted stage machine (2026-10-11)
**Decision.** Process one notebook at a time:
- `NEW â†’ EXPORTING â†’ EXPORTED â†’ UPLOADING â†’ UPLOADED â†’ CLOUD_VERIFIED â†’ DELETING â†’ DELETED`, persisted in SQLite;
- parallelism only inside a notebook (parallel file uploads, default 4).

**Why.**
- It bounds the blast radius.
- It makes crash recovery trivial.
- It respects rate limits (2 s pause between notebooks).
- Progress is easy to reason about.

## ADR-004 Â· Single deletion gate `may_delete()` (2026-10-11)
**Decision.** All deletion decisions go through `runner.may_delete(row, opts, settings)`. Deletion requires:
- `stage == CLOUD_VERIFIED`, `complete == 1`, owner, and a Drive folder id;
- run option `delete`, setting `delete_after_verify`, and the typed confirmation `delete_confirmed`.

The only exception is the e2e self-test notebook created by the app itself.

**Consequences.** It has a parametrized test matrix (`tests/test_runner.py`). **Any change to deletion logic must go through this function and its tests.**

## ADR-005 Â· "Complete export" means everything that exists was saved (2026-10-11)
**Decision.**
- A source in error/processing state (no text exists) is a *warning*.
- A source or artifact that exists but failed to download is a *problem*. It raises `IncompleteExport`; after one retry the notebook is archived but kept (`complete=0`).
- Disabling media download also marks the notebook incomplete, so it is never deleted without its audio/video.

## ADR-006 Â· Local web UI + SSE, vanilla JS (2026-10-11)
**Decision.**
- FastAPI serves a static dashboard.
- Live updates use Server-Sent Events (status snapshot every 0.5 s, plus notebook/event pushes).
- No frontend build step.
- Bound to `127.0.0.1` only, because the UI can delete notebooks.

## ADR-007 Â· Own login helper instead of `notebooklm login` (2026-10-11)
**Context.** NotebookLM (now also served at `notebook.google.com`) shows a **public landing page** to signed-out visitors. notebooklm-py 0.8.4's `login` treats "page is on the NotebookLM host" as "already logged in". It therefore saved an empty session (only `NID`, `__Host-GAPS`) within 2 seconds, even with `--fresh`.

**Decision.** `scripts/nlm_login.py`:
- opens `accounts.google.com/ServiceLogin?continue=â€¦notebooklmâ€¦` in a Playwright persistent Chrome profile;
- waits until the Google session cookies `SID` and `__Secure-1PSIDTS` exist;
- then navigates to NotebookLM itself and writes `storage_state.json` where notebooklm-py expects it.

`login.ps1` then validates with `notebooklm auth check --test`.

**Also.**
- Browser = the Windows default browser (registry `UserChoice` ProgId), overridable with `--browser`.
- Playwright's `--enable-automation` flag is removed so Google allows the sign-in.

## ADR-008 Â· Windows launcher `nlm2drive.cmd` (2026-10-11)
**Context.** Running `./nlm2drive.sh` from PowerShell opens the "Select an app" dialog.

**Decision.** `nlm2drive.cmd` finds Git Bash (fallback WSL) and forwards the arguments. `.gitattributes` pins `*.sh` to LF and `*.cmd` to CRLF; batch labels break with LF.

## ADR-009 Â· Drive OAuth app audience: Internal for Workspace (2026-10-11)
**Context.** With the OAuth app in *Testing*, Google blocked sign-in: "Access blocked: â€¦ has not completed the Google verification process", Error 403. Testing-mode refresh tokens also expire after 7 days, which would break long runs.

**Decision.**
- Workspace accounts: **Make internal**.
- Personal accounts: **Publish â†’ In production**, and accept the one-time "unverified app" screen.

**Scope.** `https://www.googleapis.com/auth/drive` is needed to find existing folders and verify them; `drive.file` cannot see folders it didn't create.


## ADR-010 Â· An expired Google session pauses the run; it is never a notebook failure (2026-10-12)
**Context.** ~2 h into the first big run (74 notebooks archived and deleted) the NotebookLM session was revoked server-side. Every call returned `RPCError â€¦ (unauthenticated)` and the token fetch landed on the public NotebookLM page (`CSRF token not found â€¦ Final URL: https://notebook.google.com/`). Evidence for a server-side revocation rather than a client bug: the stored `__Secure-1PSIDTS` still carried its login-time expiry (never rotated on disk), all required cookies were present, and `notebooklm auth check --test` failed only at "Token fetch". The exact trigger is Google's (session policy, device-bound cookies or revocation after heavy automated use). It cannot be prevented from here, so the app must survive it.

What went wrong in the app (the "stuck" UI):
1. Auth errors were treated like any other error: 3 attempts with 5 s / 20 s back-off per notebook, then the notebook was marked **FAILED** (4 notebooks, plus a failed delete counted on a fully verified one). After 5 of those the run auto-paused.
2. **Resume could not recover**: the run kept using the same dead client, so it just failed 5 more notebooks and paused again. Re-login on the host was never picked up.
3. The UI kept showing the last notebook as "in progress" with a spinner on step "metadata", and the message said "check the login" without how.
4. The exporter's tolerant `except` blocks would also have turned an auth error into an *incomplete export* (safe, since never deleted, but misleading and wasteful).
5. The UI's status/refresh calls opened a **second** NotebookLM client on the same cookie file while the run held one. Concurrent `RotateCookies` calls on one session are a plausible way to provoke revocation.

**Decision.**
- `is_auth_error()` classifies session failures (narrowly: not rate limits or network errors). The exporter re-raises them.
- An auth error raises `SessionExpired` out of the retry loop: the notebook is rolled back with no failure mark and no attempt count; the run enters `paused` + `needs_auth`, clears `current`, and waits.
- Recovery is automatic: re-probe on Re-check/Resume, when `storage_state.json` changes, and every 5 min. A probe is a fresh client plus one cheap `notebooks.list()`. The same notebook is then retried.
- A probe also runs before the first notebook (fail fast) and on every reconnect (incl. after the 5-failure auto-pause, which now rebuilds the client on Resume).
- During a run, `/api/auth/*` reports the runner's view and `/api/refresh` is refused (409): one NotebookLM client per cookie file.
- On startup, notebooks that were FAILED only because of auth return to the queue.

**Consequences.** An outage costs one wait, not a batch of false failures. Safety invariants are unchanged (deletion still only via `may_delete` after fresh MD5 verification; a notebook interrupted at DELETING is re-checked).

**Likely underlying cause (found after re-login).** The session that died had been saved without Google's rotation cookies `__Secure-1PSIDRTS` / `__Secure-3PSIDRTS`; the session saved on the next login (same browser profile, a few hours later) contained them. Google issues them a little *after* sign-in, and `scripts/nlm_login.py` saved the session the instant `SID` + `__Secure-1PSIDTS` existed. `nlm_login.py` now waits (up to 60 s) for the RTS cookies and warns if they never appear. Whether this fully prevents revocation is unproven: watch the next long run.

**Not done (options).** A durable fix for the *cause* needs a longer-lived credential: `notebooklm login --master-token` (layer-4 re-mint from a stored master token; needs the `[headless]` extra and an account-equivalent secret in `data/`) or signing in with Firefox cookies. Both have security trade-offs, so they are left as a deliberate user decision.

## ADR-011 · "Reconnect NotebookLM" button via a host-side helper (not OAuth) (2026-10-12)
**Context.** After ADR-010 the run survives a revoked session, but re-signing in still meant leaving the browser and typing `.\nlm2drive login`. The natural ask: a button, "via OAuth". NotebookLM has **no OAuth and no public API**; the only credential is the Google *browser* session (cookies). OAuth exists only for Drive, and it already works (and does not expire: Audience = Internal). The sign-in needs a real, visible Chrome window on the Windows desktop, which a Docker container cannot open.

**Decision.**
- Add an optional, tiny host-side service, `scripts/reconnect_helper.py` (stdlib only, run through `uv` Python 3.12 like the login), started/stopped by `.\nlm2drive up|down` (or `helper start|stop`, and `helper install` for a Windows logon task). It does exactly one thing: run `scripts/login.ps1` on request.
- The UI button calls the app (`POST /api/reconnect`), the app calls the helper at `host.docker.internal:8766` with a bearer token read from `/data/helper.json`. The browser never talks to the helper and never sees the token (no CORS surface at all).
- Safety rails: loopback bind; token; reject any `Origin` header and any foreign `Host` header; single-flight; 15 min limit; success = exit 0 **and** a cookie file written after the start; the app refuses cross-site origins and refuses to start while a run holds a *working* session (it would rewrite the cookie file under the run).
- After success the app clears its auth cache and nudges the runner, which probes immediately and carries on with the same notebook (ADR-010 does the rest). The cookie file is now written atomically so the run's poll can never read a half-written file.
- `login.ps1` retries once with a clean browser profile when the saved cookies fail the live check: after an expiry the persistent profile still holds the revoked cookies, which satisfy the "signed in" test locally.
- No helper = no regression: the button answers with `.\nlm2drive helper start` / `.\nlm2drive login`.

**Alternatives rejected.** (1) Run Chrome inside the container with noVNC: heavy, and Google flags such sign-ins. (2) Let the browser call a localhost helper directly: needs CORS + CSRF/DNS-rebinding handling on a service that launches processes. (3) Passing the host's Chrome profile cookies into the container: more secret handling for no gain. (4) A true "no-touch" renewal (master token, `notebooklm auth refresh`): still a user decision, see ADR-010.

**Consequences.** One click (plus the Google sign-in itself) recovers a paused run. The helper is another local process, bound to loopback and token-protected, with the same trust level as the user's own shell. It can only start the login script, nothing else.
