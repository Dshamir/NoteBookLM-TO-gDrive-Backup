# Changelog

All notable changes to this project. Format: [Keep a Changelog](https://keepachangelog.com/), versioning: [SemVer](https://semver.org/).

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
