@echo off
title SmarterClip Editor
powershell -NoProfile -ExecutionPolicy Bypass -Command "$env:SCBASE='https://smarterpaw-llc.github.io/smarterpaw-clip-editor'; iex (irm $env:SCBASE/launcher.ps1)"
pause
