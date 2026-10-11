@echo off
rem nlm2drive.cmd - Windows launcher for nlm2drive.sh (PowerShell / cmd / double-click).
rem Usage:  .\nlm2drive login   |   .\nlm2drive up   |   .\nlm2drive help
setlocal
set "HERE=%~dp0"
set "BASH="
for %%P in ("%ProgramFiles%\Git\bin\bash.exe" "%ProgramFiles(x86)%\Git\bin\bash.exe" "%LocalAppData%\Programs\Git\bin\bash.exe") do (
  if not defined BASH if exist "%%~P" set "BASH=%%~P"
)
if defined BASH goto :gitbash

where wsl >nul 2>&1
if %ERRORLEVEL%==0 goto :wsl

echo [x] Neither Git Bash nor WSL was found. Install Git for Windows: https://git-scm.com/download/win
exit /b 1

:gitbash
"%BASH%" "%HERE%nlm2drive.sh" %*
exit /b %ERRORLEVEL%

:wsl
wsl --cd "%HERE%" bash ./nlm2drive.sh %*
exit /b %ERRORLEVEL%
