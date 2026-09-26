@echo off
REM Start Dobot: the backend in a minimised console, then the installed desktop app.
REM
REM Adjust DOBOT_APP if you installed the app somewhere other than the per-user default.
REM See docs/install.md for the alternatives (Task Scheduler, NSSM service, manual start).

setlocal

set "REPO=%~dp0.."
set "DOBOT_APP=%LOCALAPPDATA%\Dobot\Dobot.exe"

if not exist "%REPO%\backend\.env" (
  echo Dobot: no .env found at "%REPO%\backend\.env" - copy .env.example to .env and add your keys.
  echo See docs/install.md section 2.
  pause
  exit /b 1
)

echo Starting the Dobot backend on http://127.0.0.1:8756 ...
start "Dobot backend" /min cmd /c "cd /d "%REPO%\backend" && uv run uvicorn app.main:app --host 127.0.0.1 --port 8756"

REM Give the gateway a moment to bind the port before the window asks it for state.
timeout /t 4 /nobreak >nul

if exist "%DOBOT_APP%" (
  echo Starting Dobot ...
  start "" "%DOBOT_APP%"
) else (
  echo Dobot: the desktop app is not installed at "%DOBOT_APP%".
  echo Run "cd desktop && npm run tauri build" and install the bundle, or use "npm run tauri dev".
  echo The backend is running; press any key to stop it.
  pause >nul
)

endlocal
