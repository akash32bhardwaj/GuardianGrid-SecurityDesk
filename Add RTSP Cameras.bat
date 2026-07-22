@echo off
title Defender Octa - Add RTSP Cameras
cd /d "%~dp0"

set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

"%PY%" add_cameras.py

echo.
pause
