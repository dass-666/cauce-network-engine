@echo off
rem Ejecuta install.ps1 solo para este proceso (no cambia la politica de ejecucion del sistema).
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
pause
