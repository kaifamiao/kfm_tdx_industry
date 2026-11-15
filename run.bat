@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

if not exist ".venv\Scripts\python.exe" (
  echo [SETUP] Creating venv
  python -m venv .venv
  if errorlevel 1 exit /b 1
  set "PY=.venv\Scripts\python.exe"
)

"%PY%" -c "import fastapi,uvicorn" >nul 2>&1
if errorlevel 1 (
  echo [SETUP] Installing requirements
  "%PY%" -m pip install -r requirements.txt --disable-pip-version-check
  if errorlevel 1 exit /b 1
)

echo [START] http://127.0.0.1:13520/
"%PY%" run.py %*
