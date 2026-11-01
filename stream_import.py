"""读取不依赖 Aspen 的通用流股 CSV/JSON 导出文件。"""

from __future__ import annotations

import csv
import io
import json
import math
import re
from typing import Any


class StreamImportError(ValueError):
    pass


def _number(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise StreamImportError(f"{label} 必须是数值。") from exc
    if not math.isfinite(result):
        raise StreamImportError(f"{label} 必须是有限数值。")
    return result


def _list_value(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [item.strip() for item in re.split(r"[,;|、，；\s]+", str(value).strip()) if item.strip()]


def _stream(row: dict[str, Any], index: int) -> dict[str, Any]:
    name = str(row.get("stream_id") or row.get("stream_name") or f"IMPORT-{index}").strip()
    components = [str(item).strip() for item in _list_value(row.get("components"))]
    fractions = [_number(item, "摩尔分数") for item in _list_value(row.get("mole_fractions"))]
    if not components or len(components) != len(fractions):
        raise StreamImportError(f"流股 {name} 的 components 和 mole_fractions 数量不一致。")
    if any(item < 0 for item in fractions) or sum(fractions) <= 0:
        raise StreamImportError(f"流股 {name} 的摩尔分数必须非负且总和大于 0。")
    T = row.get("T_K")
    if T in (None, "") and row.get("T_C") not in (None, ""):
        T = _number(row["T_C"], "温度") + 273.15
    P = row.get("P_Pa")
    if P in (None, "") and row.get("P_kPa") not in (None, ""):
        P = _number(row["P_kPa"], "压力") * 1000.0
    if T in (None, "") or P in (None, ""):
        raise StreamImportError(f"流股 {name} 缺少 T_K/P_Pa（也可提供 T_C/P_kPa）。")
    phase = str(row.get("phase", "unknown")).strip().lower()
    phase = {"l": "liquid", "g": "gas", "vapor": "gas", "two-phase": "two_phase"}.get(phase, phase)
    if phase not in {"liquid", "gas", "two_phase", "unknown"}:
        raise StreamImportError(f"流股 {name} 的 phase 不支持：{phase}。")
    return {
        "stream_id": name, "stream_name": str(row.get("stream_name") or name), "phase": phase,
        "vapor_fraction": _number(row.get("vapor_fraction", 0.0 if phase == "liquid" else 1.0 if phase == "gas" else 0.5), "气相分率"),
        "components": components, "mole_fractions": [item / sum(fractions) for item in fractions],
        "flow_kmol_h": _number(row.get("flow_kmol_h", 0.0), "摩尔流量"),
        "T_K": _number(T, "温度"), "P_Pa": _number(P, "压力"),
        "read_status": "ok", "read_message": "来源为用户上传的通用流股文件；不是 Aspen COM 直接读取。",
    }


def parse_stream_file(file_name: str, file_bytes: bytes) -> dict[str, Any]:
    suffix = str(file_name).lower().rsplit(".", 1)[-1] if "." in str(file_name) else ""
    if suffix not in {"json", "csv"}:
        raise StreamImportError("通用流股文件必须是 .json 或 .csv；.bkp 需要 Aspen Windows 主机解析。")
    try:
        if suffix == "json":
            payload = json.loads(bytes(file_bytes).decode("utf-8-sig"))
            rows = payload.get("streams") if isinstance(payload, dict) else payload
        else:
            rows = list(csv.DictReader(io.StringIO(bytes(file_bytes).decode("utf-8-sig"))))
    except (UnicodeDecodeError, json.JSONDecodeError, csv.Error) as exc:
        raise StreamImportError(f"无法读取 {suffix.upper()} 流股文件：{exc}") from exc
    if not isinstance(rows, list) or not rows:
        raise StreamImportError("文件中没有 streams 流股记录。")
    streams = [_stream(row, index) for index, row in enumerate(rows, start=1) if isinstance(row, dict)]
    return {"status": "imported", "case_id": f"import-{suffix}", "file_path": f"（上传的 .{suffix} 流股文件）", "streams": streams}
