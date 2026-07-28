@echo off
REM Daily growth tracker runner for github-pr-context-mcp
REM Called by Windows Task Scheduler every day at 09:00

set SCRIPT_DIR=%~dp0
set PYTHON=%SCRIPT_DIR%.venv\Scripts\python.exe
set SCRIPT=%SCRIPT_DIR%track_growth.py
set LOG=%SCRIPT_DIR%growth_tracker.log

echo. >> "%LOG%"
echo ============================================================ >> "%LOG%"
echo   Run at %DATE% %TIME% >> "%LOG%"
echo ============================================================ >> "%LOG%"

"%PYTHON%" "%SCRIPT%" >> "%LOG%" 2>&1
