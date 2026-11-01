"""Read-only Aspen Plus COM integration, plus an optional remote bridge client."""

from __future__ import annotations

import hashlib
import importlib.util
import math
import os
import queue
import re
import threading
import tempfile
from typing import Any, Callable
from urllib.parse import quote

import requests


ERROR_COUNT_PATH = os.getenv(
    "ASPEN_ERROR_COUNT_PATH",
    r"\Data\Results Summary\Run-Status\Output\PER_ERROR",
)
TEMPERATURE_PATHS = ("TEMP_OUT\\MIXED", "TEMP\\MIXED")
PRESSURE_PATHS = ("PRES_OUT\\MIXED", "PRES\\MIXED")
FLOW_PATHS = ("MOLEFLMX\\MIXED", "MOLEFLOW\\MIXED", "MOLARFLMX\\MIXED")
VAPOR_FRACTION_PATHS = ("VFRAC\\MIXED", "VFRAC_OUT\\MIXED")
MOLE_FRACTION_PATHS = ("MOLEFRAC\\MIXED", "MOLEFRAC_OUT\\MIXED")


class AspenConnectionError(RuntimeError):
    """Connection or active-document enumeration failed safely."""


def _sta_call(callback: Callable[[], Any]) -> Any:
    """Run a complete COM operation on an isolated single-threaded apartment."""
    result_queue: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            import pythoncom

            pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
            try:
                result_queue.put((True, callback()))
            finally:
                pythoncom.CoUninitialize()
        except Exception as exc:
            result_queue.put((False, exc))

    thread = threading.Thread(target=worker, name="AspenCOM-STA", daemon=True)
    thread.start()
    thread.join(timeout=float(os.getenv("ASPEN_COM_TIMEOUT_SECONDS", "45")))
    if thread.is_alive():
        raise AspenConnectionError("Aspen COM 请求超时；未返回案例数据。")
    ok, value = result_queue.get_nowait()
    if not ok:
        raise AspenConnectionError(f"Aspen COM 请求失败：{type(value).__name__}: {value}") from value
    return value


def _collection_items(collection: Any) -> list[Any]:
    try:
        count = int(collection.Count)
    except Exception:
        return []
    result: list[Any] = []
    for index in range(count):
        item = None
        for candidate in (index, index + 1):
            try:
                item = collection.Item(candidate)
                break
            except Exception:
                continue
        if item is None:
            continue
        result.append(item)
    return result


def _document_path(document: Any) -> str:
    for attribute in ("FullName", "FileName", "Filename", "DocumentName"):
        try:
            value = str(getattr(document, attribute)).strip()
        except Exception:
            continue
        if value.lower().endswith(".bkp"):
            return os.path.abspath(value)
    return ""


def _engine_is_running(document: Any) -> bool | None:
    try:
        return bool(document.Engine.IsRunning)
    except Exception:
        return None


def _case_id(moniker: str, path: str) -> str:
    return hashlib.sha256(f"{moniker}\0{path}".encode("utf-8")).hexdigest()[:20]


def _running_documents() -> list[tuple[str, str, Any]]:
    if os.name != "nt":
        raise AspenConnectionError("Aspen Plus COM 只能从 Windows 主机访问。")
    if importlib.util.find_spec("pythoncom") is None or importlib.util.find_spec("win32com") is None:
        raise AspenConnectionError("当前 Python 环境未安装 pywin32；请在 Aspen 主机安装 requirements.txt 中的 Windows 依赖。")

    import pythoncom
    import win32com.client

    objects: list[tuple[str, str, Any]] = []
    identities: set[tuple[str, str]] = set()
    rot = pythoncom.GetRunningObjectTable()
    context = pythoncom.CreateBindCtx(0)
    enumerator = rot.EnumRunning()
    try:
        monikers = list(enumerator)
    except Exception:
        monikers = []
        while True:
            try:
                item = enumerator.Next(1)
                if not item:
                    break
                monikers.extend(item if isinstance(item, tuple) else [item])
            except Exception:
                break

    for moniker in monikers:
        try:
            display_name = str(moniker.GetDisplayName(context, None))
        except Exception:
            continue
        if "apwn.document" not in display_name.lower():
            continue
        try:
            document = win32com.client.Dispatch(rot.GetObject(moniker))
            tree = document.Tree
            path = _document_path(document)
            if not path or not tree:
                continue
            identity = (display_name, path.casefold())
            if identity in identities:
                continue
            identities.add(identity)
            objects.append((display_name, path, document))
        except Exception:
            continue

    if not objects:
        # Some Aspen releases expose the active document by ProgID without a useful ROT name.
        for version in ("39.0", "38.0", "37.0", "36.0", "35.0", "34.0", "33.0", "32.0", "31.0"):
            progid = f"Apwn.Document.{version}"
            try:
                document = win32com.client.GetActiveObject(progid)
                path = _document_path(document)
                if path and document.Tree:
                    identity = (progid, path.casefold())
                    if identity not in identities:
                        identities.add(identity)
                        objects.append((progid, path, document))
            except Exception:
                continue
    return objects


