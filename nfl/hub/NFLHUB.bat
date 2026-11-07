@echo off
setlocal EnableExtensions EnableDelayedExpansion
title nfl hub
cd /d "%~dp0"

REM ---------- 0. the PC app, when it has been built ----------
REM  NFLHub.exe does the same job with a proper app window and a clean
REM  shutdown (close the window -> server and relays stop). Build it with
REM  ..\..\pc\build-pc.ps1, or just double-click NFLHub.exe directly.
if exist "%~dp0NFLHub.exe" (
    start "" "%~dp0NFLHub.exe" %*
    exit /b 0
)

set "PORT=8795"
set "URL=http://127.0.0.1:%PORT%/"

echo ============================================
echo   nfl hub - games by date + nfl tv
echo ============================================
echo.

REM ---------- 1. python ----------
set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY ( where py >nul 2>&1 && set "PY=py -3" )
if not defined PY (
    echo [X] Python not found on PATH.
    pause
    exit /b 1
)

REM ---------- 1b. player library (needed by the page) ----------
if not exist "%~dp0hls.min.js" (
    echo [1/3] fetching the player library...
    curl -sL --max-time 90 -o "%~dp0hls.min.js" "https://cdn.jsdelivr.net/npm/hls.js@1.5.20/dist/hls.min.js"
)
if not exist "%~dp0hls.min.js" (
    echo [X] could not download hls.min.js - check the connection
    pause
    exit /b 1
)

REM ---------- 2. clear whatever holds the port (stale copies included) ----------
REM  always restart: an old instance would keep serving old code otherwise
set "STALE="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:"LISTENING" ^| findstr ":%PORT% "') do set "STALE=%%p"
if defined STALE (
    echo [1/2] restarting the server
    taskkill /PID !STALE! /F >nul 2>&1
    timeout /t 1 /nobreak >nul
)

REM ---------- 3. start + wait ----------
echo [2/2] starting server...
set "PYW="
where pythonw >nul 2>&1 && set "PYW=pythonw"
if defined PYW (
    start "" /min %PYW% "hub.py" --port %PORT%
) else (
    start "" /min %PY% "hub.py" --port %PORT%
)

set "UP="
for /l %%i in (1,1,40) do (
    if not defined UP (
        curl -s -o nul --max-time 2 "%URL%api/channels" && set "UP=1"
        if not defined UP timeout /t 1 /nobreak >nul
    )
)

if not defined UP (
    echo.
    echo [X] server did not answer on %URL%
    echo     run this to see why:   %PY% hub.py --port %PORT%
    echo.
    pause
    exit /b 1
)

REM ---------- 4. tell the phone where to look ----------
echo.
echo   desktop:  %URL%
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do (
    for /f "tokens=* delims= " %%b in ("%%a") do echo   phone:    http://%%b:%PORT%/
)
echo.
echo   The app finds this PC by itself. If it cannot, run FIREWALL.bat once
echo   (as admin) to open ports 8795 and 8900-8959.
echo.

REM open as a real app window (no tabs, no address bar) when Chrome exists
set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if exist "%CHROME%" (
    start "" "%CHROME%" --app=%URL% --window-size=1400,880 --user-data-dir="%~dp0appprofile" --no-first-run --no-default-browser-check
) else (
    start "" "%URL%"
)
timeout /t 2 /nobreak >nul
exit /b 0
