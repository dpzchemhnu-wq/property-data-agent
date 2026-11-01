"""Start a per-user local Aspen MCP connector and open the online Streamlit app."""

from __future__ import annotations

import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from urllib.parse import urlencode
import webbrowser
from pathlib import Path


ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
PYTHON = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
CLOUDFLARED = ROOT / "cloudflared.exe"
ONLINE_APP_URL = "https://property-data-agent-pvd5y6dm5iacitfwkhtsxe.streamlit.app/"


def _ensure_python() -> None:
    if not PYTHON.exists():
        if os.name == "nt":
            launcher = shutil.which("py")
            base_python = launcher or shutil.which("python")
        else:
            base_python = shutil.which("python3")
        base_python = base_python or sys.executable
        if not base_python:
            raise SystemExit("未找到 Python。请先安装 Python 3.12，并勾选 Add Python to PATH。")
        if os.name == "nt" and Path(base_python).name.lower() == "py.exe":
            command = [base_python, "-3.12", "-m", "venv", str(VENV)]
        else:
            command = [base_python, "-m", "venv", str(VENV)]
        subprocess.check_call(command, cwd=ROOT)
    subprocess.check_call([str(PYTHON), "-m", "pip", "install", "-r", "requirements.txt"], cwd=ROOT)


def _ensure_cloudflared() -> None:
    if os.name != "nt":
        raise SystemExit("本机 Aspen 连接器必须运行在 Windows。")
    if CLOUDFLARED.exists():
        return
    url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
    request = urllib.request.Request(url, headers={"User-Agent": "PropertyDataAgent/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, CLOUDFLARED.open("wb") as output:
            shutil.copyfileobj(response, output)
    except Exception as exc:
        raise SystemExit(f"无法自动下载安全隧道程序：{type(exc).__name__}。请检查 GitHub 网络连接。") from exc
    if CLOUDFLARED.stat().st_size < 1_000_000:
        CLOUDFLARED.unlink(missing_ok=True)
        raise SystemExit("安全隧道下载文件异常，已停止启动。")


def _start_tunnel() -> tuple[subprocess.Popen[str], str]:
    _ensure_cloudflared()
    tunnel = subprocess.Popen(
        [str(CLOUDFLARED), "tunnel", "--url", "http://127.0.0.1:8765", "--no-autoupdate"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    lines: queue.Queue[str] = queue.Queue()

    def read_output() -> None:
        assert tunnel.stdout is not None
        for line in tunnel.stdout:
            lines.put(line.rstrip())

    threading.Thread(target=read_output, name="cloudflared-log-reader", daemon=True).start()
    deadline = time.monotonic() + 90
    public_url = ""
    while time.monotonic() < deadline:
        if tunnel.poll() is not None:
            raise SystemExit("HTTPS 隧道启动失败，请检查网络或校园网是否允许连接 Cloudflare。")
        try:
            line = lines.get(timeout=0.5)
        except queue.Empty:
            continue
        match = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line, flags=re.IGNORECASE)
        if match:
            public_url = match.group(0)
            break
    if not public_url:
        tunnel.terminate()
        raise SystemExit("90 秒内未能建立 HTTPS 隧道；请检查网络后重试。")
    return tunnel, public_url.rstrip("/")


def main() -> int:
    if os.name != "nt":
        raise SystemExit("此一键连接器需要在安装 Aspen Plus 的 Windows 电脑运行。")
    _ensure_python()
    env = os.environ.copy()
    env["ASPEN_MCP_HOST"] = "127.0.0.1"
    env["ASPEN_MCP_PORT"] = "8765"
    env["ASPEN_MCP_TOKEN"] = secrets.token_urlsafe(32)

    mcp = subprocess.Popen([str(PYTHON), "aspen_mcp_server.py"], cwd=ROOT, env=env)
    time.sleep(1.0)
    if mcp.poll() is not None:
        raise SystemExit("Aspen MCP 启动失败。请检查 Aspen、pywin32 和端口 8765。")
    tunnel = None
    try:
        print("正在为本机 Aspen 建立临时加密连接……")
        tunnel, public_mcp_url = _start_tunnel()
        query = urlencode({
            "aspen_mcp_url": public_mcp_url,
            "aspen_mcp_token": env["ASPEN_MCP_TOKEN"],
            "aspen_open": "1",
        })
        online_url = ONLINE_APP_URL + ("&" if "?" in ONLINE_APP_URL else "?") + query
        print("本机 Aspen 连接已启动；正在打开线上物性数据网页。")
        print("保持本窗口开启，关闭后本机 Aspen 连接会自动断开。")
        webbrowser.open(online_url)
        while mcp.poll() is None and tunnel.poll() is None:
            time.sleep(1)
        return 1 if mcp.poll() is not None or tunnel.poll() is not None else 0
    finally:
        if tunnel is not None and tunnel.poll() is None:
            tunnel.terminate()
        mcp.terminate()
        try:
            mcp.wait(timeout=8)
        except subprocess.TimeoutExpired:
            mcp.kill()


if __name__ == "__main__":
    raise SystemExit(main())
