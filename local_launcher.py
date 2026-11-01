"""一键启动 Windows 本机 Aspen MCP 与物性数据网页。

用户只需先打开 Aspen Plus，再运行本脚本；令牌和本机地址在进程内自动
生成，不要求手动填写 Render 环境变量。网页和 Aspen MCP 始终绑定到同一
台电脑的回环地址，适合每位 Aspen 用户独立运行自己的网页实例。
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path


ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
PYTHON = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _ensure_python() -> None:
    if not PYTHON.exists():
        base_python = shutil.which("py") if os.name == "nt" else shutil.which("python3")
        if not base_python:
            raise SystemExit("未找到 Python。请先安装 Python 3.12，并勾选 Add Python to PATH。")
        command = [base_python, "-3.12", "-m", "venv", str(VENV)] if os.name == "nt" else [base_python, "-m", "venv", str(VENV)]
        subprocess.check_call(command, cwd=ROOT)
    subprocess.check_call([str(PYTHON), "-m", "pip", "install", "--upgrade", "pip"], cwd=ROOT)
    subprocess.check_call([str(PYTHON), "-m", "pip", "install", "-r", "requirements.txt"], cwd=ROOT)


def main() -> int:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8501/_stcore/health", timeout=1) as response:
            if response.status == 200:
                webbrowser.open("http://127.0.0.1:8501")
                print("网页已经在运行，已打开现有页面。")
                return 0
    except Exception:
        pass
    _ensure_python()
    env = os.environ.copy()
    env["ASPEN_MCP_HOST"] = "127.0.0.1"
    env["ASPEN_MCP_PORT"] = "8765"
    env["ASPEN_MCP_TOKEN"] = secrets.token_urlsafe(32)

    mcp = subprocess.Popen([str(PYTHON), "aspen_mcp_server.py"], cwd=ROOT, env=env)
    time.sleep(1.0)
    if mcp.poll() is not None:
        raise SystemExit("Aspen MCP 启动失败。请检查 Aspen、pywin32 和端口 8765。")

    app_env = env.copy()
    app_env["ASPEN_MCP_URL"] = "http://127.0.0.1:8765"
    print("本机 Aspen 自动连接模式已启动。")
    print("请在 Aspen Plus 中保持已成功运行的 BKP 打开，然后访问 http://127.0.0.1:8501")
    app = subprocess.Popen(
        [
            str(PYTHON), "-m", "streamlit", "run", "app.py",
            "--server.address", "127.0.0.1", "--server.port", "8501",
            "--server.headless", "true", "--browser.gatherUsageStats", "false",
        ],
        cwd=ROOT,
        env=app_env,
    )
    time.sleep(2.0)
    if app.poll() is not None:
        raise SystemExit("网页进程启动失败。请重新运行启动器并查看窗口中的错误信息。")
    webbrowser.open("http://127.0.0.1:8501")
    try:
        return app.wait()
    finally:
        if app.poll() is None:
            app.terminate()
        mcp.terminate()
        try:
            mcp.wait(timeout=8)
        except subprocess.TimeoutExpired:
            mcp.kill()


if __name__ == "__main__":
    raise SystemExit(main())
