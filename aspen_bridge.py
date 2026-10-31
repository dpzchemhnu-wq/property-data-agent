"""Authenticated read-only HTTP bridge for a Windows host running Aspen Plus."""

from __future__ import annotations

import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from aspen_connector import AspenConnectionError, read_case_streams, scan_open_cases


class _BridgeHandler(BaseHTTPRequestHandler):
    server_version = "PropertyAgentAspenBridge/1.0"

    def log_message(self, format: str, *args: object) -> None:
        # Avoid logging bearer tokens or Aspen file paths from request details.
        return

    def _send(self, status: int, payload: dict[str, object]) -> None:
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        route = urlparse(self.path).path.rstrip("/") or "/"
        if route == "/health":
            self._send(200, {"status": "ok", "service": "aspen-read-only-bridge"})
            return
        expected = str(getattr(self.server, "bridge_token", ""))
        supplied = self.headers.get("Authorization", "")
        if not expected or not hmac.compare_digest(supplied, f"Bearer {expected}"):
            self._send(401, {"status": "error", "message": "未授权的桥接请求。"})
            return
        try:
            if route == "/api/cases":
                self._send(200, scan_open_cases())
                return
            prefix, suffix = "/api/cases/", "/streams"
            if route.startswith(prefix) and route.endswith(suffix):
                case_id = unquote(route[len(prefix):-len(suffix)])
                self._send(200, read_case_streams(case_id))
                return
            self._send(404, {"status": "error", "message": "未找到桥接接口。"})
        except AspenConnectionError as exc:
            self._send(503, {"status": "error", "message": str(exc)})
        except Exception as exc:
            self._send(500, {"status": "error", "message": f"桥接读取失败：{type(exc).__name__}"})


def main() -> None:
    token = os.getenv("ASPEN_BRIDGE_TOKEN", "").strip()
    if len(token) < 32:
        raise SystemExit("ASPEN_BRIDGE_TOKEN 必须至少 32 个字符；请使用随机高强度令牌。")
    host = os.getenv("ASPEN_BRIDGE_HOST", "127.0.0.1")
    port = int(os.getenv("ASPEN_BRIDGE_PORT", "8765"))
    server = ThreadingHTTPServer((host, port), _BridgeHandler)
    server.bridge_token = token
    print(f"Aspen 只读桥接服务已启动：{host}:{port}")
    print("仅允许 GET 查询；案例与流股读取由当前 Windows 用户会话中的 Aspen COM 提供。")
    server.serve_forever()


if __name__ == "__main__":
    main()
