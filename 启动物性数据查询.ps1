$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root "runtime\python.exe"
$site = Join-Path $root "runtime\Lib\site-packages"
$keyFile = Join-Path $root "config\api_key.txt"

if (-not (Test-Path -LiteralPath $python)) {
    throw "未找到便携式 Python 运行时。请确认压缩包已完整解压。"
}
$env:PYTHONPATH = $site
if (Test-Path -LiteralPath $keyFile) {
    $key = (Get-Content -LiteralPath $keyFile -Raw).Trim()
    if ($key) { $env:API_KEY = $key }
}
Set-Location -LiteralPath $root
& $python -m streamlit run app.py --server.headless true
