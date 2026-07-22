@echo off
setlocal enabledelayedexpansion
title PC Screen Control - Remove
cd /d "%~dp0"

echo.
echo   ==========================================================
echo      PC Screen Control  -  Remove
echo   ==========================================================
echo.
echo   This will:
echo     - take its entry out of your Claude config, leaving every
echo       other MCP server exactly as it is
echo     - delete the folder it installed itself into
echo.
echo   It will NOT touch the registry, any system setting, or
echo   anything outside your user profile. A copy of each config
echo   from before this runs is kept next to it.
echo.
echo   Close Claude first - a running server holds its own files open.
echo.
pause

rem -------------------------------------------------- find an interpreter
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
if not defined PYEXE (
    echo   [X] No Python found - nothing to run this with.
    echo       You can remove it by hand: delete the "pc-screen-control"
    echo       entry from your Claude config and delete the folder
    echo       %%LOCALAPPDATA%%\pc-screen-control
    echo.
    pause
    exit /b 1
)

rem  Prefer the installed copy: the downloaded folder may already be gone,
rem  and the installed copy is the one that knows where it put itself.
set "ZIEL=%LOCALAPPDATA%\pc-screen-control\server.py"
if not exist "%ZIEL%" set "ZIEL=%~dp0..\src\server.py"
if not exist "%ZIEL%" (
    echo   [X] Neither the installed copy nor src\server.py was found.
    echo.
    pause
    exit /b 1
)

"%PYEXE%" %PYARG% "%ZIEL%" --uninstall
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo   ==========================================================
    echo      Removed. Restart Claude and the tools are gone.
    echo   ==========================================================
) else (
    echo   ==========================================================
    echo      Not finished - see the messages above.
    echo   ==========================================================
)
echo.
pause
endlocal
exit /b %RC%
