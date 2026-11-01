@echo off
setlocal
cd /d "%~dp0"
python local_launcher.py
if errorlevel 1 pause
endlocal
