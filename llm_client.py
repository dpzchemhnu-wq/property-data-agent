"""OpenAI Chat Completions 兼容客户端与受限工具调用循环。

这里仅传输聊天消息和结构化工具调用。物性数值始终由本地 Python 工具计算，
不会采用模型在普通文本中自行生成的数值。
"""

from __future__ import annotations

import json
from typing import Any, Callable

import requests

from tools import TARGET_PROPERTIES, query_properties, query_properties_tool_schema


SYSTEM_PROMPT = """你是化工物性数据 Agent 的意图解析与工具调度员，请使用中文。
你不得计算、估计、改写或编造物性数值；只能从 Python 工具返回结果取得数据，网页将直接展示工具结果。
必须严格保持用户提到的组分先后顺序，摩尔分数也按相同顺序返回。
你负责把自然语言中的单位与表达转换为工具要求的 SI 数值：温度统一为 K（摄氏度 t 转成 t+273.15，华氏度 F 转成 (F-32)*5/9+273.15）；压力统一为 Pa（kPa、MPa、bar、atm 按精确单位换算）；比例统一为摩尔分数。
“甲醇与甲苯比例 5/1”“5:1”“5比1”在没有明确说明质量比时，按摩尔比解释并归一化为 [5/6, 1/6]，顺序必须对应用户列出的组分；用户明确说质量比、质量分数或体积比时，不可当作摩尔比，必须追问或说明当前工具需要摩尔组成。百分数摩尔分数可除以 100；已归一化的摩尔分数原样提取。
“常温”不等于明确温度，不得自行指定；“常压”“大气压”按 101325 Pa 解释。未明确的温度、组成或其他条件不得猜测，应使用中文询问缺少的具体信息，不调用工具。
用户要求“全套流程设计所需物性”时，请请求全部支持的物性；只要求部分物性时仅请求对应项。
名称参数优先使用后端可验证的英文通用名/IUPAC 名称或 CAS RN。若用户输入中文名称且后端别名目录未必收录，可将该中文名翻译为准确的英文通用名（例如“乙苯”→“ethylbenzene”）再交给工具解析；只能做名称翻译，不能替换成相似物质。若中文名称本身有异构体/俗名歧义，必须先追问用户，不能自行选择。
仅当参数足够时调用 query_properties；若工具返回 needs_input/out_of_domain/solver_failed，原样说明缺项或失败原因，不生成替代结果。"""
MAX_TOOL_ROUNDS = 4


class LLMServiceError(RuntimeError):
    """带有用户可操作诊断信息、且不包含请求密钥的服务错误。"""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _chat_endpoint(base_url: str) -> str:
    url = base_url.strip().rstrip("/")
    if not url:
        raise LLMServiceError("模型接口地址为空。")
    return url if url.endswith("/chat/completions") else f"{url}/chat/completions"


def _response_detail(response: requests.Response, api_key: str) -> str:
    """优先提取供应商 JSON 错误说明，并遮蔽可能回显的密钥。"""
    try:
        body = response.json()
        error = body.get("error", body) if isinstance(body, dict) else body
        if isinstance(error, dict):
            detail = str(error.get("message") or error.get("detail") or error)
        else:
            detail = str(error)
    except (ValueError, json.JSONDecodeError):
        detail = response.text or "服务未提供错误详情。"
    if api_key:
        detail = detail.replace(api_key, "[API Key 已隐藏]")
    detail = detail.replace("Bearer ", "Bearer [已隐藏] ")
    return " ".join(detail.split())[:1200]


def list_models(config: dict[str, str], *, timeout_s: float = 30) -> list[str]:
    """读取供应商模型目录；使用与 Chat Completions 相同的 base URL 和鉴权。"""
    api_key = config.get("api_key", "").strip()
    base_url = config.get("base_url", "").strip().rstrip("/")
    if not base_url:
        raise LLMServiceError("模型接口地址为空。")
    endpoint = base_url[:-17] + "/models" if base_url.endswith("/chat/completions") else f"{base_url}/models"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        response = requests.get(endpoint, headers=headers, timeout=timeout_s)
    except requests.Timeout as exc:
        raise LLMServiceError(f"读取模型目录超时（{timeout_s:g} 秒）。") from exc
    except requests.ConnectionError as exc:
        raise LLMServiceError("无法连接模型目录接口。请检查接口地址和网络。") from exc
    except requests.RequestException as exc:
        raise LLMServiceError(f"读取模型目录失败：{exc.__class__.__name__}。") from exc
    if not response.ok:
        detail = _response_detail(response, api_key)
        raise LLMServiceError(f"读取模型目录返回 HTTP {response.status_code}。详情：{detail}", response.status_code)
    try:
        body = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise LLMServiceError("模型目录返回的内容不是有效 JSON。") from exc
    model_ids: set[str] = set()

    def collect(node: Any, depth: int = 0) -> None:
        """接受常见的 OpenAI 及中转服务模型目录字段布局。"""
        if depth > 5:
            return
        if isinstance(node, list):
            for item in node:
                if isinstance(item, (dict, list)):
                    collect(item, depth + 1)
                elif isinstance(item, str) and item.strip():
                    model_ids.add(item.strip())
            return
        if not isinstance(node, dict):
            return
        for field in ("id", "model", "model_id", "model_name"):
            value = node.get(field)
            if isinstance(value, str) and value.strip():
                model_ids.add(value.strip())
        for field in ("data", "models", "items", "result", "model_list", "list"):
            if field in node:
                collect(node[field], depth + 1)

    collect(body)
    models = sorted(model_ids)
    if not models:
        top_keys = ", ".join(sorted(str(key) for key in body.keys())) if isinstance(body, dict) else type(body).__name__
        data_type = type(body.get("data")).__name__ if isinstance(body, dict) and "data" in body else "missing"
        raise LLMServiceError(
            "模型目录接口返回 HTTP 200，但响应中没有可识别的模型 ID。"
            f"响应顶层字段：{top_keys or '无'}；data 字段类型：{data_type}。"
            "这通常表示供应商未通过目录接口公开模型，或当前 API Key 没有模型权限。"
            "请从供应商控制台复制已开通的精确模型 ID，填入上方‘模型名称’，然后点‘测试模型连接’。"
        )
    return models


