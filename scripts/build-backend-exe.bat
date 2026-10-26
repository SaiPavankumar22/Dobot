@echo off
REM Build the self-contained backend sidecar: backend\dist\dobot-backend.exe
REM Run from anywhere. Requires uv (https://docs.astral.sh/uv/).
setlocal
set HERE=%~dp0

echo [1/2] Preparing the locked backend environment...
cd /d "%HERE%..\backend" || goto :fail
uv sync --locked --extra voice --extra dev || goto :fail

echo [2/2] Running PyInstaller in the same environment as the backend...
uv run --with pyinstaller python -m PyInstaller --noconfirm --clean dobot-backend.spec || goto :fail

echo Copying skills/ next to the exe for standalone smoke tests...
if exist dist\skills rmdir /s /q dist\skills
xcopy ..\skills dist\skills /e /i /q || goto :fail

echo.
echo Done: backend\dist\dobot-backend.exe (skills are also embedded inside it)
echo The desktop build picks it up automatically (desktop\src-tauri\binaries\).
exit /b 0

:fail
echo build-backend-exe failed.
exit /b 1
