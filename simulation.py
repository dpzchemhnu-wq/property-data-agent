"""与具体流程模拟器解耦的输入校验和数据契约。"""

from __future__ import annotations

import math
import uuid
from typing import Any, Protocol

from tools import query_properties


ENGINE_IDS = {"Aspen Plus": "aspen_plus", "DWSIM": "dwsim"}
UNIT_OPERATIONS = {"Flash separator": "flash"}
T_MIN_K = 150.0
T_MAX_K = 1200.0
P_MIN_PA = 100.0
P_MAX_PA = 1.0e9
FLOW_MAX_KMOL_H = 1.0e7


class SimulationInputError(ValueError):
    """输入不符合模拟器调用契约。"""


class SimulatorAdapter(Protocol):
    """具体模拟器连接器需要实现的最小接口。"""

    def run(self, request: dict[str, Any]) -> dict[str, Any]: ...


INPUT_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Process Simulator Request",
    "type": "object",
    "required": ["schema_version", "request_id", "engine", "unit_operation", "feed"],
    "properties": {
        "schema_version": {"const": "1.0"},
        "request_id": {"type": "string", "minLength": 1},
        "engine": {"type": "string", "enum": ["aspen_plus", "dwsim"]},
        "unit_operation": {"type": "string", "enum": ["flash"]},
        "feed": {
            "type": "object",
            "required": ["components", "mole_fractions", "flow_kmol_h", "T_K", "P_Pa"],
            "properties": {
                "components": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}, "uniqueItems": True},
                "mole_fractions": {"type": "array", "minItems": 1, "items": {"type": "number", "minimum": 0}},
                "flow_kmol_h": {"type": "number", "exclusiveMinimum": 0, "maximum": FLOW_MAX_KMOL_H},
                "T_K": {"type": "number", "minimum": T_MIN_K, "maximum": T_MAX_K},
                "P_Pa": {"type": "number", "minimum": P_MIN_PA, "maximum": P_MAX_PA},
            },
            "additionalProperties": False,
        },
        "validation": {"type": "object"},
    },
    "additionalProperties": False,
}

OUTPUT_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Process Simulator Result",
    "type": "object",
    "required": ["run_id", "engine", "execution_status", "converged", "streams", "mass_balance", "energy_balance", "warnings"],
    "properties": {
        "run_id": {"type": "string", "minLength": 1},
        "engine": {"type": "string", "enum": ["aspen_plus", "dwsim"]},
        "execution_status": {"type": "string", "enum": ["converged", "not_converged", "failed"]},
        "converged": {"type": "boolean"},
        "streams": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["stream_id", "phase", "T_K", "P_Pa", "flow_kmol_h", "components", "mole_fractions"],
                "properties": {
                    "stream_id": {"type": "string", "minLength": 1},
                    "phase": {"type": "string", "enum": ["liquid", "gas", "two_phase", "unknown"]},
                    "T_K": {"type": "number", "minimum": T_MIN_K, "maximum": T_MAX_K},
                    "P_Pa": {"type": "number", "minimum": P_MIN_PA, "maximum": P_MAX_PA},
                    "flow_kmol_h": {"type": "number", "minimum": 0},
                    "components": {"type": "array", "items": {"type": "string"}},
                    "mole_fractions": {"type": "array", "items": {"type": "number", "minimum": 0}},
                },
                "additionalProperties": True,
            },
        },
        "mass_balance": {"type": "object", "required": ["residual", "unit"], "properties": {"residual": {"type": "number"}, "unit": {"type": "string", "enum": ["kg/s", "kmol/h", "mol/s"]}}},
        "energy_balance": {"type": "object", "required": ["residual", "unit"], "properties": {"residual": {"type": "number"}, "unit": {"const": "W"}}},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": True,
}


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise SimulationInputError(f"{label} 必须是有限数值。")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise SimulationInputError(f"{label} 必须是有限数值。") from exc
    if not math.isfinite(result):
        raise SimulationInputError(f"{label} 必须是有限数值。")
    return result


