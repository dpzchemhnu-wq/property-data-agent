@echo off
setlocal EnableExtensions
title Aspen Property Agent - One Click
set "INSTALL_ROOT=%LOCALAPPDATA%\PropertyDataAgent"
set "ZIP_PATH=%TEMP%\property-data-agent-main.zip"
set "REPO_ZIP=https://github.com/dpzchemhnu-wq/property-data-agent/archive/refs/heads/main.zip"
echo.
echo [1/4] 正在准备网页程序...
if not exist "%INSTALL_ROOT%" mkdir "%INSTALL_ROOT%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $zip='%ZIP_PATH%'; $root='%INSTALL_ROOT%'; $stage=Join-Path $env:TEMP ('property-data-agent-extract-' + [guid]::NewGuid().ToString('N')); try { Invoke-WebRequest -UseBasicParsing -Uri '%REPO_ZIP%' -OutFile $zip; Expand-Archive -LiteralPath $zip -DestinationPath $stage -Force; $folder=Get-ChildItem -LiteralPath $stage -Directory | Select-Object -First 1; if(-not $folder){throw 'GitHub 压缩包没有项目目录'}; Get-ChildItem -LiteralPath $folder.FullName -Force | Copy-Item -Destination $root -Recurse -Force } finally { if(Test-Path -LiteralPath $stage){Remove-Item -LiteralPath $stage -Recurse -Force} }"
if errorlevel 1 goto download_failed
echo [2/4] 正在检查 Python...
where py >nul 2>nul
if not errorlevel 1 (
  py -3.12 -c "import sys" >nul 2>nul
  if not errorlevel 1 goto start_with_py
)
where python >nul 2>nul
if not errorlevel 1 goto start_with_python
echo 未检测到 Python，尝试通过 Windows 包管理器安装 Python 3.12...
where winget >nul 2>nul
if errorlevel 1 goto python_missing
winget install --id Python.Python.3.12 --exact --silent --accept-package-agreements --accept-source-agreements
if errorlevel 1 goto python_missing
where py >nul 2>nul
if not errorlevel 1 (
  py -3.12 -c "import sys" >nul 2>nul
  if not errorlevel 1 goto start_with_py
)
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" goto start_with_direct_python
goto python_missing
:start_with_py
echo [3/4] 正在启动本机 Aspen 网页...
py -3.12 "%INSTALL_ROOT%\local_launcher.py"
goto finished
:start_with_python
echo [3/4] 正在启动本机 Aspen 网页...
python "%INSTALL_ROOT%\local_launcher.py"
goto finished
:start_with_direct_python
echo [3/4] 正在启动本机 Aspen 网页...
"%LOCALAPPDATA%\Programs\Python\Python312\python.exe" "%INSTALL_ROOT%\local_launcher.py"
goto finished
:download_failed
echo.
echo 项目下载失败。请检查电脑是否联网，或确认 GitHub 可以访问。
pause
goto finished
:python_missing
echo.
echo 无法自动安装 Python。请先安装 Python 3.12，再重新双击此文件。
echo 下载地址：https://www.python.org/downloads/windows/
pause
:finished
endlocal