def _unit_table(tree: Any) -> dict[tuple[int, int], str]:
    table: dict[tuple[int, int], str] = {}
    try:
        root = tree.FindNode(r"\Unit Table")
        for row in _collection_items(root.Elements):
            try:
                row_code = int(row.Value)
            except (TypeError, ValueError, AttributeError):
                continue
            for unit in _collection_items(row.Elements):
                try:
                    table[(row_code, int(unit.Value))] = str(unit.Name).strip()
                except (TypeError, ValueError, AttributeError):
                    continue
    except Exception:
        pass
    return table


def _node_unit(node: Any, units: dict[tuple[int, int], str]) -> str:
    try:
        row = int(node.AttributeValue("HAP_UNITROW"))
        col = int(node.AttributeValue("HAP_UNITCOL"))
        if (row, col) in units:
            return units[(row, col)]
    except Exception:
        pass
    try:
        return str(node.Units).strip()
    except Exception:
        return ""


def _number(node: Any, units: dict[tuple[int, int], str]) -> tuple[float | None, str]:
    if node is None:
        return None, ""
    try:
        value = float(node.Value)
    except (TypeError, ValueError, AttributeError):
        return None, _node_unit(node, units)
    if not math.isfinite(value):
        return None, _node_unit(node, units)
    return value, _node_unit(node, units)


def _find_node(tree: Any, base: str, candidates: tuple[str, ...]) -> Any:
    for candidate in candidates:
        try:
            node = tree.FindNode(base + "\\" + candidate)
            if node is not None:
                return node
        except Exception:
            continue
    return None


def _to_kelvin(value: float, unit: str) -> float | None:
    unit_key = re.sub(r"[^a-z]", "", unit.casefold())
    if unit_key in {"k", "kelvin", "degk"}:
        return value
    if unit_key in {"c", "degc", "celsius", "degreec"}:
        return value + 273.15
    if unit_key in {"f", "degf", "fahrenheit", "degreef"}:
        return (value - 32.0) * 5.0 / 9.0 + 273.15
    if unit_key in {"r", "degr", "rankine"}:
        return value * 5.0 / 9.0
    return None


def _to_pascal(value: float, unit: str) -> float | None:
    unit_key = re.sub(r"[^a-z]", "", unit.casefold())
    factors = {
        "pa": 1.0, "pascal": 1.0, "pascals": 1.0,
        "kpa": 1e3, "mpa": 1e6, "bar": 1e5, "bara": 1e5,
        "atm": 101325.0, "atmosphere": 101325.0, "atmospheres": 101325.0,
        "psi": 6894.757293168, "psia": 6894.757293168,
        "mbar": 100.0, "kbar": 1e8,
        "nsqm": 1.0, "nm2": 1.0, "kgfcm2": 98066.5, "kgcm2": 98066.5,
    }
    factor = factors.get(unit_key)
    return value * factor if factor is not None else None


