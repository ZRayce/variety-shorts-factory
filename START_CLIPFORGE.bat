@echo off
title ClipForge — Local video workbench
setlocal
cd /d "%~dp0"
set "PYTHON_EXE=python"
where "%PYTHON_EXE%" >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python 3.11 or newer, then reopen this window.
  pause
  exit /b 1
)
"%PYTHON_EXE%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
  echo Python 3.11 or newer is required.
  pause
  exit /b 1
)
echo ClipForge will print the local URL and data folder after startup.
echo Keep this window open. Press Ctrl+C to stop the local server.
"%PYTHON_EXE%" -m app.server
endlocal