def chat_completion(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    config: dict[str, str],
    *,
    timeout_s: float = 60,
) -> dict[str, Any]:
    """发送一次 OpenAI Chat Completions 兼容请求，提供可定位的错误信息。"""
    api_key = config.get("api_key", "").strip()
    base_url = config.get("base_url", "").strip()
    model = config.get("model", "").strip()
    if not model:
        raise LLMServiceError("模型名称未填写。")
    endpoint = _chat_endpoint(base_url)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        response = requests.post(
            endpoint,
            headers=headers,
            json={"model": model, "messages": messages, "tools": tools, "tool_choice": "auto", "temperature": 0},
            timeout=timeout_s,
        )
    except requests.Timeout as exc:
        raise LLMServiceError(f"连接模型服务超时（{timeout_s:g} 秒）。请检查服务是否可访问及接口地址。") from exc
    except requests.ConnectionError as exc:
        raise LLMServiceError("无法连接模型服务。请检查服务是否启动、主机名和端口是否正确。") from exc
    except requests.RequestException as exc:
        raise LLMServiceError(f"模型服务网络请求失败：{exc.__class__.__name__}。") from exc

    if not response.ok:
        detail = _response_detail(response, api_key)
        hints = {
            400: "常见原因：模型不支持工具调用、请求格式不兼容或模型名错误。",
            401: "请检查 API Key 是否有效。",
            403: "服务拒绝访问；请检查账户权限、模型授权或网络策略。",
            404: "请检查 BASE_URL 是否应包含 /v1，以及模型名/接口路径是否正确。",
            429: "服务限流或额度不足；请检查供应商配额后重试。",
            503: "该模型当前没有可用渠道，通常是模型名不在账号目录或供应商暂时下线；请先获取模型目录并复制准确 ID。",
        }
        hint = hints.get(response.status_code, "请结合服务端错误详情检查供应商状态与请求参数。")
        raise LLMServiceError(f"模型服务返回 HTTP {response.status_code}。{hint} 详情：{detail}", response.status_code)
    try:
        body = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise LLMServiceError("模型服务返回的内容不是有效 JSON。") from exc
    choices = body.get("choices") if isinstance(body, dict) else None
    if not choices:
        raise LLMServiceError("服务已响应，但响应中缺少 choices；该接口可能不是 Chat Completions 格式。")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise LLMServiceError("服务响应缺少 choices[0].message，接口格式不兼容。")
    return message


