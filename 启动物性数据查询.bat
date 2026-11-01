@echo off
setlocal
set "ROOT=%~dp0"
set "PYTHONPATH=%ROOT%runtime\Lib\site-packages"
if exist "%ROOT%config\api_key.txt" set /p API_KEY=<"%ROOT%config\api_key.txt"
cd /d "%ROOT%"
"%ROOT%runtime\python.exe" -m streamlit run app.py --server.headless true
pause
