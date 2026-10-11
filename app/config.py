"""Runtime configuration: environment paths + user settings persisted in /data/config.json."""
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
NOTEBOOKLM_HOME = Path(os.environ.get("NOTEBOOKLM_HOME", str(DATA_DIR / "notebooklm")))
STAGING_DIR = DATA_DIR / "staging"
GOOGLE_DIR = DATA_DIR / "google"
LOG_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "state.sqlite"
CONFIG_PATH = DATA_DIR / "config.json"
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:8765").rstrip("/")


@dataclass
class Settings:
    years: list[int] = field(default_factory=lambda: [2024, 2025])
    include_media: bool = True
    include_chat: bool = True
    delete_after_verify: bool = True
    # Archive folder path relative to "My Drive" → shows up as G:\My Drive\<path>
    drive_archive_path: str = "NotebookLM/Archive"
    upload_concurrency: int = 4
    inter_notebook_delay_sec: float = 2.0
    auth_refresh_hours: float = 6.0
    notebook_limit: int = 500
    keep_staging: bool = False
    # The first deleting run requires a typed confirmation in the UI.
    delete_confirmed: bool = False

    @classmethod
    def field_names(cls) -> set[str]:
        return {f.name for f in fields(cls)}


class SettingsStore:
    def __init__(self, path: Path = CONFIG_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._settings = self._load()

    def _load(self) -> Settings:
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                known = {k: v for k, v in raw.items() if k in Settings.field_names()}
                return Settings(**known)
            except Exception:  # corrupt file -> defaults
                pass
        return Settings()

    def get(self) -> Settings:
        with self._lock:
            return Settings(**asdict(self._settings))

    def update(self, **changes) -> Settings:
        with self._lock:
            data = asdict(self._settings)
            for k, v in changes.items():
                if k in Settings.field_names() and v is not None:
                    data[k] = v
            data["years"] = sorted({int(y) for y in data["years"]})
            self._settings = Settings(**data)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            return Settings(**data)


def ensure_dirs() -> None:
    for d in (DATA_DIR, STAGING_DIR, GOOGLE_DIR, LOG_DIR, NOTEBOOKLM_HOME):
        d.mkdir(parents=True, exist_ok=True)
