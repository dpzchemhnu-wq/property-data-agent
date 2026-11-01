"""MCP-compatible Aspen Plus tool server.

The server runs on the Windows machine that owns Aspen Plus.  It exposes only
typed, read-only tools over JSON-RPC 2.0; no model supplied code is executed.
"""

from __future__ import annotations

import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from aspen_connector import AspenConnectionError, read_case_streams, scan_open_cases


PROTOCOL_VERSION = "2024-11-05"


TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_aspen_cases",
        "description": "列出当前 Windows 用户会话中已打开且可检查的 Aspen Plus BKP 案例。只返回状态、错误数和流股数量，不修改案例。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "read_aspen_streams",
        "description": "读取一个已停止计算、错误数为 0 的 Aspen Plus 案例的全部流股结果。",
        "inputSchema": {
            "type": "object",
            "properties": {"case_id": {"type": "string", "pattern": "^[0-9a-f]{20}$"}},
            "required": ["case_id"],
            "additionalProperties": False,
        },
    },
]


def _jsonrpc_response(request_id: Any, result: Any = None, error: dict[str, Any] | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        response["error"] = error
    else:
        response["result"] = result
    return response


def _tool_result(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "structuredContent": payload,
        "isError": payload.get("status") == "error",
    }


def dispatch(request: dict[str, Any]) -> dict[str, Any] | None:
    request_id = request.get("id")
    method = request.get("method")
    if not isinstance(method, str):
        return _jsonrpc_response(request_id, error={"code": -32600, "message": "无效的 JSON-RPC 请求。"})
    if method == "notifications/initialized" or method.startswith("notifications/"):
        return None
    if method == "initialize":
        return _jsonrpc_response(request_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "property-agent-aspen", "version": "1.0.0"},
        })
    if method == "ping":
        return _jsonrpc_response(request_id, {})
    if method == "tools/list":
        return _jsonrpc_response(request_id, {"tools": TOOLS})
    if method != "tools/call":
        return _jsonrpc_response(request_id, error={"code": -32601, "message": f"不支持的方法：{method}"})

    params = request.get("params")
    if not isinstance(params, dict) or not isinstance(params.get("name"), str):
        return _jsonrpc_response(request_id, error={"code": -32602, "message": "tools/call 缺少工具名称。"})
    name = params["name"]
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        return _jsonrpc_response(request_id, error={"code": -32602, "message": "工具参数必须是 JSON 对象。"})
    try:
        if name == "list_aspen_cases":
            if arguments:
                raise ValueError("list_aspen_cases 不接受参数。")
            return _jsonrpc_response(request_id, _tool_result(scan_open_cases()))
        if name == "read_aspen_streams":
            case_id = arguments.get("case_id")
            if not isinstance(case_id, str) or len(case_id) != 20 or any(char not in "0123456789abcdef" for char in case_id):
                raise ValueError("case_id 必须是扫描结果中的 20 位十六进制标识。")
            return _jsonrpc_response(request_id, _tool_result(read_case_streams(case_id)))
        return _jsonrpc_response(request_id, error={"code": -32602, "message": f"未知工具：{name}"})
    except ValueError as exc:
        return _jsonrpc_response(request_id, error={"code": -32602, "message": str(exc)})
    except AspenConnectionError as exc:
        return _jsonrpc_response(request_id, error={"code": -32001, "message": str(exc), "data": {"status": "aspen_connection_error"}})
    except Exception as exc:
        return _jsonrpc_response(request_id, error={"code": -32000, "message": f"Aspen 工具执行失败：{type(exc).__name__}"})


class _MCPHandler(BaseHTTPRequestHandler):
    server_version = "PropertyAgentAspenMCP/1.0"

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/health":
            self._send(200, {"status": "ok", "service": "property-agent-aspen-mcp", "protocol": PROTOCOL_VERSION})
            return
        self._send(404, {"status": "error", "message": "MCP 服务只接受 /mcp 的 JSON-RPC 请求。"})

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/mcp":
            self._send(404, {"status": "error", "message": "未找到 MCP 端点。"})
            return
        expected = str(getattr(self.server, "mcp_token", ""))
        supplied = self.headers.get("Authorization", "")
        if not expected or not hmac.compare_digest(supplied, f"Bearer {expected}"):
            self._send(401, {"status": "error", "message": "MCP 令牌无效。"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 2_000_000:
                raise ValueError("请求体为空或超过 2 MB 限制。")
            request = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("JSON-RPC 请求必须是对象。")
            response = dispatch(request)
            if response is None:
                self.send_response(202)
                self.end_headers()
            else:
                self._send(200, response)
        except (ValueError, json.JSONDecodeError) as exc:
            self._send(400, _jsonrpc_response(None, error={"code": -32700, "message": f"无效请求：{exc}"}))


def main() -> None:
    token = os.getenv("ASPEN_MCP_TOKEN", "").strip()
    if len(token) < 32:
        raise SystemExit("ASPEN_MCP_TOKEN 必须至少 32 个字符；请使用随机高强度令牌。")
    host = os.getenv("ASPEN_MCP_HOST", "127.0.0.1")
    port = int(os.getenv("ASPEN_MCP_PORT", "8765"))
    server = ThreadingHTTPServer((host, port), _MCPHandler)
    server.mcp_token = token
    print(f"Aspen MCP 服务已启动：{host}:{port}/mcp")
    print("仅开放 list_aspen_cases/read_aspen_streams 两个只读工具；要扫描案例，Aspen Plus 必须已安装并在当前 Windows 用户会话打开。")
    server.serve_forever()


if __name__ == "__main__":
    main()
