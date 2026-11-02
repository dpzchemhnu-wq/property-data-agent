@echo off
chcp 65001 >nul
if /i not "%~1"=="--connector-console" (
  start "Aspen Property Agent Connector" cmd /k call "%~f0" --connector-console
  exit /b
)
shift
setlocal EnableExtensions
title Aspen Property Agent - One Click
set "INSTALL_ROOT=%LOCALAPPDATA%\PropertyDataAgent"
set "ZIP_PATH=%TEMP%\property-data-agent-main.zip"
set "REPO_ZIP=https://github.com/dpzchemhnu-wq/property-data-agent/archive/refs/heads/main.zip"
set "STATUS_FILE=%INSTALL_ROOT%\connection_status.txt"
set "LOG_FILE=%INSTALL_ROOT%\startup.log"
set "PSEXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
echo.
echo [1/4] 正在准备本地安装目录...
if not exist "%INSTALL_ROOT%" mkdir "%INSTALL_ROOT%"
if not exist "%INSTALL_ROOT%" goto install_folder_failed
echo [%date% %time%] Connector startup > "%LOG_FILE%"
if exist "%INSTALL_ROOT%\local_launcher.py" del /q "%INSTALL_ROOT%\connection_status.txt" >nul 2>nul
"%PSEXE%" -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $zip='%ZIP_PATH%'; $root='%INSTALL_ROOT%'; $stage=Join-Path $env:TEMP ('property-data-agent-extract-' + [guid]::NewGuid().ToString('N')); try { Invoke-WebRequest -UseBasicParsing -Uri '%REPO_ZIP%' -OutFile $zip; Expand-Archive -LiteralPath $zip -DestinationPath $stage -Force; $folder=Get-ChildItem -LiteralPath $stage -Directory | Select-Object -First 1; if(-not $folder){throw 'GitHub zip no project dir'}; Get-ChildItem -LiteralPath $folder.FullName -Force | Copy-Item -Destination $root -Recurse -Force } finally { if(Test-Path -LiteralPath $stage){Remove-Item -LiteralPath $stage -Recurse -Force} }" >> "%LOG_FILE%" 2>&1
if errorlevel 1 goto download_failed
echo [2/4] 正在检测 Python...
set "PYEXE="
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
if not errorlevel 1 set "PYEXE=python"
if not defined PYEXE (
  py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
  if not errorlevel 1 set "PYEXE=py -3"
)
if not defined PYEXE (
  if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
)
if not defined PYEXE (
  if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
)
if not defined PYEXE goto python_missing
echo [3/4] 正在启动本地 Aspen 桥接...
%PYEXE% "%INSTALL_ROOT%\local_launcher.py"
if errorlevel 1 goto launcher_failed
goto finished
:download_failed
echo.
echo 项目下载失败。请检查网络，确认能访问 GitHub 后重试。
echo 启动日志：%LOG_FILE%
if exist "%LOG_FILE%" type "%LOG_FILE%"
pause
goto finished
:install_folder_failed
echo.
echo 无法创建安装目录：%INSTALL_ROOT%
echo 请确认当前 Windows 用户对此目录有写入权限。
pause
goto finished
:python_missing
echo.
echo 未找到可用的 Python 3.11 或更高版本。
echo 请安装 Python 3.11+ 后再双击本文件。
echo 下载地址：https://www.python.org/downloads/windows/
echo 启动日志：%LOG_FILE%
if exist "%LOG_FILE%" type "%LOG_FILE%"
pause
goto finished
:launcher_failed
echo.
echo 本机连接器没有成功启动，网页不会收到配对信息。
echo 请查看诊断文件：%INSTALL_ROOT%\connection_status.txt
echo 启动日志：%LOG_FILE%
if exist "%STATUS_FILE%" type "%STATUS_FILE%"
if exist "%LOG_FILE%" type "%LOG_FILE%"
echo 若需要重新尝试，请关闭此窗口后再次运行 BAT。
pause
:finished
endlocal
