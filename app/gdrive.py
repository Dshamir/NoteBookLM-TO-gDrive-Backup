"""Google Drive API — OAuth via the web UI, folder management, resumable uploads and MD5 verification.

The archive is uploaded straight into My Drive (default: NotebookLM/Archive). Google Drive for
Desktop then shows it at G:\\My Drive\\NotebookLM\\Archive. Every upload returns an MD5 that is
compared with the local manifest, and the whole notebook folder is re-listed and compared again
before a notebook may be deleted from NotebookLM.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .config import GOOGLE_DIR, PUBLIC_URL

SCOPES = ["https://www.googleapis.com/auth/drive"]
CLIENT_FILE = GOOGLE_DIR / "credentials.json"
TOKEN_FILE = GOOGLE_DIR / "token.json"
REDIRECT_URI = f"{PUBLIC_URL}/api/drive/callback"
FOLDER_MIME = "application/vnd.google-apps.folder"
RESUMABLE_THRESHOLD = 5 * 1024 * 1024
CHUNK = 16 * 1024 * 1024
RETRIES = 6

MIME_BY_EXT = {
    ".md": "text/markdown", ".json": "application/json", ".csv": "text/csv", ".txt": "text/plain",
    ".html": "text/html", ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
    ".m4a": "audio/mp4", ".mp4": "video/mp4", ".mp3": "audio/mpeg", ".wav": "audio/wav",
}


class DriveError(RuntimeError):
    pass


class DriveClient:
    def __init__(self):
        self._flows: dict = {}
        self._lock = threading.Lock()
        self._local = threading.local()
        self._email: str | None = None
        self._folder_cache: dict[tuple[str, str], str] = {}

    # ------------------------------------------------------------------ OAuth
    def has_client(self) -> bool:
        return CLIENT_FILE.exists()

    def save_client(self, raw: bytes) -> None:
        data = json.loads(raw)
        if "installed" not in data and "web" not in data:
            raise ValueError("Not an OAuth client JSON (expected an 'installed' or 'web' key).")
        GOOGLE_DIR.mkdir(parents=True, exist_ok=True)
        CLIENT_FILE.write_bytes(raw)

    def auth_url(self) -> str:
        from google_auth_oauthlib.flow import Flow
        if not self.has_client():
            raise DriveError("Upload credentials.json (OAuth Desktop client) first.")
        flow = Flow.from_client_secrets_file(str(CLIENT_FILE), scopes=SCOPES, redirect_uri=REDIRECT_URI)
        state = secrets.token_urlsafe(16)
        url, _ = flow.authorization_url(access_type="offline", prompt="consent",
                                        include_granted_scopes="true", state=state)
        with self._lock:
            self._flows[state] = flow
        return url

    def finish(self, state: str, code: str) -> None:
        with self._lock:
            flow = self._flows.pop(state, None)
        if flow is None:
            raise DriveError("Unknown or expired OAuth state — click Connect again.")
        flow.fetch_token(code=code)
        TOKEN_FILE.write_text(flow.credentials.to_json(), encoding="utf-8")
        self._reset()

    def disconnect(self) -> None:
        if TOKEN_FILE.exists():
            TOKEN_FILE.unlink()
        self._reset()

    def _reset(self) -> None:
        self._local = threading.local()
        self._email = None
        self._folder_cache.clear()

    def _creds(self):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        if not TOKEN_FILE.exists():
            return None
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
        if not creds.valid and creds.refresh_token:
            with self._lock:
                creds.refresh(Request())
                TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
        return creds if creds.valid else None

    def service(self):
        """One googleapiclient service per thread (httplib2 is not thread-safe)."""
        svc = getattr(self._local, "svc", None)
        if svc is None:
            from googleapiclient.discovery import build
            creds = self._creds()
            if creds is None:
                raise DriveError("Google Drive is not connected.")
            svc = build("drive", "v3", credentials=creds, cache_discovery=False)
            self._local.svc = svc
        return svc

    def connected(self) -> bool:
        return TOKEN_FILE.exists()

    def status(self) -> dict:
        st = {"has_client": self.has_client(), "connected": False, "email": None, "error": None,
              "scope": "drive"}
        if not TOKEN_FILE.exists():
            return st
        try:
            if self._email is None:
                about = self.service().about().get(fields="user(emailAddress)").execute(num_retries=2)
                self._email = about["user"]["emailAddress"]
            st.update(connected=True, email=self._email)
        except Exception as e:  # expired/revoked token etc.
            self._local = threading.local()
            st["error"] = str(e)[:300]
        return st

    # ---------------------------------------------------------------- folders
    @staticmethod
    def _esc(name: str) -> str:
        return name.replace("\\", "\\\\").replace("'", "\\'")

    def find_child(self, parent_id: str, name: str, folder: bool | None = None) -> dict | None:
        q = f"name = '{self._esc(name)}' and '{parent_id}' in parents and trashed = false"
        if folder is True:
            q += f" and mimeType = '{FOLDER_MIME}'"
        elif folder is False:
            q += f" and mimeType != '{FOLDER_MIME}'"
        res = self.service().files().list(q=q, fields="files(id,name,mimeType,md5Checksum,size)",
                                          pageSize=10, spaces="drive").execute(num_retries=RETRIES)
        files = res.get("files", [])
        return files[0] if files else None

    def ensure_folder(self, parent_id: str, name: str) -> str:
        key = (parent_id, name)
        if key in self._folder_cache:
            return self._folder_cache[key]
        with self._lock:  # avoid duplicate folder creation races
            if key in self._folder_cache:
                return self._folder_cache[key]
            found = self.find_child(parent_id, name, folder=True)
            if found:
                fid = found["id"]
            else:
                fid = self.service().files().create(
                    body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
                    fields="id").execute(num_retries=RETRIES)["id"]
            self._folder_cache[key] = fid
            return fid

    def ensure_path(self, parts: list[str], parent_id: str = "root") -> str:
        current = parent_id
        for part in [p for p in parts if p]:
            current = self.ensure_folder(current, part)
        return current

    def trash_child_folder(self, parent_id: str, name: str) -> bool:
        """Move a (partial) previous upload of a notebook folder to the Drive trash."""
        found = self.find_child(parent_id, name, folder=True)
        if not found:
            return False
        self.service().files().update(fileId=found["id"], body={"trashed": True}).execute(num_retries=RETRIES)
        self._folder_cache.pop((parent_id, name), None)
        return True

    # ---------------------------------------------------------------- uploads
    def upload_file(self, local: Path, parent_id: str, name: str | None = None,
                    progress: Callable[[int], None] | None = None) -> dict:
        from googleapiclient.http import MediaFileUpload
        name = name or local.name
        size = local.stat().st_size
        mime = MIME_BY_EXT.get(local.suffix.lower(), "application/octet-stream")
        resumable = size > RESUMABLE_THRESHOLD
        media = MediaFileUpload(str(local), mimetype=mime, resumable=resumable,
                                chunksize=CHUNK if resumable else -1)
        req = self.service().files().create(body={"name": name, "parents": [parent_id]},
                                            media_body=media, fields="id,name,md5Checksum,size")
        if not resumable:
            res = req.execute(num_retries=RETRIES)
            if progress:
                progress(size)
            return res
        res, sent = None, 0
        while res is None:
            status, res = req.next_chunk(num_retries=RETRIES)
            if status and progress:
                now = int(status.resumable_progress)
                progress(now - sent)
                sent = now
        if progress and size - sent > 0:
            progress(size - sent)
        return res

    def put_text(self, parent_id: str, name: str, text: str, mime: str = "text/markdown") -> str:
        """Create or overwrite a small text file (catalog)."""
        from googleapiclient.http import MediaInMemoryUpload
        media = MediaInMemoryUpload(text.encode("utf-8"), mimetype=mime, resumable=False)
        existing = self.find_child(parent_id, name, folder=False)
        if existing:
            self.service().files().update(fileId=existing["id"], media_body=media).execute(num_retries=RETRIES)
            return existing["id"]
        return self.service().files().create(body={"name": name, "parents": [parent_id]},
                                             media_body=media, fields="id").execute(num_retries=RETRIES)["id"]

    # ----------------------------------------------------------- verification
    def list_tree(self, folder_id: str, prefix: str = "") -> dict[str, dict]:
        out: dict[str, dict] = {}
        token = None
        while True:
            res = self.service().files().list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields="nextPageToken, files(id,name,mimeType,md5Checksum,size)",
                pageSize=1000, pageToken=token, spaces="drive").execute(num_retries=RETRIES)
            for f in res.get("files", []):
                rel = f"{prefix}{f['name']}"
                if f["mimeType"] == FOLDER_MIME:
                    out.update(self.list_tree(f["id"], rel + "/"))
                else:
                    out[rel] = {"id": f["id"], "md5": f.get("md5Checksum"), "size": int(f.get("size", 0) or 0)}
            token = res.get("nextPageToken")
            if not token:
                return out

    def verify_folder(self, folder_id: str, expected: dict[str, dict]) -> dict:
        """Compare a notebook folder in Drive against the manifest (md5 + size), file by file."""
        cloud = self.list_tree(folder_id)
        missing = [r for r in expected if r not in cloud]
        mismatched = [r for r, m in expected.items()
                      if r in cloud and (cloud[r]["md5"] != m["md5"] or cloud[r]["size"] != m["size"])]
        extra = [r for r in cloud if r not in expected]
        ok = not missing and not mismatched
        return {"ok": ok, "total": len(expected), "found": len(expected) - len(missing),
                "missing": missing[:20], "mismatched": mismatched[:20], "extra": extra[:20],
                "detail": f"all {len(expected)} files verified in Drive (md5)" if ok else
                f"{len(missing)} missing, {len(mismatched)} mismatched"}


def with_backoff(fn, attempts: int = 4, base: float = 2.0):
    """Retry wrapper for whole-operation failures not covered by googleapiclient num_retries."""
    for i in range(attempts):
        try:
            return fn()
        except Exception:
            if i == attempts - 1:
                raise
            time.sleep(base * (2 ** i))
