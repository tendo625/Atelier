@echo off
setlocal
cd /d "%~dp0"
if defined ATELIER_PYTHON goto run
if exist ".venv-build\Scripts\python.exe" set "ATELIER_PYTHON=%~dp0.venv-build\Scripts\python.exe"
if defined ATELIER_PYTHON goto run
if exist ".venv\Scripts\python.exe" set "ATELIER_PYTHON=%~dp0.venv\Scripts\python.exe"
if defined ATELIER_PYTHON goto run
echo Build environment missing. Run powershell -NoProfile -ExecutionPolicy Bypass -File scripts\bootstrap_windows.ps1 -Python PATH
exit /b 1
:run
"%ATELIER_PYTHON%" scripts\package_release.py %*
exit /b %errorlevel%
