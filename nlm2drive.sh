#!/usr/bin/env bash
# nlm2drive.sh — orchestrates the NotebookLM → Google Drive archiver stack.
# Works from Git Bash or WSL on Windows (uses Docker Desktop's docker.exe when available).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# ---------------------------------------------------------------- helpers
c_ok=$'\e[32m'; c_warn=$'\e[33m'; c_err=$'\e[31m'; c_dim=$'\e[2m'; c_off=$'\e[0m'
say()  { printf '%s▸%s %s\n' "$c_ok" "$c_off" "$*"; }
warn() { printf '%s!%s %s\n' "$c_warn" "$c_off" "$*" >&2; }
die()  { printf '%s✗%s %s\n' "$c_err" "$c_off" "$*" >&2; exit 1; }

is_wsl()     { grep -qi microsoft /proc/version 2>/dev/null; }
is_gitbash() { [[ "$(uname -s)" == MINGW* || "$(uname -s)" == MSYS* ]]; }

DOCKER_DESKTOP_BIN="/c/Program Files/Docker/Docker/resources/bin"
DOCKER_DESKTOP_EXE="C:\\Program Files\\Docker\\Docker\\Docker Desktop.exe"
if is_wsl; then DOCKER_DESKTOP_BIN="/mnt/c/Program Files/Docker/Docker/resources/bin"; fi

pick_docker() {
  # Prefer Docker Desktop's Windows CLI (works from Git Bash and WSL without extra integration).
  if is_wsl || is_gitbash; then
    if [[ -x "$DOCKER_DESKTOP_BIN/docker.exe" ]]; then echo "$DOCKER_DESKTOP_BIN/docker.exe"; return; fi
    if command -v docker.exe >/dev/null 2>&1; then command -v docker.exe; return; fi
  fi
  command -v docker 2>/dev/null || die "Docker not found. Install Docker Desktop: https://www.docker.com/products/docker-desktop/"
}
DOCKER="$(pick_docker)"
export MSYS_NO_PATHCONV=1   # stop Git Bash from mangling container paths
dk()  { "$DOCKER" "$@"; }
dc()  { "$DOCKER" compose --project-directory "$(winpath "$ROOT")" -f "$(winpath "$ROOT/docker-compose.yml")" "$@"; }

winpath() {
  if is_wsl && [[ "$DOCKER" == *.exe ]]; then wslpath -w "$1"
  elif is_gitbash; then cygpath -w "$1"
  else echo "$1"; fi
}

run_powershell() {
  local exe="powershell.exe"
  command -v "$exe" >/dev/null 2>&1 || exe="/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
  "$exe" -NoProfile -ExecutionPolicy Bypass "$@"
}

load_env() {
  [[ -f .env ]] || cp .env.example .env
  set -a; # shellcheck disable=SC1091
  source <(sed -e 's/\r$//' .env | grep -E '^[A-Za-z_][A-Za-z0-9_]*=' ); set +a
  UI_PORT="${UI_PORT:-8765}"
  UI_URL="http://localhost:${UI_PORT}"
}

ensure_engine() {
  if dk version --format '{{.Server.Version}}' >/dev/null 2>&1; then return; fi
  say "Starting Docker Desktop…"
  # explorer.exe launches it detached from this shell (child processes of a closing shell can be killed).
  explorer.exe "$DOCKER_DESKTOP_EXE" >/dev/null 2>&1 || run_powershell -Command "Start-Process '$DOCKER_DESKTOP_EXE'" >/dev/null 2>&1 || true
  for _ in $(seq 1 60); do
    sleep 5
    if dk version --format '{{.Server.Version}}' >/dev/null 2>&1; then say "Docker engine is up."; return; fi
  done
  die "Docker engine did not start within 5 minutes. Open Docker Desktop manually and retry."
}

api() {  # api METHOD PATH [JSON]
  local method="$1" path="$2" body="${3:-}"
  if [[ -n "$body" ]]; then
    curl -fsS -X "$method" -H 'Content-Type: application/json' -d "$body" "${UI_URL}${path}"
  else
    curl -fsS -X "$method" "${UI_URL}${path}"
  fi
}

wait_healthy() {
  for _ in $(seq 1 40); do
    if curl -fsS "${UI_URL}/api/health" >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  return 1
}

open_url() {
  if is_wsl || is_gitbash; then run_powershell -Command "Start-Process '$1'" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null; then xdg-open "$1"
  elif command -v open >/dev/null; then open "$1"; fi
}