def _to_kmol_h(value: float, unit: str) -> float | None:
    unit_key = re.sub(r"[^a-z]", "", unit.casefold())
    factors = {
        "kmolhr": 1.0, "kmolhour": 1.0, "kgmolhr": 1.0, "kgmolhour": 1.0,
        "kmolh": 1.0, "molhr": 1e-3, "molhour": 1e-3, "molh": 1e-3,
        "mols": 3.6, "molsec": 3.6, "kmols": 3600.0, "kmolsec": 3600.0,
        "lbmolhr": 0.45359237, "lbmolhour": 0.45359237, "lbmolmin": 27.2155422,
        "kgmolehr": 1.0, "kgmolehour": 1.0,
    }
    factor = factors.get(unit_key)
    return value * factor if factor is not None else None


def _read_error_count(tree: Any) -> int | None:
    paths = [ERROR_COUNT_PATH]
    for fallback in (
        r"\Data\Results Summary\Run-Status\Output\PER_ERROR",
        r"\Data\Results Summary\Run-Status\Output\ERROR_COUNT",
    ):
        if fallback not in paths:
            paths.append(fallback)
    for path in paths:
        try:
            node = tree.FindNode(path)
            value = float(node.Value) if node is not None else None
            if value is not None and value >= 0 and value.is_integer():
                return int(value)
        except (TypeError, ValueError, AttributeError):
            continue
        except Exception:
            continue
    return None


def _read_streams(document: Any, case_id: str) -> dict[str, Any]:
    tree = document.Tree
    base_node = tree.FindNode(r"\Data\Streams")
    if base_node is None:
        raise AspenConnectionError(r"所选 Aspen 案例中没有 \Data\Streams 节点。")
    units = _unit_table(tree)
    streams: list[dict[str, Any]] = []
    for stream_node in _collection_items(base_node.Elements):
        try:
            name = str(stream_node.Name).strip()
        except Exception:
            continue
        if not name or name.startswith(("#", "@")):
            continue
        output = rf"\Data\Streams\{name}\Output"
        temp_node = _find_node(tree, output, TEMPERATURE_PATHS)
        pressure_node = _find_node(tree, output, PRESSURE_PATHS)
        flow_node = _find_node(tree, output, FLOW_PATHS)
        temp, temp_unit = _number(temp_node, units)
        pressure, pressure_unit = _number(pressure_node, units)
        flow, flow_unit = _number(flow_node, units)
        T_K = _to_kelvin(temp, temp_unit) if temp is not None else None
        P_Pa = _to_pascal(pressure, pressure_unit) if pressure is not None else None
        flow_kmol_h = _to_kmol_h(flow, flow_unit) if flow is not None else None

        fraction_root = _find_node(tree, output, MOLE_FRACTION_PATHS)
        fractions_node = None
        if fraction_root is not None:
            try:
                fractions_node = fraction_root.FindNode("MIXED")
            except Exception:
                fractions_node = None
            if fractions_node is None:
                fractions_node = fraction_root
        components: list[str] = []
        mole_fractions: list[float] = []
        if fractions_node is not None:
            for component_node in _collection_items(fractions_node.Elements):
                try:
                    component = str(component_node.Name).strip()
                    fraction = float(component_node.Value)
                except (TypeError, ValueError, AttributeError):
                    continue
                if component and fraction >= 0:
                    components.append(component)
                    mole_fractions.append(fraction)
        total = sum(mole_fractions)
        if total > 0:
            mole_fractions = [fraction / total for fraction in mole_fractions]
        else:
            components, mole_fractions = [], []

        vfrac_node = _find_node(tree, output, VAPOR_FRACTION_PATHS)
        vapor_fraction, _ = _number(vfrac_node, units)
        if vapor_fraction is None:
            phase = "unknown"
        elif vapor_fraction <= 1e-6:
            phase = "liquid"
        elif vapor_fraction >= 1.0 - 1e-6:
            phase = "gas"
        else:
            phase = "two_phase"

        streams.append({
            "stream_id": name,
            "stream_name": name,
            "phase": phase,
            "vapor_fraction": vapor_fraction,
            "components": components,
            "mole_fractions": mole_fractions,
            "flow_kmol_h": flow_kmol_h,
            "T_K": T_K,
            "P_Pa": P_Pa,
            "units": {"T_K": "K", "P_Pa": "Pa", "flow_kmol_h": "kmol/h"},
        })

    return {"case_id": case_id, "streams": streams}


