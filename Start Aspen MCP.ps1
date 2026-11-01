$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot
if (-not $env:ASPEN_MCP_TOKEN) {
    $Bytes = New-Object byte[] 32
    $Generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $Generator.GetBytes($Bytes)
    $Generator.Dispose()
    $env:ASPEN_MCP_TOKEN = [Convert]::ToBase64String($Bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
    Write-Host "请将本次令牌安全填入 Render 的 ASPEN_MCP_TOKEN：" -ForegroundColor Yellow
    Write-Host $env:ASPEN_MCP_TOKEN -ForegroundColor Cyan
}
$PythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $PythonCommand) { throw "未找到 Python 3.12。" }
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -c "import pythoncom, win32com.client"
if ($LASTEXITCODE -ne 0) { throw "pywin32 安装失败，无法连接 Aspen COM。" }
$env:ASPEN_MCP_HOST = "127.0.0.1"
$env:ASPEN_MCP_PORT = "8765"
Write-Host "Aspen MCP 只读服务：127.0.0.1:8765/mcp" -ForegroundColor Green
python aspen_mcp_server.py
