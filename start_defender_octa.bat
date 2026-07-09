@echo off
title Defender Octa - Security Operations Platform
cd /d "%~dp0"

REM ================================================================
REM  start_defender_octa.bat
REM  One-double-click launcher. Auto-restarts if the app crashes,
REM  so an unattended gate box recovers on its own.
REM  Place next to api_server.py.
REM ================================================================

echo ==================================================
echo    DEFENDER OCTA - starting...
echo ==================================================
echo.

REM --- basic checks -------------------------------------------------
if not exist "api_server.py" (
  echo [ERROR] api_server.py not found in this folder.
  echo         Put this launcher next to api_server.py.
  pause
  exit /b 1
)
if not exist "site_config.json" (
  echo [WARNING] site_config.json not found - the app will use defaults.
  echo           Copy and edit site_config.json for this society.
  echo.
)

:runloop
echo [%date% %time%] Launching Defender Octa...
python api_server.py

REM  If python exits, we land here. Code 0 = clean stop (Ctrl+C).
if %errorlevel% == 0 (
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
