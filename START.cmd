@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
set "CHECK=import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)"

rem 1) The py launcher from python.org: pick the newest installed Python 3.
where py.exe >nul 2>&1
if not errorlevel 1 (
  py -3 -c "%CHECK%" >nul 2>&1
  if not errorlevel 1 (
    where pyw.exe >nul 2>&1
    if not errorlevel 1 (
      start "" pyw.exe -3 "%~dp0launcher.py"
      exit /b 0
    )
    py -3 "%~dp0launcher.py"
    if errorlevel 1 pause
    exit /b
  )
)

rem 2) python.exe on PATH, skipping the Microsoft Store stub in WindowsApps
rem    (it only opens the Store and is not a real interpreter).
for /f "delims=" %%P in ('where python.exe 2^>nul') do (
  set "CANDIDATE=%%P"
  echo !CANDIDATE! | find /i "\WindowsApps\" >nul
  if errorlevel 1 (
    "!CANDIDATE!" -c "%CHECK%" >nul 2>&1
    if not errorlevel 1 (
      set "WINDOWED=%%~dpPpythonw.exe"
      if exist "!WINDOWED!" (
        start "" "!WINDOWED!" "%~dp0launcher.py"
        exit /b 0
      )
      "!CANDIDATE!" "%~dp0launcher.py"
      if errorlevel 1 pause
      exit /b
    )
  )
)

echo Python 3.12 or newer is required.
echo Install it from https://www.python.org/downloads/ (enable "tcl/tk" and "py launcher").
echo The Microsoft Store shortcut "python" is not enough.
pause
exit /b 1