def _scan_on_com_thread() -> dict[str, Any]:
    cases = []
    for moniker, path, document in _running_documents():
        case_id = _case_id(moniker, path)
        error_count = _read_error_count(document.Tree)
        is_running = _engine_is_running(document)
        streams_node = document.Tree.FindNode(r"\Data\Streams")
        stream_names = [str(node.Name) for node in _collection_items(streams_node.Elements)] if streams_node else []
        run_status = "running" if is_running is True else "converged" if is_running is False and error_count == 0 and stream_names else "unknown"
        eligible = run_status == "converged" and error_count == 0
        cases.append({
            "case_id": case_id,
            "file_name": os.path.basename(path),
            "file_path": path,
            "moniker": moniker,
            "run_status": run_status,
            "error_count": error_count,
            "stream_count": len(stream_names),
            "eligible": eligible,
            "status_basis": "Aspen Engine.IsRunning=false, PER_ERROR=0, and stream tree present" if eligible else "Run status or error count could not be verified; case is not selectable",
        })
    return {"status": "connected" if cases else "no_active_cases", "cases": cases}


def scan_open_cases() -> dict[str, Any]:
    return _sta_call(_scan_on_com_thread)


def _read_selected_on_com_thread(case_id: str) -> dict[str, Any]:
    found = None
    for moniker, path, document in _running_documents():
        if _case_id(moniker, path) == case_id:
            found = (path, document)
            break
    if found is None:
        raise AspenConnectionError("所选案例已关闭或 Aspen 连接发生变化；请重新扫描。")
    path, document = found
    error_count = _read_error_count(document.Tree)
    is_running = _engine_is_running(document)
    if is_running is not False or error_count != 0:
        raise AspenConnectionError("案例正在运行，或无法确认已收敛且错误数为 0；为避免读取不完整数据，已拒绝读取。")
    payload = _read_streams(document, case_id)
    if not payload["streams"]:
        raise AspenConnectionError("Aspen 案例未返回任何流股。")
    for stream in payload["streams"]:
        missing = [field for field in ("T_K", "P_Pa", "flow_kmol_h") if stream[field] is None]
        if missing or not stream["components"]:
            stream["read_status"] = "incomplete"
            stream["read_message"] = "Aspen 输出缺少可确认的温度、压力、摩尔流量或组成。"
        elif stream["phase"] == "unknown":
            stream["read_status"] = "phase_unknown"
            stream["read_message"] = "未能从 Aspen 输出判定气相分率；不调用单相物性模型。"
        else:
            stream["read_status"] = "ok"
            stream["read_message"] = ""
    return {"status": "ok", "case_id": case_id, "file_path": path, **payload}


def read_case_streams(case_id: str) -> dict[str, Any]:
    if not case_id or not re.fullmatch(r"[0-9a-f]{20}", case_id):
        raise AspenConnectionError("案例标识无效；请重新扫描 Aspen 案例。")
    return _sta_call(lambda: _read_selected_on_com_thread(case_id))


def _read_bkp_on_com_thread(file_path: str) -> dict[str, Any]:
    """在 Windows Aspen 主机加载一个 BKP 并只读结果树。

    不在这里自动运行流程；只有 BKP 自带可读取的收敛结果且错误数为 0
    时才返回流股。Aspen COM 版本若不支持 InitFromArchive2 会明确失败。
    """
    if os.name != "nt":
        raise AspenConnectionError("BKP 文件只能在安装 Aspen Plus 的 Windows 主机上解析；当前云端不是 Windows。")
    if not file_path.lower().endswith(".bkp"):
        raise AspenConnectionError("上传文件必须是 Aspen Plus .bkp 文件。")
    if not os.path.isfile(file_path):
        raise AspenConnectionError("上传的 BKP 文件在服务器临时目录中不存在。")
    try:
        import win32com.client
        document = win32com.client.Dispatch("Apwn.Document")
        initializer = getattr(document, "InitFromArchive2", None) or getattr(document, "InitFromArchive", None)
        if initializer is None:
            raise AspenConnectionError("当前 Aspen COM 版本未提供 BKP 归档加载接口 InitFromArchive2/InitFromArchive。")
        initializer(os.path.abspath(file_path))
        tree = document.Tree
        error_count = _read_error_count(tree)
        is_running = _engine_is_running(document)
        if is_running is True:
            raise AspenConnectionError("BKP 已加载但 Aspen 仍在计算；请等待计算停止后重试。")
        if error_count != 0:
            raise AspenConnectionError(f"BKP 已加载，但 Aspen 报告错误数为 {error_count!r}；未读取不完整流股。")
        case_id = _case_id("uploaded-bkp", os.path.abspath(file_path))
        payload = _read_streams(document, case_id)
        if not payload["streams"]:
            raise AspenConnectionError("BKP 已加载，但结果树中没有可读取流股。请确认案例已成功运行并保存结果。")
        return {"status": "ok", "case_id": case_id, "file_path": os.path.abspath(file_path), **payload}
    except AspenConnectionError:
        raise
    except Exception as exc:
        raise AspenConnectionError(
            "Aspen 无法加载该 BKP。请确认文件来自当前 Aspen Plus 版本、案例已成功运行并保存，"
            f"且当前 Windows 用户有读取权限（{type(exc).__name__}）。"
        ) from exc


