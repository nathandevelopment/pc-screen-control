@echo off
setlocal
title PC Screen Control - Check for updates
cd /d "%~dp0"

echo.
echo   This is the ONLY part that goes online, and only because you started it.
echo   The server that controls your PC never connects to anything.
echo.

where py >nul 2>&1 && (set "PY=py -3") || (set "PY=python")
"%PY%" "%~dp0check-for-updates.py"

echo.
pause
endlocal
