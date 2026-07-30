@echo off
title Defender Octa Launcher
cd /d "%~dp0"

echo.
echo   DEFENDER OCTA LAUNCHER
echo   [1] Local only
echo   [2] Local + Internet (live.snguardiangrid.com)
echo.
choice /c 12 /n /m "  Choose 1 or 2: "
set MODE=%errorlevel%

echo   Starting Defender Octa API + AI engine...
start "Defender Octa - API" cmd /k python api_server.py

timeout /t 12 /nobreak >nul

if "%MODE%"=="2" start "Defender Octa - Tunnel" cmd /k cloudflared.exe tunnel run --url http://localhost:5000 octa-demo

timeout /t 3 /nobreak >nul
start "" http://localhost:5000/frontend/

echo   Running. Closing the API/Tunnel windows stops the system.
pause