def read_bkp_file(file_name: str, file_bytes: bytes) -> dict[str, Any]:
    """在本机 Windows Aspen 环境中读取上传的 BKP；云端严格返回诊断。"""
    if not isinstance(file_bytes, (bytes, bytearray)) or not file_bytes:
        raise AspenConnectionError("上传的 BKP 文件为空。")
    if not str(file_name).lower().endswith(".bkp"):
        raise AspenConnectionError("请选择 Aspen Plus 的 .bkp 文件。")
    if os.name != "nt":
        raise AspenConnectionError(
            "当前网页运行在 Linux 云端，无法直接解析 Aspen BKP。请把文件上传到安装了 Aspen Plus 的 Windows 主机运行本地网页，"
            "或在该主机启动 Aspen 桥接后读取已打开的案例。"
        )
    with tempfile.NamedTemporaryFile(prefix="property-agent-", suffix=".bkp", delete=False) as handle:
        handle.write(bytes(file_bytes))
        temp_path = handle.name
    try:
        return _sta_call(lambda: _read_bkp_on_com_thread(temp_path))
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass


def _bridge_request(path: str) -> dict[str, Any]:
    base_url = os.getenv("ASPEN_BRIDGE_URL", "").strip().rstrip("/")
    token = os.getenv("ASPEN_BRIDGE_TOKEN", "").strip()
    if not base_url or not token:
        raise AspenConnectionError("未配置 Aspen 本地桥接地址或桥接令牌。")
    try:
        response = requests.get(
            base_url + path,
            headers={"Authorization": f"Bearer {token}"},
            timeout=(5, float(os.getenv("ASPEN_BRIDGE_TIMEOUT_SECONDS", "60"))),
        )
        response.raise_for_status()
        result = response.json()
    except requests.RequestException as exc:
        raise AspenConnectionError(f"无法访问 Aspen 本地桥接服务：{exc.__class__.__name__}") from exc
    if not isinstance(result, dict):
        raise AspenConnectionError("Aspen 本地桥接服务返回了无效响应。")
    if result.get("status") == "error":
        raise AspenConnectionError(str(result.get("message", "Aspen 桥接请求失败。")))
    return result


def scan_cases() -> dict[str, Any]:
    bridge_url = os.getenv("ASPEN_BRIDGE_URL", "").strip()
    if bridge_url:
        return _bridge_request("/api/cases")
    if os.name != "nt":
        raise AspenConnectionError(
            "当前云端运行环境不是 Windows，不能直接访问你电脑上的 Aspen。请在 Aspen 主机启动 aspen_bridge.py，"
            "通过 HTTPS 隧道发布桥接地址，并在 Render 设置 ASPEN_BRIDGE_URL 与 ASPEN_BRIDGE_TOKEN。"
        )
    return scan_open_cases()


def read_streams(case_id: str) -> dict[str, Any]:
    bridge_url = os.getenv("ASPEN_BRIDGE_URL", "").strip()
    if bridge_url:
        return _bridge_request(f"/api/cases/{quote(case_id, safe='')}/streams")
    return read_case_streams(case_id)
