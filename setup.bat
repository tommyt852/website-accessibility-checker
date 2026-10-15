@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ============================================
echo  Website Accessibility Checker - Setup
echo ============================================

where py >nul 2>&1
if %ERRORLEVEL%==0 (
  set "PY=py -3"
) else (
  where python >nul 2>&1
  if %ERRORLEVEL%==0 (
    set "PY=python"
  ) else (
    echo [ERROR] Python 3 not found. Install from https://www.python.org/downloads/
    echo         and enable "Add python.exe to PATH".
    pause
    exit /b 1
  )
)

echo Using: %PY%
%PY% --version

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo [ERROR] Failed to create .venv
    pause
    exit /b 1
  )
) else (
  echo Virtual environment already exists.
)

echo Installing dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo [ERROR] pip install failed
  pause
  exit /b 1
)

echo.
echo Setup complete.
echo Next: double-click run.bat  OR  run.bat https://example.com/
echo Ctrl+C during a crawl saves progress; use run.bat --resume to continue.
echo.
pause
endlocal
