# Troubleshooting

These are problems actually encountered while building and running this project, with their fixes.

| Symptom | Cause | Fix |
|---|---|---|
| Running `./nlm2drive.sh` in PowerShell opens **"Select an app to open this .sh file"** | Windows has no handler for `.sh` | Use `.\nlm2drive <cmd>` (the `nlm2drive.cmd` launcher runs it through Git Bash) |
| Login prints **"Already logged in."** within seconds, then `auth check` shows âœ— `SID`, âœ— `__Secure-1PSIDTS`, cookies only `NID`, `__Host-GAPS` | notebooklm-py 0.8.4 mistakes NotebookLM's public landing page for a signed-in session (ADR-007) | Fixed by `scripts/nlm_login.py`. If it ever recurs: `.\nlm2drive login --fresh` and complete the Google sign-in |
| Login window waits forever after you signed in | The old helper waited for the tab URL to be on `notebooklm.google.com`; NotebookLM now also uses `notebook.google.com` | Fixed: the helper now waits for Google cookies only, then navigates itself |
| **"Access blocked: <app> has not completed the Google verification process"**, Error 403 `access_denied` | OAuth app in *Testing* and your account isn't a test user | [Audience](https://console.cloud.google.com/auth/audience) â†’ **Make internal** (Workspace) or **Publish app** â†’ press Connect again |
| Drive works for a week, then `invalid_grant` | Testing-mode refresh tokens expire after 7 days | Same as above, then reconnect |
| Self-test fails: `RPCError: The server rejected this request (resource exhausted)` | Google refuses to create a new notebook when you're at or near the 500 limit (or daily create quota) | Skip the self-test; do an archive-only trial on one real notebook instead. The self-test works once there is free space |
| Files "written" to `G:\My Drive` from Docker never appear | Docker Desktop can't bind-mount the Google Drive virtual drive (ADR-002) | Not used anymore: uploads go through the Drive API |
| Docker commands fail: engine not running | Docker Desktop not started | `.\nlm2drive up` starts it (`explorer.exe "Docker Desktop.exe"`) and waits up to 5 min. If it doesn't come up, start Docker Desktop manually |
| `docker` not found in PowerShell | Docker Desktop's CLI isn't on PATH | The scripts use `C:\Program Files\Docker\Docker\resources\bin\docker.exe` directly. In your own shell, prepend that folder to `$env:Path` |
| `status` prints "Python was not foundâ€¦" | The Microsoft Store `python3` alias stub | Fixed: JSON is formatted with the container's Python |
| `gh` says "The token in GITHUB_TOKEN is invalid" although you're logged in | A stale `GITHUB_TOKEN` env var overrides the keyring login | `Remove-Item Env:GITHUB_TOKEN` for the session, or delete the variable from Windows environment settings |
| Notebook shows **"In Drive Â· kept"** | Export incomplete: some content that exists couldn't be downloaded | See the activity log / `metadata.json` â†’ `stats.problems`. Retry later; if it persists, archive manually or accept keeping it |
| Notebook stuck in **FAILED** | 3 attempts failed (network, auth, Drive) | Read the error under the title, fix the cause, press â†» or start another run |
| Run auto-paused ("N notebooks failed in a row") | Rate limiting, a Drive outage or network trouble (an expired session no longer counts here: see the next row) | Read the activity log, fix the cause, press **Resume**. The NotebookLM connection is rebuilt on resume |
| Pill says **Sign-in needed**, red banner, run paused, no notebook marked failed (`unauthenticated` / `CSRF token not found … notebook.google.com` in the log) | Google revoked the NotebookLM session mid-run (ADR-010) | In PowerShell: `.\nlm2drive login` and sign in. The run notices the new session by itself (or press **Re-check now**) and continues with the same notebook. Nothing is lost; archived notebooks stay archived |
| Notebooks marked FAILED with `unauthenticated` after an outage (older versions) | Auth errors used to be counted as notebook failures | Fixed: they are re-queued automatically on the next container start (`.\nlm2drive restart`) |
| PowerShell output saved to a file looks garbled | Windows PowerShell 5.1 writes UTF-16 with `>` | Use `Out-File -Encoding utf8`, or view it in PowerShell |
| Activity log shows `Ã¢â‚¬â€` instead of `â€”` in PowerShell `Invoke-RestMethod` output | Console code page, not data corruption | Cosmetic; the UI renders correctly |

## Diagnostics
```powershell
.\nlm2drive status                     # container + summary JSON
.\nlm2drive logs                       # follow container logs
curl.exe -s http://localhost:8765/api/auth/status?force=true
curl.exe -s http://localhost:8765/api/drive/status?force=true
curl.exe -s "http://localhost:8765/api/events/history?limit=50"
```
Session details (run on the host, outside Docker):
```powershell
$env:NOTEBOOKLM_HOME = "$PWD\data\notebooklm"
uvx --python 3.12 --from "notebooklm-py[browser]==0.8.4" notebooklm auth check --test
```
