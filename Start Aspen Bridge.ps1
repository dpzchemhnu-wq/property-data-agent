$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

if (-not $env:ASPEN_BRIDGE_TOKEN) {
    $Bytes = New-Object byte[] 32
    $Generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $Generator.GetBytes($Bytes)
    $Generator.Dispose()
    $env:ASPEN_BRIDGE_TOKEN = [Convert]::ToBase64String($Bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
    Write-Host "已生成本次桥接令牌。请将下方值安全地填入 Render 的 ASPEN_BRIDGE_TOKEN：" -ForegroundColor Yellow
    Write-Host $env:ASPEN_BRIDGE_TOKEN -ForegroundColor Cyan
    Write-Host "不要把令牌提交到 GitHub、聊天记录或公开文档。停止本窗口后令牌失效；重启时需同步更新 Render。"
}

$PythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $PythonCommand) {
    throw "未找到 Python。请在 Aspen Windows 主机安装 Python 3.12，并勾选 Add Python to PATH。"
}

python -c "import sys; assert sys.version_info >= (3, 10), '需要 Python 3.10 或更高版本'"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -c "import pythoncom, win32com.client"
if ($LASTEXITCODE -ne 0) {
    throw "pywin32 安装失败；Aspen COM 桥接无法启动。"
}

$env:ASPEN_BRIDGE_HOST = "127.0.0.1"
$env:ASPEN_BRIDGE_PORT = "8765"
Write-Host "Aspen 只读桥接即将启动于 127.0.0.1:8765。保持 Aspen 登录会话和此窗口运行。" -ForegroundColor Green
python aspen_bridge.py
