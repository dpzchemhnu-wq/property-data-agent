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
STATUS_FILE = ROOT / "connection_status.txt"


def _status(message: str) -> None:
    """Keep a short non-secret diagnostic file for users whose console closes."""
    print(message, flush=True)
    try:
        STATUS_FILE.write_text(message + "\n", encoding="utf-8")
    except OSError:
        pass


def _python312_command() -> list[str] | None:
    """Return a verified Python 3.10+ executable command; never fall back to old Python."""
    candidates: list[list[str]] = []
    launcher = shutil.which("py")
    if launcher:
        candidates.append([launcher, "-3.12"])
    path_python = shutil.which("python")
    if path_python:
        candidates.append([path_python])
    for candidate in (
        Path(os.getenv("LOCALAPPDATA", "")) / "Programs/Python/Python312/python.exe",
        Path(r"C:\Program Files\Python312\python.exe"),
        Path(r"C:\Python312\python.exe"),
    ):
        if str(candidate) and candidate.exists():
            candidates.append([str(candidate)])
    for command in candidates:
        try:
            probe = subprocess.run(
                [*command, "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
                capture_output=True, text=True, check=True, timeout=10,
            )
            version = tuple(int(part) for part in probe.stdout.strip().split(".")[:2])
            if version >= (3, 10):
                return command
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
    return None


def _ensure_python() -> None:
    if not PYTHON.exists():
        base_python = _python312_command()
        if not base_python:
            raise SystemExit("未找到可用的 Python 3.10+。请先安装 Python 3.12，再重新运行此连接器。")
        try:
            subprocess.check_call([*base_python, "-m", "venv", str(VENV)], cwd=ROOT)
        except subprocess.CalledProcessError as exc:
            raise SystemExit("Python 3.12 虚拟环境创建失败；请检查 Python 安装和文件夹权限。") from exc
    try:
        probe = subprocess.run(
            [str(PYTHON), "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
            capture_output=True, text=True, check=True, timeout=10,
        )
        version = tuple(int(part) for part in probe.stdout.strip().split(".")[:2])
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise SystemExit("项目虚拟环境中的 Python 无法启动；请删除本机 PropertyDataAgent 文件夹后重新运行 BAT。") from exc
    if version < (3, 10):
        raise SystemExit("项目虚拟环境中的 Python 版本过旧；请删除本机 PropertyDataAgent 文件夹后重新运行 BAT。")
    try:
        subprocess.check_call(
            [str(PYTHON), "-m", "pip", "install", "--disable-pip-version-check", "-r", "requirements.txt"],
            cwd=ROOT,
        )
    except subprocess.CalledProcessError as exc:
        raise SystemExit("项目依赖安装失败；请检查网络连接后重新运行 BAT。") from exc


def _wait_for_mcp(process: subprocess.Popen[str], timeout_s: float = 15.0) -> None:
    """Wait for the local bridge before starting a tunnel, so failures are actionable."""
    health_url = "http://127.0.0.1:8765/health"
    deadline = time.monotonic() + timeout_s
    last_error = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit("本机 Aspen 连接服务启动失败。请检查 Python 依赖、端口 8765 是否被占用。")
        try:
            request = urllib.request.Request(health_url, headers={"User-Agent": "PropertyDataAgent/1.0"})
            with urllib.request.urlopen(request, timeout=2) as response:
                if response.status == 200:
                    return
        except Exception as exc:
            last_error = type(exc).__name__
        time.sleep(0.5)
    raise SystemExit(f"本机连接服务未在规定时间内启动（{last_error or 'timeout'}）。")


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


def _wait_for_public_mcp(public_url: str, timeout_s: float = 20.0) -> None:
    """Confirm the temporary HTTPS route is live before sending a pairing URL."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(public_url + "/health", timeout=4) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.8)
    raise SystemExit("HTTPS 隧道地址已生成，但暂时无法访问本机连接服务；请检查网络、防火墙或校园网策略。")


def main() -> int:
    if os.name != "nt":
        raise SystemExit("此一键连接器需要在安装 Aspen Plus 的 Windows 电脑运行。")
    _ensure_python()
    env = os.environ.copy()
    env["ASPEN_MCP_HOST"] = "127.0.0.1"
    env["ASPEN_MCP_PORT"] = "8765"
    env["ASPEN_MCP_TOKEN"] = secrets.token_urlsafe(32)

    mcp = subprocess.Popen([str(PYTHON), "aspen_mcp_server.py"], cwd=ROOT, env=env)
    tunnel = None
    try:
        _wait_for_mcp(mcp)
        _status("连接器可以先与线上网页配对；是否安装 Aspen 会在点击扫描时单独检查。")
        _status("正在为本机 Aspen 建立临时加密连接……")
        tunnel, public_mcp_url = _start_tunnel()
        _status("正在验证 HTTPS 隧道可访问性……")
        _wait_for_public_mcp(public_mcp_url)
        query = urlencode({
            "aspen_mcp_url": public_mcp_url,
            "aspen_mcp_token": env["ASPEN_MCP_TOKEN"],
            "aspen_open": "1",
        })
        online_url = ONLINE_APP_URL + ("&" if "?" in ONLINE_APP_URL else "?") + query
        _status("本机连接已启动；正在复用现有浏览器窗口或打开线上物性数据网页。")
        _status("保持本窗口开启，关闭后本机 Aspen 连接会自动断开。")
        # new=0 请求默认浏览器复用现有窗口；若没有可复用窗口，再由浏览器打开。
        # 浏览器安全模型不允许脚本保证替换某个指定标签页。
        try:
            if os.name == "nt":
                os.startfile(online_url)  # type: ignore[attr-defined]
            else:
                webbrowser.open(online_url, new=0)
        except OSError:
            webbrowser.open(online_url, new=0)
        try:
            subprocess.run(["clip"], input=online_url, text=True, check=False, timeout=3)
            _status("配对网址已复制到剪贴板；若浏览器未自动打开，可粘贴该网址到浏览器。")
        except (OSError, subprocess.SubprocessError):
            _status(f"请手动打开下面的配对网址：\n{online_url}")
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
    try:
        raise SystemExit(main())
    except SystemExit as exc:
        if exc.code not in (None, 0):
            _status(f"连接器未完成：{exc}")
        raise
    except Exception as exc:
        _status(f"连接器未完成：{type(exc).__name__}: {exc}")
        raise
