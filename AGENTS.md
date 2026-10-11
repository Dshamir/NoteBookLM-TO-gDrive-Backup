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

## How to work on it
```powershell
.\nlm2drive test      # build image + pytest in container (fake NotebookLM + in-memory Drive)
.\nlm2drive up        # rebuild + restart (static files are baked into the image)
.\nlm2drive logs
```
- The host Python is 3.8 (too old). Run everything in the container, or use `uv`/`uvx` with `--python 3.12`.
- Docker CLI: `C:\Program Files\Docker\Docker\resources\bin\docker.exe` (not on PATH by default).
- In PowerShell, native stderr shows up as red `NativeCommandError` text even on success; check the real exit status.
- Line endings: `*.sh` LF, `*.cmd`/`*.ps1` CRLF (`.gitattributes`).

## notebooklm-py 0.8.4 API facts (verified by introspection)
- `NotebookLMClient.from_storage(keepalive=900)` is an async context manager. Storage is `$NOTEBOOKLM_HOME/profiles/default/storage_state.json`.
- Notebook fields: `id, title, emoji, created_at, modified_at, last_viewed_at, sources_count, is_owner, role`.
- `notebooks.list/get/get_or_none/get_summary/create/delete`; `sources.list/get_fulltext(output_format='markdown'|'text')/add_text(..., wait=True)`; `notes.list/create`; `mind_maps.list/get_tree`; `chat.get_history(nb, limit)` returns `[(q, a)]`.
- `artifacts.list`; `artifacts.download_{audio,video,report,slide_deck,infographic,quiz,flashcards,data_table,mind_map}(nb_id, path, artifact_id, **kw)`. Quiz/flashcards take `output_format='json'|'markdown'`; slides take `output_format='pdf'`.
- `notebooklm login` is unreliable (ADR-007). Use `scripts/nlm_login.py`.
- NotebookLM is also served at `notebook.google.com`; cookies exist for both hosts.

## Current state (2026-10-11)
- v1.0.0 working end to end.
  - Login: Chrome, Workspace account, 498 notebooks.
  - Drive connected (Audience = Internal).
  - Archive-only trial: 1 notebook, 11 files, 15.5 MB, verified in about 30 s.
- Account mix at that time:
  - 2024: 4 notebooks, 2025: 81, 2026: 413 (1 shared).
  - Archiving 2024 + 2025 frees only 85 slots.

## Open ideas / backlog
- "Created before <date>" cutoff selector in the UI (to include early-2026 notebooks).
- Optional scheduled runs (e.g. monthly: archive notebooks older than N months).
- Upstream: report the `login` landing-page false positive to notebooklm-py.
- Optionally save original-file metadata for uploaded PDFs (the originals themselves are not retrievable from NotebookLM).
