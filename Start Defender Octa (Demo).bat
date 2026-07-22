@echo off
setlocal enabledelayedexpansion
title Defender Octa - Security Operations Platform
cd /d "%~dp0"

echo ==================================================
echo    DEFENDER OCTA - starting demo...
echo ==================================================
echo.

REM --- 0) Sanity: this must sit next to api_server.py ---------------
if not exist "api_server.py" (
  echo [ERROR] api_server.py not found. Put this launcher in the project folder.
  pause
  exit /b 1
)

REM --- 1) Make sure the login page (index.html) is in place ---------
REM The React build ships index.html in indian_anpr\frontend but the app
REM serves from frontend\ . Copy it across if missing, else the login
REM page 404s.
if not exist "frontend\index.html" (
  echo [ERROR] frontend\index.html is missing. The dashboard will show a
  echo         blank page. Restore it from the project you shipped.
)

REM --- 2) Choose Python (prefer the demo virtual environment) -------
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

REM --- 2b) Auto-heal the login page (fixes stale hash / wrong base) -
if exist "fix_frontend.py" (
  echo [FIX] Checking the login page bundle...
  "%PY%" fix_frontend.py
)

REM --- 3) Open the browser to the login page once the API is up ----
echo [INFO] The login page will open automatically when the server is ready.
start "" powershell -NoProfile -WindowStyle Hidden -Command ^
  "for($i=0;$i -lt 90;$i++){try{Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 'http://localhost:5000/api/health' ^| Out-Null; Start-Process 'http://localhost:5000'; break}catch{Start-Sleep -Seconds 2}}"

REM --- 4) Run the server with auto-restart (unattended-gate style) --
:runloop
echo.
echo [%date% %time%] Launching Defender Octa on http://localhost:5000 ...
echo    Login:  admin  /  admin123     (change in site_config.json)
echo.
"%PY%" api_server.py

if %errorlevel%==0 (
  echo.
  echo Defender Octa stopped normally.
  goto end
)

echo.
echo ==================================================
echo  [%date% %time%] App exited unexpectedly (code %errorlevel%).
echo  Restarting in 5 seconds...  (close this window to stop)
echo ==================================================
timeout /t 5 /nobreak >nul
goto runloop

:end
echo.
pause
exit /b 0
