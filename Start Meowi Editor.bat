@echo off
title SmarterClip Editor
cd /d "%~dp0"
echo Starting SmarterClip Editor...
echo Opening http://127.0.0.1:8765/ in your browser.
echo Keep this window open while you edit. Close it to stop the editor.
start "" http://127.0.0.1:8765/
python editor\server.py
pause
