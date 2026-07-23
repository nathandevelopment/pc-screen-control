@echo off
rem Prints the config other MCP clients (Cursor, VS Code, GPT Agents SDK, ...)
rem need, with your real path filled in. Changes nothing.
setlocal
where py >nul 2>&1 && (set "PY=py -3") || (set "PY=python")
%PY% "%~dp0print-config.py"
echo.
pause
endlocal
