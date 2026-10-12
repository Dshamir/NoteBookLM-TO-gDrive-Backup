# Operations runbook

All commands are run from the project folder. Use `.\nlm2drive <cmd>` in PowerShell or `./nlm2drive.sh <cmd>` in Git Bash.

## Daily / periodic use
1. `.\nlm2drive up`. It starts Docker Desktop if needed and opens http://localhost:8765.
2. Press **Refresh list**.
3. Adjust the selection: year chips, search, checkboxes, or **Select all 2024 + 2025**.
4. Press **Archive & delete**, or turn deletion off for **Archive only**.
5. Watch progress in the UI. **Pause** finishes the current step and waits. **Stop** ends the run; half-done notebooks restart next time.
6. When finished, browse `G:\My Drive\NotebookLM\Archive\_catalog.md`.

Headless alternative: `.\nlm2drive run [--dry-run] [--no-delete] [--limit N]`.

## First-run checklist
- [ ] `.\nlm2drive setup`
- [ ] `.\nlm2drive login`. The auth check must show **Authentication is valid** and a notebook count.
- [ ] OAuth client created. Audience = **Internal** (Workspace) or **In production**.
- [ ] UI: upload `credentials.json` → **Connect**. The Drive card shows your email.
- [ ] **Refresh list** → **Dry run**.
- [ ] Archive-only trial with `ids` / Limit 1, then open the folder on `G:`.
- [ ] Type `DELETE AFTER BACKUP` → Limit 3 → **Archive & delete** → check NotebookLM and Drive.
- [ ] Full run.

## NotebookLM session expired
Symptoms: red **Sign-in needed** pill + banner, the run is paused (no notebook is marked failed), or the NotebookLM card is red.

**One click:** press **Reconnect NotebookLM** and sign in to Google in the Chrome window that opens. The run continues by itself with the same notebook.

**From the shell** (always works, no helper needed):
```powershell
.\nlm2drive login          # reuses the saved browser profile; if the saved cookies are dead it retries once with a clean profile
.\nlm2drive login --fresh  # switch Google account / profile is broken
```
`login` calls `/api/auth/reload` automatically when the stack is up, and a run waiting for a sign-in continues on its own.

### Reconnect helper (powers the button)
```powershell
.\nlm2drive helper status      # running / not running
.\nlm2drive helper start       # (`up` already does this)
.\nlm2drive helper install     # start it at every Windows logon (Scheduled Task, current user, no admin)
.\nlm2drive helper uninstall   # remove that task
.\nlm2drive helper stop        # (`down` already does this)
```
It listens on `127.0.0.1:8766` only, requires a per-start token (`data\helper.json`), and can do exactly one thing: run `scripts\login.ps1`. Logs: `data\logs\helper.log`, `helper.err.log`. Port busy? Edit `-Port` in `scripts/helper.ps1` calls (default 8766).

## Drive token problems
- Drive card shows an error → **Disconnect** → **Connect**.
- `invalid_grant` after about 7 days → the OAuth app is still in *Testing*. Set Audience to Internal or In production, then reconnect.
- New OAuth client → upload the new `credentials.json` → **Connect**.

## Changing the archive location
UI → **Settings** → *Drive archive folder* (a path inside My Drive, e.g. `Backups/NotebookLM`). Notebooks that are already archived stay where they are; their status in the DB is unchanged.

## Retrying failures
Failed notebooks show a ↻ button: press it, or just start another run. FAILED notebooks stay selected and are retried. The error text is shown under the title and in the activity log.

## Upgrading notebooklm-py
1. Read the [releases](https://github.com/teng-lin/notebooklm-py/releases).
2. Bump the version in `requirements.txt` **and** `NOTEBOOKLM_PY_VERSION` in `.env` / `.env.example`.
3. `.\nlm2drive check` (lint + typecheck + tests), then `.\nlm2drive up`, then `.\nlm2drive smoke`.
4. Archive-only trial on one notebook. Check the README, sources and studio files on Drive.
5. If `notebooklm login` gets fixed upstream (see ADR-007), `scripts/nlm_login.py` can stay. It only uses Playwright.

## Resetting state
- **Forget run history / stages** (Drive is not touched): `.\nlm2drive down`, delete `data/state.sqlite*`, `.\nlm2drive up`, **Refresh list**. Archived-but-not-deleted notebooks will be archived again into a fresh folder; the old one is trashed.
- **Re-arm the deletion confirmation:** Settings → *Revoke confirmation*.
- **Full reset:** `.\nlm2drive down` and delete `data/`. You must log in and connect Drive again.

## Backing up the tool itself
`data/state.sqlite` holds the history of what was deleted and where it was archived. The same information is in `_catalog.md/csv` on Drive. Don't commit `data/`: it contains cookies and OAuth tokens.

## Ports / config
- `.env`: `UI_PORT` (default 8765), `TZ`, `NOTEBOOKLM_PY_VERSION`. If you change `UI_PORT`, the OAuth redirect URI changes too (`PUBLIC_URL`). Desktop clients accept any localhost port.
- UI settings (persisted in `data/config.json`): years, include media and chat, delete after verify, archive path, parallel uploads (1–8), pause between notebooks, keep staging.
