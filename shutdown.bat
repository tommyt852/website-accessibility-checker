@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ============================================
echo  Website Accessibility Checker - Shutdown
echo ============================================
echo Stops running a11y_crawl.py processes, then optionally
echo removes the local .venv folder.
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '^python(w)?\.exe$' -and $_.CommandLine -and ($_.CommandLine -like '*a11y_crawl.py*') };" ^
  "if (-not $procs) { Write-Host 'No running a11y_crawl.py process found.'; exit 0 }" ^
  "foreach ($p in @($procs)) { Write-Host ('Stopping PID ' + $p.ProcessId); Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }"

echo.
choice /C YN /M "Also delete .venv (need setup.bat again next time)"
if errorlevel 2 goto done
if exist ".venv\" (
  echo Removing .venv ...
  rmdir /s /q ".venv"
  if exist ".venv\" (
    echo [WARN] Could not fully remove .venv - close any open terminals using it.
  ) else (
    echo .venv removed.
  )
) else (
  echo No .venv folder.
)

:done
echo.
echo Shutdown finished.
pause
endlocal
