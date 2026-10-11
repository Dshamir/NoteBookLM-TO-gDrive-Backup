<#
  login.ps1 — one-time NotebookLM sign-in on the Windows host.
  Installs `uv` if missing, then runs `notebooklm login` (notebooklm-py) in an isolated
  Python 3.12. By default it uses your Windows *default browser* (Google Chrome → chrome,
  Microsoft Edge → msedge, anything else → bundled Chromium). Cookies are written to -DataDir,
  which is mounted into the container as /data/notebooklm.
#>
param(
  [Parameter(Mandatory = $true)][string]$DataDir,
  [string]$Version = "",
  [ValidateSet("auto", "chrome", "msedge", "chromium")][string]$Browser = "auto",
  [switch]$Fresh,
  [int]$Timeout = 600
)
$ErrorActionPreference = "Stop"

function Find-Uv {
  $cmd = Get-Command uvx -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  $candidates = @("$env:USERPROFILE\.local\bin\uvx.exe", "$env:USERPROFILE\.cargo\bin\uvx.exe")
  foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
  return $null
}

function Test-ChromeInstalled {
  $paths = @("$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
             "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
             "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe")
  foreach ($p in $paths) { if ($p -and (Test-Path $p)) { return $true } }
  return $false
}

function Get-DefaultBrowser {
  $key = "HKCU:\Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice"
  $progId = (Get-ItemProperty -Path $key -ErrorAction SilentlyContinue).ProgId
  if (-not $progId) {
    $key = "HKCU:\Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice"
    $progId = (Get-ItemProperty -Path $key -ErrorAction SilentlyContinue).ProgId
  }
  switch -Wildcard ($progId) {
    "ChromeHTML*" { return "chrome" }
    "MSEdge*"     { return "msedge" }
    default       { if (Test-ChromeInstalled) { return "chrome" } else { return "chromium" } }
  }
}

if ($Browser -eq "auto") {
  $Browser = Get-DefaultBrowser
  Write-Host "Default browser detected: $Browser" -ForegroundColor DarkGray
}
if ($Browser -eq "chrome" -and -not (Test-ChromeInstalled)) {
  Write-Host "Google Chrome not found - falling back to the bundled Chromium." -ForegroundColor Yellow
  $Browser = "chromium"
}

$uvx = Find-Uv
if (-not $uvx) {
  Write-Host "Installing uv (https://astral.sh/uv) ..." -ForegroundColor Cyan
  powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
  $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
  $uvx = Find-Uv
  if (-not $uvx) { throw "uv installation failed. Install manually from https://docs.astral.sh/uv/ and retry." }
}

New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
$env:NOTEBOOKLM_HOME = (Resolve-Path $DataDir).Path
$spec = if ($Version) { "notebooklm-py[browser]==$Version" } else { "notebooklm-py[browser]" }

if ($Browser -eq "chromium") {
  # Only the bundled Chromium needs a Playwright browser download; chrome/msedge use the installed app.
  Write-Host "Installing Playwright Chromium (one time) ..." -ForegroundColor Cyan
  & $uvx --python 3.12 --from $spec playwright install chromium
}

$uv = Join-Path (Split-Path $uvx) "uv.exe"
$helper = Join-Path $PSScriptRoot "nlm_login.py"

function Invoke-Login([bool]$UseFresh) {
  $loginArgs = @($Browser, "$Timeout")
  if ($UseFresh) { $loginArgs += "--fresh" }
  Write-Host ""
  Write-Host "Launching sign-in with $Browser ..." -ForegroundColor Cyan
  Write-Host ">>> A separate $Browser window opens on the Google sign-in page." -ForegroundColor Yellow
  Write-Host ">>> Sign in with the account that owns your notebooks; it finishes by itself once NotebookLM opens." -ForegroundColor Yellow
  & $uv run --no-project --quiet --python 3.12 --with $spec python $helper @loginArgs | Out-Host
  return $LASTEXITCODE
}

function Test-Session {
  Write-Host "Verifying session ..." -ForegroundColor Cyan
  & $uvx --python 3.12 --from $spec notebooklm auth check --test | Out-Host
  return ($LASTEXITCODE -eq 0)
}

Write-Host "NOTEBOOKLM_HOME = $env:NOTEBOOKLM_HOME" -ForegroundColor DarkGray
$code = Invoke-Login $Fresh.IsPresent
if ($code -eq 0 -and (Test-Session)) {
  Write-Host "Signed in - NotebookLM session saved. You can close the browser window." -ForegroundColor Green
  exit 0
}
throw "NotebookLM sign-in did not produce a valid session. Run '.\nlm2drive login --fresh' and complete the Google sign-in in the browser window."
