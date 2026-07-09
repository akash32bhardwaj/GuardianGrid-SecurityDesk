@echo off
setlocal enabledelayedexpansion
title Defender Octa - Version Switcher

REM ================================================================
REM  switch_version.bat
REM  Swaps which build Flask serves at localhost:5000:
REM    - Defender Octa (official)  <-->  Legacy (previous version)
REM  Place this file in the backend folder next to api_server.py.
REM ================================================================

cd /d "%~dp0"

echo ==================================================
echo    DEFENDER OCTA - VERSION SWITCHER
echo ==================================================
echo.

REM --- Figure out which version is currently live ---------------
REM  Live build is always the folder named "frontend".
REM  We detect it by a marker: Octa's index.html contains "/frontend/assets".

set CURRENT=UNKNOWN
if exist "frontend\index.html" (
  findstr /C:"/frontend/assets" "frontend\index.html" >nul 2>&1
  if !errorlevel! == 0 (
    set CURRENT=OCTA
  ) else (
    set CURRENT=LEGACY
  )
) else (
  echo [ERROR] No "frontend" folder found here.
  echo         Run this from the backend folder next to api_server.py.
  echo.
  pause
  exit /b 1
)

echo Currently live at localhost:5000:  !CURRENT!
echo.

REM --- Check both archives exist before touching anything -------
if "!CURRENT!"=="OCTA" (
  if not exist "frontend_legacy\index.html" (
    echo [ERROR] "frontend_legacy" not found - nothing to switch to.
    echo.
    pause
    exit /b 1
  )
  echo This will switch to:  LEGACY  ^(previous version^)
) else (
  if not exist "frontend_octa\index.html" (
    echo [ERROR] "frontend_octa" not found - nothing to switch to.
    echo.
    pause
    exit /b 1
  )
  echo This will switch to:  DEFENDER OCTA  ^(official^)
)

echo.
set /p CONFIRM="Proceed with the switch? (Y/N): "
if /i not "!CONFIRM!"=="Y" (
  echo.
  echo Cancelled. Nothing changed.
  echo.
  pause
  exit /b 0
)

echo.
echo Switching...

if "!CURRENT!"=="OCTA" (
  REM  Octa is live -> stash it as frontend_octa, promote legacy
  ren "frontend" "frontend_octa"
  ren "frontend_legacy" "frontend"
  set NOWLIVE=LEGACY ^(previous version^)
) else (
  REM  Legacy is live -> stash it as frontend_legacy, promote octa
  ren "frontend" "frontend_legacy"
  ren "frontend_octa" "frontend"
  set NOWLIVE=DEFENDER OCTA ^(official^)
)

echo.
echo ==================================================
echo    DONE.  Now live:  !NOWLIVE!
echo ==================================================
echo.
echo IMPORTANT:
echo   1. Restart Flask  (Ctrl+C, then: python api_server.py --camera 1)
echo   2. Refresh the browser with Ctrl+Shift+R  (or use Incognito)
echo.
pause
