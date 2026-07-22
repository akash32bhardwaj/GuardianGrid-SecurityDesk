@echo off
setlocal enabledelayedexpansion
title Defender Octa - First-time setup (run once)
cd /d "%~dp0"

echo ==================================================
echo    DEFENDER OCTA - First-time setup (run once)
echo ==================================================
echo.
echo   This installs Python + all dependencies and puts a
echo   "Start Defender Octa" shortcut on the Desktop.
echo   Needs internet. Takes 10-25 min (big AI libraries).
echo.
pause

REM --- 0) Must be in the project folder ----------------------------
if not exist "api_server.py" (
  echo [ERROR] Run this from the Defender Octa project folder ^(next to api_server.py^).
  pause
  exit /b 1
)

REM --- 1) Ensure Python 3.11 is available --------------------------
echo.
echo [1/6] Checking Python 3.11...
set "PYLAUNCH="
py -3.11 --version >nul 2>&1 && set "PYLAUNCH=py -3.11"
if not defined PYLAUNCH (
  python --version >nul 2>&1 && set "PYLAUNCH=python"
)
if not defined PYLAUNCH (
  echo       Not found. Installing Python 3.11 via winget...
  where winget >nul 2>&1
  if errorlevel 1 (
    echo       [ERROR] winget not available. Install Python 3.11 manually from
    echo               https://www.python.org/downloads/release/python-3119/
    echo               ^(tick "Add python.exe to PATH"^), then re-run this setup.
    pause
    exit /b 1
  )
  winget install -e --id Python.Python.3.11 --scope machine --accept-source-agreements --accept-package-agreements
  REM py launcher lands in C:\Windows and is usable right away
  py -3.11 --version >nul 2>&1 && set "PYLAUNCH=py -3.11"
)
if not defined PYLAUNCH (
  echo       [INFO] Python was installed but this window can't see it yet.
  echo       Please CLOSE this window and run SETUP again to finish.
  pause
  exit /b 0
)
echo       Using: !PYLAUNCH!

REM --- 2) Create the virtual environment ---------------------------
echo.
echo [2/6] Creating virtual environment (.venv)...
if not exist ".venv\Scripts\python.exe" (
  !PYLAUNCH! -m venv .venv
)
set "VPY=.venv\Scripts\python.exe"
if not exist "%VPY%" (
  echo       [ERROR] Could not create the virtual environment.
  pause
  exit /b 1
)
"%VPY%" -m pip install --upgrade pip wheel setuptools

REM --- 3) Install CORE dependencies (must succeed) -----------------
echo.
echo [3/6] Installing core dependencies (Flask, ANPR/easyOCR, reports)...
echo       This is the big one - easyOCR pulls PyTorch. Please wait.
"%VPY%" -m pip install flask>=3.0.0 flask-cors>=4.0.0 Werkzeug>=3.0.0 PyJWT>=2.8.0 ^
  python-dotenv>=1.0.0 "opencv-python>=4.8.0" "numpy>=1.24.0,<2.0" easyocr>=1.7.1 ^
  reportlab>=4.0.0 openpyxl>=3.1.0 requests>=2.31.0 twilio>=8.0.0 tqdm>=4.66.0 ^
  Pillow>=10.0.0 pyttsx3>=2.90 psycopg2-binary>=2.9.9
if errorlevel 1 (
  echo.
  echo   [ERROR] Core install failed. Check the internet connection and re-run.
  pause
  exit /b 1
)

REM --- 4) Install HEAVY / OPTIONAL vision deps (non-fatal) ---------
echo.
echo [4/6] Installing face + person detection (insightface, onnxruntime, ultralytics)...
echo       If these fail, the app STILL runs - only live face/person AI is off.
"%VPY%" -m pip install onnxruntime>=1.16.0 insightface>=0.7.3 ultralytics>=8.0.0
if errorlevel 1 (
  echo.
  echo   [WARNING] Optional AI libraries did not fully install.
  echo   Likely cause: insightface needs "Microsoft C++ Build Tools".
  echo   The demo login + ANPR + dashboard will still work.
  echo   To enable face recognition later, install Build Tools from:
  echo   https://visualstudio.microsoft.com/visual-cpp-build-tools/  then re-run setup.
  echo.
)

REM --- 5) Put the login page where the app serves it --------------
echo.
echo [5/6] Installing the login page (index.html)...
if exist "frontend\index.html" (
  echo       Present.
) else (
  echo       [WARNING] frontend\index.html missing - dashboard will be blank.
  echo       Restore it from the project you shipped.
)

REM --- 6) Desktop shortcut ----------------------------------------
echo.
echo [6/6] Creating Desktop shortcut...
set "TARGET=%~dp0Start Defender Octa (Demo).bat"
powershell -NoProfile -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\Start Defender Octa.lnk');" ^
  "$s.TargetPath='%TARGET%';" ^
  "$s.WorkingDirectory='%~dp0';" ^
  "$s.IconLocation='%SystemRoot%\System32\shell32.dll,77';" ^
  "$s.Description='Start the Defender Octa demo';" ^
  "$s.Save()"
set "CAMTOOL=%~dp0Add RTSP Cameras.bat"
powershell -NoProfile -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\Add RTSP Cameras.lnk');" ^
  "$s.TargetPath='%CAMTOOL%';" ^
  "$s.WorkingDirectory='%~dp0';" ^
  "$s.IconLocation='%SystemRoot%\System32\shell32.dll,137';" ^
  "$s.Description='Enter the client RTSP camera links';" ^
  "$s.Save()"
echo       Done.

echo.
echo ==================================================
echo    SETUP COMPLETE
echo ==================================================
echo.
echo   Two Desktop shortcuts were created:
echo     * "Add RTSP Cameras"   - the ONLY on-site step: enter the
echo                              client's camera links.
echo     * "Start Defender Octa" - launches the demo, opens the login page.
echo.
echo   On-site flow:
echo     1. Double-click "Add RTSP Cameras", paste the client's RTSP URLs.
echo     2. Double-click "Start Defender Octa".
echo        Login page opens automatically. Login: admin / admin123
echo.
echo   NOTE: The FIRST launch downloads the easyOCR and face models
echo   (~400 MB), so it may take a couple of minutes. Later launches
echo   are fast.
echo.
pause
exit /b 0
