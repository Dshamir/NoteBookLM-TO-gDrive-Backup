<#
  helper.ps1 - manage the host-side "Reconnect NotebookLM" helper (see scripts/reconnect_helper.py, ADR-011).

    -Action start      launch the helper hidden in the background (127.0.0.1:8766, token in data\helper.json)
    -Action stop       shut it down
    -Action status     is it running?
    -Action install    start it automatically at every Windows logon (Scheduled Task, current user, no admin)
    -Action uninstall  remove that Scheduled Task

  The helper only ever launches scripts\login.ps1 (the same sign-in as `.\nlm2drive login`) when the web UI's
  Reconnect button asks for it. It is optional: without it the UI tells you to run `.\nlm2drive login`.
#>
param(
  [Parameter(Mandatory = $true)][ValidateSet("start", "stop", "status", "install", "uninstall")][string]$Action,
  [Parameter(Mandatory = $true)][string]$DataDir,
  [string]$Version = "",
  [ValidateSet("auto", "chrome", "msedge", "chromium")][string]$Browser = "auto",
  [int]$Port = 8766
)
$ErrorActionPreference = "Stop"
$TaskName = "nlm2drive-reconnect-helper"

New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
$nlmDir = (Resolve-Path $DataDir).Path
$dataRoot = Split-Path $nlmDir
$conf = Join-Path $dataRoot "helper.json"
$helperPy = Join-Path $PSScriptRoot "reconnect_helper.py"

function Find-Uv {
  $cmd = Get-Command uv -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  foreach ($c in @("$env:USERPROFILE\.local\bin\uv.exe", "$env:USERPROFILE\.cargo\bin\uv.exe")) { if (Test-Path $c) { return $c } }
  return $null
}

function Get-Conf {
  if (-not (Test-Path $conf)) { return $null }
  try { return Get-Content $conf -Raw | ConvertFrom-Json } catch { return $null }
}

function Test-Helper {
  $c = Get-Conf
  if (-not $c) { return $false }
  try {
    $null = Invoke-RestMethod -Uri "http://127.0.0.1:$($c.port)/status" -Headers @{ Authorization = "Bearer $($c.token)" } -TimeoutSec 3
    return $true
  } catch { return $false }
}

switch ($Action) {
  "status" {
    if (Test-Helper) { $c = Get-Conf; Write-Host "running (pid $($c.pid), 127.0.0.1:$($c.port))"; exit 0 }
    Write-Host "not running"; exit 1
  }
  "start" {
    if (Test-Helper) { Write-Host "Reconnect helper already running."; exit 0 }
    if (Test-Path $conf) { Remove-Item $conf -Force -ErrorAction SilentlyContinue }   # stale
    $uv = Find-Uv
    if (-not $uv) { throw "uv is not installed yet - run '.\nlm2drive login' once (it installs uv), then retry." }
    $logs = Join-Path $dataRoot "logs"
    New-Item -ItemType Directory -Force -Path $logs | Out-Null
    $argList = @("run", "--no-project", "--quiet", "--python", "3.12", "python", "`"$helperPy`"",
                 "--data-dir", "`"$nlmDir`"", "--port", "$Port", "--browser", $Browser)
    if ($Version) { $argList += @("--version", $Version) }
    # NOT Start-Process -RedirectStandard*: that makes the child inherit our stdout pipe, so anything that captures
    # this script's output (nlm2drive up | ..., CI, an agent) would wait until the helper exits. cmd does the file
    # redirection itself. It is launched through WMI (Win32_Process.Create): the helper then inherits no handles and
    # lives outside our process tree / job object, so it also survives the caller (a terminal, a CI step, an agent
    # task) being torn down. Start-Process is only the fallback if WMI is unavailable.
    $out = Join-Path $logs "helper.log"
    $err = Join-Path $logs "helper.err.log"
    $inner = "`"$uv`" " + ($argList -join " ") + " > `"$out`" 2> `"$err`""
    $cmdLine = "cmd.exe /c `"$inner`""
    $created = $null
    try { $created = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmdLine; CurrentDirectory = (Split-Path $PSScriptRoot) } -ErrorAction Stop } catch { }
    if (-not $created -or $created.ReturnValue -ne 0) {
      Start-Process -FilePath "cmd.exe" -ArgumentList @("/c", "`"$inner`"") -WindowStyle Hidden | Out-Null
    }
    for ($i = 0; $i -lt 40; $i++) {   # first run may download Python 3.12 once
      Start-Sleep -Milliseconds 500
      if (Test-Helper) { Write-Host "Reconnect helper started (127.0.0.1:$Port)."; exit 0 }
    }
    throw "Reconnect helper did not come up - see data\logs\helper.err.log"
  }
  "stop" {
    $c = Get-Conf
    if (-not $c) { Write-Host "Reconnect helper not running."; exit 0 }
    try {
      Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$($c.port)/shutdown" -Headers @{ Authorization = "Bearer $($c.token)" } -TimeoutSec 3 | Out-Null
      Start-Sleep -Seconds 1
    } catch { }
    if (Get-Process -Id $c.pid -ErrorAction SilentlyContinue) { & taskkill /PID $c.pid /T /F | Out-Null }
    if (Test-Path $conf) { Remove-Item $conf -Force -ErrorAction SilentlyContinue }
    Write-Host "Reconnect helper stopped."
  }
  "install" {
    $me = "$env:USERDOMAIN\$env:USERNAME"
    $psArgs = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Action start -DataDir `"$nlmDir`" -Browser $Browser -Port $Port"
    if ($Version) { $psArgs += " -Version $Version" }
    $act = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $psArgs
    $trg = New-ScheduledTaskTrigger -AtLogOn -User $me
    $set = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
    $pri = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $act -Trigger $trg -Settings $set -Principal $pri -Force | Out-Null
    Write-Host "Installed: the Reconnect helper now starts at every Windows logon (task '$TaskName')."
  }
  "uninstall" {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
      Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
      Write-Host "Removed the logon task '$TaskName'."
    } else { Write-Host "No logon task installed." }
  }
}
