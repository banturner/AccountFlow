@echo off
rem AccountFlow installer — double-click me. (Runs setup.ps1 with PowerShell.)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
echo.
pause
