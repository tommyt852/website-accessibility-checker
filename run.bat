@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [.venv missing] Running setup.bat first...
  call "%~dp0setup.bat"
  if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Setup did not create .venv
    pause
    exit /b 1
  )
)

set "URL="
set "EXTRA="
set "OUT=a11y_report"
set "WORKERS=4"

if /I "%~1"=="--resume" (
  set "EXTRA=--resume"
  set "URL=%~2"
) else (
  set "URL=%~1"
  if /I "%~2"=="--resume" set "EXTRA=--resume"
)

if "%URL%"=="" (
  set /p URL=Start URL (e.g. https://example.com/): 
)
if "%URL%"=="" (
  echo [ERROR] No URL given.
  pause
  exit /b 1
)

echo Starting crawl...
echo URL=%URL%  OUT=%OUT%  WORKERS=%WORKERS%  %EXTRA%
echo Progress shows below. Press Ctrl+C to save checkpoint and exit.
echo.

".venv\Scripts\python.exe" a11y_crawl.py "%URL%" -o "%OUT%" --workers %WORKERS% %EXTRA%
set "EC=%ERRORLEVEL%"

echo.
if "%EC%"=="130" (
  echo Interrupted. Checkpoint saved under %OUT%\checkpoint.json
  echo Resume: run.bat "%URL%" --resume
) else if "%EC%"=="0" (
  echo Done. Open %OUT%\summary.txt
) else (
  echo Exit code %EC%
)
pause
endlocal
exit /b %EC%
