@echo off
call npm run build
if errorlevel 1 exit /b 1
xcopy /E /Y "%~dp0dist\*" "C:\GuardianGrid\GuardianGrid-SecurityDesk\frontend\"
echo.
echo BUILD + COPY DONE — restart serve.py and hard-refresh.