def test_tool_call_connection(config: dict[str, str]) -> tuple[bool, str]:
    """验证接口连通性和 Tool Calling 能力，不运行物性工具。"""
    probe_schema = [{
        "type": "function",
        "function": {
            "name": "connection_probe",
            "description": "只用于验证工具调用接口。",
            "parameters": {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
        },
    }]
    try:
        message = chat_completion(
            [
                {"role": "system", "content": "请调用 connection_probe 工具并传入 ok=true。不要只用文字回答。"},
                {"role": "user", "content": "检查工具调用连接。"},
            ],
            probe_schema,
            config,
            timeout_s=30,
        )
    except LLMServiceError as exc:
        return False, str(exc)
    calls = message.get("tool_calls") or []
    legacy_call = message.get("function_call")
    if any((call.get("function") or {}).get("name") == "connection_probe" for call in calls):
        return True, "连接成功，且模型返回了兼容的 Tool Calling 结构。"
    if isinstance(legacy_call, dict) and legacy_call.get("name") == "connection_probe":
        return True, "连接成功；供应商使用旧版 Function Calling 格式，主对话可尝试兼容处理。"
    return False, "HTTP 接口可访问，但模型没有返回 connection_probe 工具调用；请检查所选模型是否支持 Tool Calling。"


def _normalize_tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    """兼容当前 tool_calls 与旧版 function_call 两种常见响应格式。"""
    calls = message.get("tool_calls")
    if isinstance(calls, list):
        return calls
    legacy = message.get("function_call")
    if isinstance(legacy, dict):
        return [{"id": "legacy-function-call", "function": legacy}]
    return []


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return ""


def _explicit_target_properties(user_text: str) -> list[str] | None:
    """若用户明确点名物性，则用规则结果约束模型，防止把单项查询扩成全套。"""
    text = user_text.casefold()
    all_terms = ("全套", "全部物性", "所有物性", "完整数据包", "全套物性", "all properties", "complete property set", "full property package")
    if any(term in text for term in all_terms):
        return list(TARGET_PROPERTIES)
    terms = {
        "density": ("密度", "density"),
        "enthalpy": ("焓", "enthalpy"),
        "viscosity": ("粘度", "黏度", "viscosity"),
        "bubble_point": ("泡点", "bubble point", "bubble-point"),
        "surface_tension": ("表面张力", "surface tension"),
        "thermal_conductivity": ("热导率", "导热系数", "thermal conductivity"),
    }
    selected = [key for key, aliases in terms.items() if any(alias in text for alias in aliases)]
    return selected or None


def dispatch_tool(name: str, raw_arguments: str | dict[str, Any]) -> dict[str, Any]:
    """只运行白名单中的本地 Python 工具，不执行模型生成代码。"""
    if name != "query_properties":
        return {"status": "needs_input", "message": f"未注册工具：{name}"}
    try:
        arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
        if not isinstance(arguments, dict):
            raise ValueError("arguments 必须是 JSON 对象。")
        return query_properties(
            components=arguments.get("components"),
            mole_fractions=arguments.get("mole_fractions"),
            T_K=arguments.get("T_K"),
            P_Pa=arguments.get("P_Pa"),
            target_properties=arguments.get("target_properties"),
        )
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        return {"status": "needs_input", "message": f"工具参数 JSON 无效：{exc}"}


def run_tool_loop(
    user_text: str,
    history: list[dict[str, Any]],
    config: dict[str, str],
    *,
    property_query: Callable[..., dict[str, Any]] = query_properties,
    max_rounds: int = MAX_TOOL_ROUNDS,
) -> tuple[list[dict[str, Any]], str]:
    """执行工具调用；非 ok 状态立即停止，不再让模型尝试补造参数/结果。"""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *history[-8:],
        {"role": "user", "content": user_text},
    ]
    tool_schema = [query_properties_tool_schema()]
    explicit_targets = _explicit_target_properties(user_text)
    results: list[dict[str, Any]] = []
    for _ in range(max_rounds):
        assistant_message = chat_completion(messages, tool_schema, config)
        calls = _normalize_tool_calls(assistant_message)
        if not calls:
            return results, _content_text(assistant_message.get("content")) or "请补充查询所需的组分、摩尔分数、温度、压力和目标物性。"
        messages.append(assistant_message)
        for call in calls:
            function = call.get("function") or {}
            name = str(function.get("name", ""))
            result = _dispatch_injected(name, function.get("arguments", "{}"), property_query, explicit_targets)
            results.append(result)
            messages.append({
                "role": "tool", "tool_call_id": call.get("id", "legacy-function-call"),
                "name": name, "content": json.dumps(result, ensure_ascii=False),
            })
            if result.get("status") != "ok":
                return results, str(result.get("message", "工具调用未成功。"))
        # 当前只注册 query_properties；成功后直接展示可信工具结果，不做多余的第二次 LLM 请求。
        return results, "已完成后端物性查询；数值和来源见结果卡片。"
    return results, "工具调用达到安全轮数上限；请检查模型是否在重复调用工具。"


def _dispatch_injected(
    name: str,
    raw_arguments: str | dict[str, Any],
    property_query: Callable[..., dict[str, Any]],
    explicit_targets: list[str] | None = None,
) -> dict[str, Any]:
    """依赖注入只供测试替身；仍校验白名单和 JSON 参数形状。"""
    if name != "query_properties":
        return {"status": "needs_input", "message": f"未注册工具：{name}"}
    try:
        arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
        if not isinstance(arguments, dict):
            raise ValueError("arguments 必须是 JSON 对象。")
        if explicit_targets:
            arguments["target_properties"] = explicit_targets
        return property_query(
            components=arguments.get("components"),
            mole_fractions=arguments.get("mole_fractions"),
            T_K=arguments.get("T_K"), P_Pa=arguments.get("P_Pa"),
            target_properties=arguments.get("target_properties"),
        )
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        return {"status": "needs_input", "message": f"工具参数 JSON 无效：{exc}"}
