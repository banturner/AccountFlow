@echo off
REM ============================================================
REM  AccountFlow one-click installer (Windows)
REM  Double-click this file. It runs the real installer below.
REM ============================================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
pause
