@echo off
title SmarterClip Editor
REM Start the local editor (or just open it if it's already running), then open the browser.
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue){exit 1}else{exit 0}"
if errorlevel 1 (
  echo SmarterClip Editor is already running. Opening it...
  start "" http://127.0.0.1:8765/
  timeout /t 2 /nobreak >nul
  exit /b 0
)
echo Starting SmarterClip Editor... keep this window open while you work; close it to quit.
start "" http://127.0.0.1:8765/
cd /d "%~dp0editor"
python server.py
echo.
echo Server stopped. Press any key to close.
pause >nul
