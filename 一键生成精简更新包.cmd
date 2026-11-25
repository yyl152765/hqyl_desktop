@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build.ps1" -SkipInternalDependencies %*
set "BuildExitCode=%ERRORLEVEL%"
if not "%BuildExitCode%"=="0" echo Build failed. Review the error above.
pause
exit /b %BuildExitCode%
