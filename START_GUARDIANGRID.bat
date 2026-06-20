@echo off 
title GuardianGrid Security System 
cd /d "%~dp0" 
start "" /B python api_server.py --camera 1 --port 5000 
timeout /t 4 /nobreak >nul 
start "" "http://localhost:5000" 
echo GuardianGrid is RUNNING at http://localhost:5000 
echo Close this window to STOP the system. 
pause 
