# Changelog

All notable changes to this project. Format: [Keep a Changelog](https://keepachangelog.com/), versioning: [SemVer](https://semver.org/).

## [Unreleased]
### Added
- Quality gates, all run in the container: `.\nlm2drive lint` (ruff: pyflakes, pycodestyle, isort, bugbear, pyupgrade, async, bandit-subset), `typecheck` (mypy), `smoke` (read-only live checks, `scripts/smoke.py`) and `check` (everything). Config in `pyproject.toml`; tools in `requirements-dev.txt`.
### Changed
- Lint/type clean-up: exception chaining on API errors, blocking directory ops in the exporter moved off the event loop, md5 marked as a non-security checksum, `typing` fixes.
- Safety: `Runner._finish` and `_process_with_retries` now refuse to continue (nothing is deleted) if a notebook row has vanished from the state DB.

## [1.0.1] - 2026-10-12
### Fixed
- **Run stuck at "metadata" after the NotebookLM session expired mid-run** (ADR-010):
  - an expired/revoked Google session is now recognised (`is_auth_error`) and pauses the run instead of failing notebooks;
  - no retries or FAILED marks for it; the interrupted notebook is rolled back and retried;
  - the run waits for a sign-in and continues by itself (session-file change, Re-check, Resume, or every 5 min);
  - Resume after the 5-failure auto-pause now rebuilds the NotebookLM connection (it reused the dead client before);
  - the exporter no longer swallows auth errors as "incomplete export";
  - notebooks wrongly FAILED by an auth outage are re-queued on startup.
- UI: no stale "in progress" card while paused/after a failure; red "Sign-in needed" pill and a banner with the exact steps; correct login hint (`.\nlm2drive login`).
- A second NotebookLM client is no longer opened during a run (status/refresh calls); `/api/refresh` returns 409 while running.
- `StateDB._x` returned a stale `lastrowid` for UPDATE/DELETE, so the "N notebooks interrupted" count was wrong.
- Login helper now waits for Google's `__Secure-*PSIDRTS` rotation cookies before saving the session (the expired session lacked them).
### Added
- Session probe before the first notebook (fail fast with a clear message) and on every reconnect.
- 15 regression tests (42 total) with a fake NotebookLM that can simulate a revoked session.
## [1.0.0] - 2026-10-11
### Added
- Dockerized FastAPI service with a local web UI:
  - selection by year, status and search; run controls; live progress over SSE;
  - activity log, run history, settings.
- Full notebook export:
  - metadata, summary, sources (full text, markdown), notes;
  - studio artifacts (audio, video, reports, slides, infographics, quizzes, flashcards, data tables, mind maps);
  - chat history, README and manifest.
- Google Drive upload via the Drive API (OAuth through the UI, resumable uploads, parallel files). Browsable folder layout `Archive/<year>/<YYYY-MM-DD Title [id8]>/`, plus `_catalog.md/csv`.
- Cloud verification: per-upload MD5 plus a full folder re-list against the manifest.
- Automatic deletion after verification, behind a single policy gate, a typed confirmation and per-run toggles.
- Crash recovery, retries, auto-pause after repeated failures, and re-verification before deleting previously archived notebooks.
- Orchestration: `nlm2drive.sh` (Git Bash/WSL) and the `nlm2drive.cmd` Windows launcher.
- Robust NotebookLM sign-in (`scripts/nlm_login.py`) using the Windows default browser.
- Tests: 27 pytest cases with a fake NotebookLM and an in-memory Drive.
- Docs: README, ARCHITECTURE, DECISIONS, OPERATIONS, TROUBLESHOOTING, AGENTS.
