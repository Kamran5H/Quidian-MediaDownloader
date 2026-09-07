@echo off
REM Quidian — Launcher (Windows)
title Quidian — Kamran Ashraf
cd /d "%~dp0"
echo ==============================================================================
echo             QUIDIAN — MEDIA DOWNLOADER
echo               Developed by: Kamran Ashraf
echo ==============================================================================
echo Starting application server at http://127.0.0.1:5050 ...
start "" powershell -NoProfile -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:5050'"
python app.py
pause

