@echo off
setlocal enabledelayedexpansion
title PC Screen Control - Setup
cd /d "%~dp0"

echo.
echo   ==========================================================
echo      PC Screen Control  -  Setup
echo   ==========================================================
echo.
echo   This will:
echo     - check that Python is installed
echo     - install two Python packages (uiautomation, pillow)
echo     - copy the server to your user folder
echo     - add an entry to your Claude config (backed up first)
echo.
echo   No administrator rights. No system settings changed.
echo   Safe to run more than once - it just overwrites its own entry.
echo.

rem -------------------------------------------------- 1. find an interpreter
rem  The py launcher is preferred: it is a real path and never the Microsoft
rem  Store placeholder that "python" on PATH often is.
set "PYEXE="
set "PYARG="
for /f "delims=" %%P in ('where py 2^>nul') do (
    if not defined PYEXE ( set "PYEXE=%%P" & set "PYARG=-3" )
)
if not defined PYEXE (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        if not defined PYEXE set "PYEXE=%%P"
    )
)

if not defined PYEXE goto :nopython

rem -------------------------------------------------- 2. prove that it works
rem  Being on PATH is not the same as being able to run. The Microsoft Store
rem  placeholder is on PATH, opens the Store when executed, and runs nothing.
"%PYEXE%" %PYARG% -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3,9) else 2)" >nul 2>&1
if errorlevel 2 (
    echo   [X] Python is too old. Version 3.9 or newer is required.
    echo       Found: "%PYEXE%"
    goto :fail
)
if errorlevel 1 goto :nopython

rem -------------------------------------------------- 3. hand over to Python
"%PYEXE%" %PYARG% "%~dp0..\src\server.py" --install
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo   ==========================================================
    echo.
    echo      DONE  -  one thing left:
    echo.
    echo         ^>^>^>   CLOSE CLAUDE COMPLETELY AND START IT AGAIN   ^<^<^<
    echo.
    echo      Completely means the tray icon too, not just the window.
    echo      Claude reads its config only when it starts.
    echo.
    echo      Nothing else. There is no switch to flip afterwards.
    echo      To check: ask Claude to run  describe_screen
    echo.
    echo   ==========================================================
) else (
    echo   ==========================================================
    echo      Setup did not finish - see the messages above.
    echo   ==========================================================
)
echo.
echo   This output is also saved in install_log.txt next to this file.
echo.
pause
endlocal
exit /b %RC%

rem ---------------------------------------------------------------- failures
:nopython
echo.
echo   [X] No working Python was found.
echo.
echo       Install Python 3.9 or newer from python.org.
echo       During its setup, tick  "Add python.exe to PATH"  -  this is
echo       the step people miss, and without it this installer cannot
echo       find Python afterwards.
echo.
echo       Then run this file again.
echo.
echo   Opening the download page ...
start "" "https://www.python.org/downloads/"
goto :fail

:fail
echo.
pause
endlocal
exit /b 1