# ---------------------------------------------------------------- commands
cmd_setup() {
  load_env
  mkdir -p data/notebooklm data/google data/staging data/logs
  say "Docker CLI: $DOCKER"
  ensure_engine
  say "Building image…"
  dc build
  cat <<EOF

${c_ok}Setup complete.${c_off} Next steps:
  1. ./nlm2drive.sh login      # sign in to NotebookLM once (opens Chrome)
  2. ./nlm2drive.sh up         # start the stack and open ${UI_URL}
  3. In the UI: upload credentials.json, press Connect (Google Drive), Refresh list, Start.
     Archives land in My Drive/NotebookLM/Archive  (G:\\My Drive\\NotebookLM\\Archive)
EOF
}

cmd_login() {   # login [--browser chrome|msedge|chromium] [--fresh]
  load_env
  local browser="${LOGIN_BROWSER:-auto}" extra=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --browser) browser="$2"; shift ;;
      --fresh) extra+=(-Fresh) ;;
      *) die "unknown option $1 (use --browser chrome|msedge|chromium, --fresh)" ;;
    esac; shift
  done
  mkdir -p data/notebooklm
  say "Opening NotebookLM sign-in (browser: ${browser}). Sign in, wait until NotebookLM loads, then return here."
  run_powershell -File "$(winpath "$ROOT/scripts/login.ps1")" -DataDir "$(winpath "$ROOT/data/notebooklm")" \
    -Version "${NOTEBOOKLM_PY_VERSION:-}" -Browser "$browser" ${extra[@]+"${extra[@]}"}
  if curl -fsS "${UI_URL}/api/health" >/dev/null 2>&1; then api POST /api/auth/reload >/dev/null || true; fi
  say "Login stored in data/notebooklm."
}

cmd_up() {
  load_env; ensure_engine
  mkdir -p data/notebooklm data/google data/staging data/logs
  dc up -d --build
  if wait_healthy; then say "UI ready → ${UI_URL}"; open_url "$UI_URL"; else warn "UI not healthy yet — check: ./nlm2drive.sh logs"; fi
}

cmd_down()    { load_env; dc down; }
cmd_restart() { load_env; ensure_engine; dc restart; wait_healthy && say "Restarted → ${UI_URL}"; }
cmd_logs()    { load_env; dc logs -f --tail=200; }
cmd_open()    { load_env; open_url "$UI_URL"; }
cmd_test()    { load_env; ensure_engine; dc build; dc run --rm --no-deps nlm2drive pytest -q; }

cmd_status() {
  load_env
  dc ps
  if curl -fsS "${UI_URL}/api/health" >/dev/null 2>&1; then
    echo; api GET /api/summary | dk exec -i nlm2drive python -m json.tool 2>/dev/null || api GET /api/summary
  else
    warn "UI not reachable at ${UI_URL}"
  fi
}

cmd_run() {   # run [--dry-run] [--no-delete] [--limit N]
  load_env
  local dry=false delete=null limit=null
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run) dry=true ;;
      --no-delete) delete=false ;;
      --limit) limit="$2"; shift ;;
      *) die "unknown option $1" ;;
    esac; shift
  done
  wait_healthy || die "UI/API not running — ./nlm2drive.sh up"
  api POST /api/refresh >/dev/null
  api POST /api/run "{\"dry_run\": $dry, \"delete\": $delete, \"limit\": $limit}"
  echo; say "Run started — watch progress at ${UI_URL}"
}

cmd_refresh() { load_env; dc exec nlm2drive notebooklm auth refresh && api POST /api/auth/reload >/dev/null && say "NotebookLM session refreshed."; }

cmd_e2e() {    # self-test: throwaway notebook → archive → verify → delete
  load_env
  wait_healthy || die "UI/API not running — ./nlm2drive.sh up"
  api POST /api/test-e2e '{}'; echo; say "Self-test started — watch ${UI_URL}"
}

usage() {
  cat <<EOF
nlm2drive.sh — NotebookLM → Google Drive archiver

  setup            create folders/.env, start Docker, build image
  login [--browser chrome|msedge|chromium] [--fresh]
                   sign in to NotebookLM (default browser = Chrome; --fresh switches account)
  up | down        start / stop the stack (UI at http://localhost:\${UI_PORT:-8765})
  restart | logs   restart container / follow logs
  open             open the web UI
  status           container + archive summary
  run [--dry-run] [--no-delete] [--limit N]   start an archive run headlessly
  refresh          refresh the NotebookLM session cookies
  e2e              self-test with a throwaway notebook (archive → verify → delete)
  test             run the unit tests in the container
EOF
}

main() {
  local cmd="${1:-help}"; shift || true
  case "$cmd" in
    setup|login|up|down|restart|logs|open|status|run|refresh|e2e|test) "cmd_$cmd" "$@" ;;
    help|-h|--help) usage ;;
    *) usage; exit 1 ;;
  esac
}
main "$@"