def build_simulation_request(
    *,
    components: list[str],
    mole_fractions: list[float],
    flow_kmol_h: float,
    T_K: float,
    P_Pa: float,
    engine: str,
    unit_operation: str = "Flash separator",
) -> dict[str, Any]:
    """校验输入并组装与具体软件无关的进料请求。"""
    if engine not in ENGINE_IDS:
        raise SimulationInputError("请选择支持的流程模拟器接口。")
    if unit_operation not in UNIT_OPERATIONS:
        raise SimulationInputError("当前演示接口只定义 Flash separator 单元。")
    if not isinstance(components, list) or not components or any(not isinstance(c, str) or not c.strip() for c in components):
        raise SimulationInputError("请提供至少一个组分名称。")
    names = [c.strip() for c in components]
    if len({c.casefold() for c in names}) != len(names):
        raise SimulationInputError("组分名称重复，请合并重复组分后再提交。")
    if not isinstance(mole_fractions, list) or len(components) != len(mole_fractions):
        raise SimulationInputError("组分名称和摩尔比例数量必须相同，且顺序对应。")
    fractions = [_finite(value, f"第 {i + 1} 个摩尔比例") for i, value in enumerate(mole_fractions)]
    if any(value < 0 for value in fractions):
        raise SimulationInputError("摩尔比例不能为负数。")
    total = sum(fractions)
    if total <= 0:
        raise SimulationInputError("摩尔比例总和必须大于 0。")
    normalized = [value / total for value in fractions]

    flow = _finite(flow_kmol_h, "进料摩尔流量")
    temperature = _finite(T_K, "温度")
    pressure = _finite(P_Pa, "压力")
    if not 0 < flow <= FLOW_MAX_KMOL_H:
        raise SimulationInputError(f"进料摩尔流量必须在 (0, {FLOW_MAX_KMOL_H:g}] kmol/h 范围内。")
    if not T_MIN_K <= temperature <= T_MAX_K:
        raise SimulationInputError(f"温度必须在 {T_MIN_K:g}–{T_MAX_K:g} K 范围内。")
    if not P_MIN_PA <= pressure <= P_MAX_PA:
        raise SimulationInputError(f"压力必须在 {P_MIN_PA:g}–{P_MAX_PA:g} Pa 范围内。")

    return {
        "schema_version": "1.0",
        "request_id": str(uuid.uuid4()),
        "engine": ENGINE_IDS[engine],
        "unit_operation": UNIT_OPERATIONS[unit_operation],
        "feed": {
            "components": names,
            "mole_fractions": normalized,
            "flow_kmol_h": flow,
            "T_K": temperature,
            "P_Pa": pressure,
        },
        "validation": {
            "composition_input_sum": total,
            "composition_normalized": not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-6),
            "units_explicit": True,
        },
    }


def enrich_streams(streams: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run local property calculations only for successfully read single-phase streams."""
    source_rows: list[dict[str, Any]] = []
    property_rows: list[dict[str, Any]] = []
    for stream in streams:
        base = {
            key: stream.get(key)
            for key in ("stream_id", "stream_name", "phase", "flow_kmol_h", "T_K", "P_Pa")
        }
        base["components"] = "、".join(stream.get("components", []))
        base["mole_fractions"] = ", ".join(f"{value:.6g}" for value in stream.get("mole_fractions", []))
        source_rows.append(base)
        read_status = stream.get("read_status", "ok")
        if read_status != "ok":
            property_rows.append({**base, "status": read_status, "message": stream.get("read_message", "Aspen 流股数据不完整。")})
            continue
        phase = stream.get("phase")
        if phase == "two_phase":
            property_rows.append({**base, "status": "two_phase_skipped", "message": "气液两相流股暂不使用单相模型计算；Aspen 原始数据已保留。"})
            continue
        if phase not in {"liquid", "gas"}:
            property_rows.append({**base, "status": "phase_unknown", "message": "相态未知，未调用物性模型。"})
            continue
        target_properties = (
            ["density", "enthalpy", "viscosity", "thermal_conductivity"]
            if phase == "gas"
            else ["density", "enthalpy", "viscosity", "surface_tension", "thermal_conductivity"]
        )
        result = query_properties(
            stream["components"],
            stream["mole_fractions"],
            stream["T_K"],
            stream["P_Pa"],
            target_properties,
            include_references=False,
        )
        returned = result.get("properties", {}) or result.get("partial_properties", {})
        row: dict[str, Any] = {
            **base,
            "status": result.get("status", "solver_failed"),
            "message": result.get("message", ""),
            "phase_state": result.get("phase_state", phase),
        }
        backend_phase = (
            "gas" if result.get("phase_state") == "single_gas_detected"
            else "liquid" if result.get("phase_state") == "single_liquid_assumed"
            else next((item.get("phase") for item in returned.values() if isinstance(item, dict) and item.get("phase")), None)
        )
        if returned and backend_phase and backend_phase != phase:
            returned = {}
            row["status"] = "phase_mismatch"
            row["message"] = f"Aspen 相态为 {phase}，物性后端识别为 {backend_phase}；已阻止相态不匹配的数据。"
        elif phase == "gas" and returned:
            row["status"] = "gas_partial"
            row["message"] = "仅返回后端可计算的气相属性；不适用的液相表面张力不会计算。"
        for property_name, value in returned.items():
            row[property_name] = value.get("value") if isinstance(value, dict) else value
            row[f"{property_name}_unit"] = value.get("unit", "") if isinstance(value, dict) else ""
            row[f"{property_name}_source_ids"] = ", ".join(value.get("source_ids", [])) if isinstance(value, dict) else ""
        if result.get("warnings"):
            row["warnings"] = "；".join(str(warning) for warning in result["warnings"])
        if not row.get("warnings") and row["status"] not in {"ok", "gas_partial"}:
            row["warnings"] = row["message"] or "该流股未返回物性结果。"
        property_rows.append(row)
    return source_rows, property_rows


__all__ = ["build_simulation_request", "enrich_streams"]

