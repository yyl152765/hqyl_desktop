@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build.ps1" %*
set "BuildExitCode=%ERRORLEVEL%"
if not "%BuildExitCode%"=="0" echo Build failed. Check the missing prerequisites listed above.
pause
exit /b %BuildExitCode%
