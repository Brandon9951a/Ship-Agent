@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_local_demo.ps1"
if errorlevel 1 pause
endlocal
