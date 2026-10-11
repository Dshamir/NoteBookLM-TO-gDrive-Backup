<p align="center">
  <img src="docs/assets/banner.png" alt="NoteBookLM → gDrive Backup: Archive · Verify · Free up space" width="100%">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white" alt="Python 3.12">
  <img src="https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/Uvicorn-499848?logo=gunicorn&logoColor=white" alt="Uvicorn">
  <img src="https://img.shields.io/badge/SQLite-003B57?logo=sqlite&logoColor=white" alt="SQLite">
  <img src="https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white" alt="Docker Compose">
  <img src="https://img.shields.io/badge/Google%20Drive-API%20v3-4285F4?logo=googledrive&logoColor=white" alt="Google Drive API v3">
  <img src="https://img.shields.io/badge/OAuth%202.0-Google-EB5424?logo=auth0&logoColor=white" alt="OAuth 2.0">
  <img src="https://img.shields.io/badge/notebooklm--py-0.8.4-8b5cf6" alt="notebooklm-py 0.8.4">
  <img src="https://img.shields.io/badge/Playwright-Chrome-2EAD33?logo=playwright&logoColor=white" alt="Playwright">
  <br>
  <img src="https://img.shields.io/badge/JavaScript-vanilla-F7DF1E?logo=javascript&logoColor=black" alt="Vanilla JavaScript">
  <img src="https://img.shields.io/badge/HTML5-E34F26?logo=html5&logoColor=white" alt="HTML5">
  <img src="https://img.shields.io/badge/CSS3-glassmorphism-1572B6?logo=css3&logoColor=white" alt="CSS3">
  <img src="https://img.shields.io/badge/Bash-orchestrator-4EAA25?logo=gnubash&logoColor=white" alt="Bash">
  <img src="https://img.shields.io/badge/PowerShell-5.1-5391FE?logo=powershell&logoColor=white" alt="PowerShell">
  <img src="https://img.shields.io/badge/uv-astral-DE5FE9?logo=astral&logoColor=white" alt="uv">
  <img src="https://img.shields.io/badge/pytest-27%20passing-0A9EDC?logo=pytest&logoColor=white" alt="pytest 27 passing">
  <img src="https://img.shields.io/badge/platform-Windows%2011-0078D4?logo=windows11&logoColor=white" alt="Windows 11">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT license">
  <img src="https://img.shields.io/badge/version-1.0.0-blueviolet" alt="version 1.0.0">
</p>

# NoteBookLM-TO-gDrive Backup (`nlm2drive`)

Back up NotebookLM notebooks to **Google Drive as browsable folders**. Every file is **verified in the cloud (MD5)**. Only then is the notebook **deleted from NotebookLM**, which frees space under the 500-notebook limit.

Runs locally in Docker with a small web UI for selecting notebooks and watching progress. It is orchestrated by one script (`nlm2drive.sh`, or `nlm2drive.cmd` from PowerShell).

