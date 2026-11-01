@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3.12 "%~dp0local_launcher.py"
) else (
  python "%~dp0local_launcher.py"
)
if errorlevel 1 pause
endlocal