> **Status:** v1.0.0, working end to end on Windows 11 + Docker Desktop + Google Workspace.
> It is built on the unofficial [notebooklm-py](https://github.com/teng-lin/notebooklm-py) library (MIT, pinned at 0.8.4).

---

## What you get

```
My Drive/NotebookLM/Archive/            (= G:\My Drive\NotebookLM\Archive with Drive for Desktop)
├── _catalog.md / _catalog.csv          index of every archived notebook (status, size, link)
├── 2024/
│   └── 2024-09-27 My research [3c775e3c]/
│       ├── README.md                   summary, source table, links to everything below
│       ├── metadata.json               notebook metadata + export stats
│       ├── manifest.json               sha256 + md5 + size of every file (used for verification)
│       ├── sources/01 - <title>.md …   full extracted text of each source (+ index.json with URLs/Drive IDs)
│       ├── notes/*.md                  saved notes
│       ├── studio/audio/*.m4a  video/*.mp4  reports/*.md  slides/*.pdf  infographics/*.png
│       │        quizzes/  flashcards/ (json+md)  data-tables/*.csv  mind-maps/*.json
│       └── chat/history.md + history.json
└── 2025/…
```

## Requirements
- Windows 10/11 with **Docker Desktop**.
- **Git for Windows** (provides Git Bash; `nlm2drive.cmd` uses it) or WSL.
- **Google Chrome** (or Edge) for the one-time NotebookLM sign-in. [`uv`](https://docs.astral.sh/uv/) is installed automatically.
- A Google Cloud **OAuth "Desktop app" client** for Drive. It's free and takes about 5 minutes (see below).

## Quick start
From **PowerShell / cmd** use `.\nlm2drive <command>`. From Git Bash/WSL use `./nlm2drive.sh <command>`.

```powershell
.\nlm2drive setup     # starts Docker Desktop if needed, builds the image
.\nlm2drive login     # opens Chrome on Google sign-in → session saved to data/notebooklm
.\nlm2drive up        # starts the stack and opens http://localhost:8765
```

Then, in the web UI:
1. **Google Drive** card: upload `credentials.json`, then press **Connect**.
2. Press **Refresh list**. All owned 2024 + 2025 notebooks are pre-selected (years are configurable in Settings).
3. **Dry run** to preview, then do a trial **archive-only** run: switch deletion off and set Limit = 1.
4. Type **`DELETE AFTER BACKUP`** in the red banner, set Limit = 3, then **Archive & delete**. Check the results, clear Limit, and run again.

## Google Drive OAuth client (one time)
1. [Create a project](https://console.cloud.google.com/projectcreate) and enable the [Google Drive API](https://console.cloud.google.com/apis/library/drive.googleapis.com).
2. Under [Audience](https://console.cloud.google.com/auth/audience):
   - With a **Google Workspace** account, choose **Make internal**. There's no verification and tokens don't expire.
   - Otherwise press **Publish app → In production**.
   - Leaving it in *Testing* causes `Error 403: access_denied`, and tokens expire after 7 days.
3. Under [Clients](https://console.cloud.google.com/auth/clients), choose **Create client → Desktop app → Download JSON**.
4. Upload the JSON in the UI and press **Connect**. For an External app, Google shows "Google hasn't verified this app"; choose **Advanced → Go to … (unsafe)**. It's your own app.

## Commands
| Command | Purpose |
|---|---|
| `setup` | create `data/` folders, start Docker, build the image |
| `login [--browser chrome\|msedge\|chromium] [--fresh]` | NotebookLM sign-in. Defaults to your Windows default browser. `--fresh` wipes the sign-in browser profile (to switch accounts) |
| `up` / `down` / `restart` / `logs` / `open` / `status` | manage the stack |
| `run [--dry-run] [--no-delete] [--limit N]` | start a run headlessly; watch it in the UI |
| `refresh` | refresh the NotebookLM session cookies |
| `e2e` | self-test with a throwaway notebook. It needs a free notebook slot: Google refuses to create one when you're near the limit |
| `test` | run the unit tests in the container |

## Safety guarantees
- A notebook is deleted **only** if every one of these holds:
  1. its Drive folder was just re-listed and **every file's MD5 and size match** the local manifest;
  2. the export was **complete** (any source or studio item that exists but failed to download means it is archived but **kept**);
  3. deletion is enabled for the run, **and** the one-time typed confirmation was given.
- **Shared** notebooks (where you aren't the owner) are never touched.
- A notebook archived in an earlier run is re-verified in Drive before deletion. It is re-archived if its source count changed.
- Interrupted runs resume safely. Half-done notebooks restart, and an interrupted delete is re-checked.
- After 5 failures in a row the run auto-pauses (usually an expired session).
- Only read, download and delete endpoints are called. No AI generation, so no NotebookLM quota is used.
- The UI is bound to `127.0.0.1` only. Secrets live in `data/`, which is git-ignored.

## Documentation
| Doc | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | components, data flow, stage machine, API, data layout |
| [docs/DECISIONS.md](docs/DECISIONS.md) | design decisions and why (ADR log) |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | runbook: daily use, re-login, upgrades, reset, backup |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | every problem hit so far and its fix |
| [AGENTS.md](AGENTS.md) | continuity notes for future maintainers and AI coding agents |
| [CHANGELOG.md](CHANGELOG.md) | release history |

## Limits
- NotebookLM does not expose the original uploaded files (PDFs etc.). Their **full extracted text** is saved instead. URL, YouTube and Drive sources keep their link or ID.
- notebooklm-py uses undocumented Google endpoints. If Google changes them, update the pin (see [OPERATIONS](docs/OPERATIONS.md#upgrading-notebooklm-py)).

## License
[MIT](LICENSE). Not affiliated with Google.
