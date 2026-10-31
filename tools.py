"""物性数据后端：输入校验、模型选择、物性计算与参考数据检索。

所有数值都由后端计算或外部数据源返回。模型参数台账采用 JSON，格式见 README.md。
"""

from __future__ import annotations

import json
import math
import os
import csv
import re
from functools import lru_cache
from pathlib import Path
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests

try:
    from chemicals.identifiers import int_to_CAS, search_chemical
    from thermo import Chemical, Mixture
except ImportError:  # 允许界面启动并将缺失依赖作为明确错误返回
    search_chemical = None
    int_to_CAS = None
    Chemical = None
    Mixture = None


TARGET_PROPERTIES = {
    "density": "液相密度",
    "enthalpy": "摩尔焓",
    "viscosity": "动力粘度",
    "bubble_point": "泡点温度",
    "surface_tension": "表面张力",
    "thermal_conductivity": "热导率",
}
T_MIN_K = 273.15
T_MAX_K = 450.0
P_MIN_PA = 1.0
P_MAX_PA = 100_000_000.0
R_J_MOL_K = 8.31446261815324

# 核心中文/常用缩写作为代码内兜底映射，避免部署时漏拷贝 JSON 文件就无法查组分。
# 其他组分仍由别名文件和 chemicals 通用索引扩展识别，不把计算体系锁定在此列表。
BUILTIN_COMPONENT_ALIASES = {
    "水": "7732-18-5", "water": "7732-18-5", "h2o": "7732-18-5", "w": "7732-18-5",
    "乙醇": "64-17-5", "ethanol": "64-17-5", "酒精": "64-17-5", "e": "64-17-5",
    "乙酸": "64-19-7", "醋酸": "64-19-7", "acetic acid": "64-19-7", "ethanoic acid": "64-19-7", "ac": "64-19-7",
    "乙酸乙酯": "141-78-6", "醋酸乙酯": "141-78-6", "ethyl acetate": "141-78-6", "ea": "141-78-6",
    "丙酮": "67-64-1", "acetone": "67-64-1", "甲苯": "108-88-3", "toluene": "108-88-3",
}
AMBIGUOUS_COMPONENT_CAS = {
    "1330-20-7": "二甲苯/xylene 通常指邻、间、对位异构体混合物，无法作为单一纯组分计算；请指定 o-xylene、m-xylene 或 p-xylene。",
}


@dataclass
class Component:
    """保留用户名称与位置，同时记录可供 thermo 使用的 CAS。"""

    input_name: str
    cas: str
    canonical_name: str


def _failure(status: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"status": status, "message": message, **extra}


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} 必须是数值。")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} 必须是有限数值。")
    return number


def _unit_key(unit: Any) -> str:
    """仅规整等价排版字符，不做单位量纲换算。"""
    text = str(unit or "").casefold().replace("³", "3").replace("²", "2").replace("·", "")
    return re.sub(r"[\s*]", "", text)


def _load_component_aliases() -> dict[str, str]:
    """合并代码内基础别名与可编辑 JSON 别名；缺少 JSON 时保留内置中文映射。"""
    aliases = {key.casefold(): value for key, value in BUILTIN_COMPONENT_ALIASES.items()}
    alias_path = os.getenv("COMPONENT_ALIAS_FILE", "").strip()
    if not alias_path:
        alias_path = str(Path(__file__).with_name("component_aliases.json"))
    try:
        with open(alias_path, "r", encoding="utf-8") as alias_file:
            alias_payload = json.load(alias_file)
        raw_aliases = alias_payload.get("aliases", {}) if isinstance(alias_payload, dict) else {}
        if isinstance(raw_aliases, dict):
            aliases.update({str(key).strip().casefold(): str(value).strip() for key, value in raw_aliases.items()})
    except (OSError, json.JSONDecodeError):
        # 别名文件是可选扩展；内置映射仍确保常用中文名称可用。
        pass
    return aliases


BUILTIN_CHINESE_NORMALIZATIONS = {
    "乙苯": "ethylbenzene",
    "乙基苯": "ethylbenzene",
    "苯乙烷": "ethylbenzene",
    "甲苯": "toluene",
    "甲基苯": "toluene",
    "苯": "benzene",
    "甲醇": "methanol",
    "木醇": "methanol",
    "乙醇": "ethanol",
    "酒精": "ethanol",
    "水": "water",
    "氧化氢": "water",
}


@lru_cache(maxsize=1024)
def _pubchem_name_lookup(name: str) -> tuple[str, str]:
    """本地目录未命中时访问 PubChem PUG REST，用规范名称反查 CAS。"""
    cleaned = name.strip()
    if not cleaned or not re.search(r"[A-Za-z0-9]", cleaned):
        # PubChem 的 PUG REST 名称索引不稳定覆盖中文名；中文常见名由可扩展别名表处理。
        return "", ""
    base = "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/" + quote(cleaned, safe="")
    try:
        response = requests.get(f"{base}/cids/JSON", timeout=8, headers={"User-Agent": "PropertyDataAgent/1.0 (chemical identifier lookup)"})
        if response.status_code == 404:
            return "", ""
        response.raise_for_status()
        body = response.json()
        cids = body.get("IdentifierList", {}).get("CID", []) if isinstance(body, dict) else []
        if not cids:
            return "", ""
        # Name lookup's first CID is PubChem's exact/best match. Search its synonyms only for a CAS RN;
        # do not invent an identifier if no CAS synonym is present.
        synonym_response = requests.get(
            f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/{int(cids[0])}/synonyms/JSON",
            timeout=8,
            headers={"User-Agent": "PropertyDataAgent/1.0 (chemical identifier lookup)"},
        )
        synonym_response.raise_for_status()
        synonym_body = synonym_response.json()
        synonyms = synonym_body.get("InformationList", {}).get("Information", [{}])[0].get("Synonym", [])
        for synonym in synonyms:
            candidate = str(synonym).strip()
            if re.fullmatch(r"\d{2,7}-\d{2}-\d", candidate):
                try:
                    # Confirm the CAS is understood by the local chemistry index before returning it.
                    record = search_chemical(candidate) if search_chemical is not None else None
                    raw_cas = getattr(record, "CAS", "") if record is not None else ""
                    normalized = int_to_CAS(int(raw_cas)) if raw_cas and str(raw_cas).isdigit() and int_to_CAS else str(raw_cas or "")
                    if normalized == candidate:
                        canonical = str(getattr(record, "common_name", "") or getattr(record, "name", "") or cleaned).strip()
                        return candidate, canonical
                except Exception:
                    continue
    except (requests.RequestException, ValueError, KeyError, TypeError, IndexError):
        return "", ""
    return "", ""


def _resolve_components(names: list[str]) -> list[Component]:
    if search_chemical is None:
        raise RuntimeError("缺少 chemicals/thermo 依赖，无法解析组分。")
    aliases = _load_component_aliases()
    resolved: list[Component] = []
    unknown: list[str] = []
    for name in names:
        try:
            normalized_name = name.strip()
            lookup_name = aliases.get(normalized_name.casefold(), normalized_name)
            lookup_name = BUILTIN_CHINESE_NORMALIZATIONS.get(normalized_name, lookup_name)
            if lookup_name in AMBIGUOUS_COMPONENT_CAS:
                raise LookupError(AMBIGUOUS_COMPONENT_CAS[lookup_name])
            try:
                record = search_chemical(lookup_name)
                raw_cas = getattr(record, "CAS", "")
                cas = int_to_CAS(int(raw_cas)) if raw_cas and str(raw_cas).isdigit() else str(raw_cas or "").strip()
                canonical = str(getattr(record, "common_name", "") or getattr(record, "name", "") or name).strip()
            except Exception:
                cas, canonical = "", ""
            if not cas:
                cas, canonical = _pubchem_name_lookup(lookup_name)
            if not cas:
                unknown.append(name)
            else:
                resolved.append(Component(name, cas, canonical))
        except LookupError:
            raise
        except Exception:
            unknown.append(name)
    if unknown:
        raise LookupError(
            "以下名称未能通过别名目录、chemicals 或 PubChem 精确匹配到 CAS RN：" + "、".join(unknown)
            + "。请尝试使用英文/IUPAC 名称或 CAS RN；系统不会用相似物质代替。"
        )
    return resolved


def _load_ledger() -> dict[str, Any]:
    """读取团队维护的交互参数；没有配置时返回空台账。"""
    path = os.getenv("BINARY_PARAMETER_LEDGER", "").strip()
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as ledger_file:
            payload = json.load(ledger_file)
        if not isinstance(payload, dict):
            return {}
        return payload
    except (OSError, json.JSONDecodeError):
        return {}


def _pair_record(ledger: dict[str, Any], cas_i: str, cas_j: str, model: str) -> dict[str, Any] | None:
    pairs = ledger.get("pairs", [])
    if not isinstance(pairs, list):
        return None
    wanted = {cas_i, cas_j}
    for pair in pairs:
        if not isinstance(pair, dict) or str(pair.get("model", "")).upper() != model:
            continue
        if {str(pair.get("CAS1", "")), str(pair.get("CAS2", ""))} == wanted:
            return pair
    return None


def _ordered_pair_value(pair: dict[str, Any], cas_i: str, key_ij: str, key_ji: str, cas_j: str) -> float:
    forward = cas_i == str(pair.get("CAS1", ""))
    key = key_ij if forward else key_ji
    return _finite_number(pair[key], key)


def _model_coverage(components: list[Component], ledger: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
    """仅在台账覆盖全部二元对时选择对应模型，避免部分参数被误用。"""
    if len(components) == 1:
        return "PURE", "纯组分（无需二元交互参数）", []
    pair_rows = [(components[i].cas, components[j].cas) for i in range(len(components)) for j in range(i + 1, len(components))]
    for model in ("NRTL", "UNIQUAC"):
        found: list[dict[str, Any]] = []
        complete = True
        for cas_i, cas_j in pair_rows:
            row = _pair_record(ledger, cas_i, cas_j, model)
            required = (
                ("tau_a_ij", "tau_a_ji", "tau_b_ij", "tau_b_ji", "alpha_ij", "source_id")
                if model == "NRTL"
                else ("u_ij_J_mol", "u_ji_J_mol", "source_id")
            )
            if not row or any(key not in row for key in required):
                complete = False
                break
            try:
                source_id = str(row["source_id"]).strip()
                if not source_id or "replace-with" in source_id.lower() or source_id.lower() in {"tbd", "unknown", "placeholder"}:
                    complete = False
                    break
                numeric_keys = [key for key in required if key != "source_id"]
                for key in numeric_keys:
                    _finite_number(row[key], key)
            except (KeyError, ValueError):
                complete = False
                break
            found.append(row)
        if complete and model == "UNIQUAC":
            component_data = ledger.get("components", {})
            complete = isinstance(component_data, dict) and all(
                component.cas in component_data
                and isinstance(component_data[component.cas], dict)
                and "r" in component_data[component.cas]
                and "q" in component_data[component.cas]
                and isinstance(component_data[component.cas].get("r"), (int, float))
                and isinstance(component_data[component.cas].get("q"), (int, float))
                and component_data[component.cas]["r"] > 0
                and component_data[component.cas]["q"] > 0
                for component in components
            )
        if complete:
            return model, "团队参数台账覆盖全部二元对", found
    return "UNIFAC", "未找到覆盖全部二元对的 NRTL/UNIQUAC 文献参数，降级为基团贡献估算", []


def _nrtl_gammas(xs: list[float], T: float, components: list[Component], rows: list[dict[str, Any]]) -> list[float]:
    """用台账的 tau=a+b/T 与 alpha 参数计算 NRTL 活度系数。"""
    n = len(xs)
    tau = [[0.0] * n for _ in range(n)]
    alpha = [[0.0] * n for _ in range(n)]
    row_by_pair: dict[frozenset[str], dict[str, Any]] = {frozenset((r["CAS1"], r["CAS2"])): r for r in rows}
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            row = row_by_pair[frozenset((components[i].cas, components[j].cas))]
            tau[i][j] = _ordered_pair_value(row, components[i].cas, "tau_a_ij", "tau_a_ji", components[j].cas) + _ordered_pair_value(row, components[i].cas, "tau_b_ij", "tau_b_ji", components[j].cas) / T
            alpha[i][j] = _finite_number(row["alpha_ij"], "alpha_ij")
    G = [[math.exp(max(-700.0, min(700.0, -alpha[i][j] * tau[i][j]))) for j in range(n)] for i in range(n)]
    lngamma: list[float] = []
    for i in range(n):
        denominator_i = sum(xs[k] * G[k][i] for k in range(n))
        first = sum(xs[j] * tau[j][i] * G[j][i] for j in range(n)) / max(denominator_i, 1e-300)
        second = 0.0
        for j in range(n):
            denominator_j = sum(xs[k] * G[k][j] for k in range(n))
            weighted_tau = sum(xs[k] * tau[k][j] * G[k][j] for k in range(n)) / max(denominator_j, 1e-300)
            second += xs[j] * G[i][j] / max(denominator_j, 1e-300) * (tau[i][j] - weighted_tau)
        lngamma.append(first + second)
    return [math.exp(max(-700.0, min(700.0, value))) for value in lngamma]


def _uniquac_gammas(xs: list[float], T: float, components: list[Component], rows: list[dict[str, Any]], ledger: dict[str, Any]) -> list[float]:
    """计算 UNIQUAC 活度系数；r/q 来自台账 components 区段。"""
    component_data = ledger.get("components", {})
    rs, qs = [], []
    for component in components:
        item = component_data.get(component.cas, {}) if isinstance(component_data, dict) else {}
        rs.append(_finite_number(item["r"], f"{component.cas}.r"))
        qs.append(_finite_number(item["q"], f"{component.cas}.q"))
    n = len(xs)
    tau = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    row_by_pair: dict[frozenset[str], dict[str, Any]] = {frozenset((r["CAS1"], r["CAS2"])): r for r in rows}
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            row = row_by_pair[frozenset((components[i].cas, components[j].cas))]
            energy = _ordered_pair_value(row, components[i].cas, "u_ij_J_mol", "u_ji_J_mol", components[j].cas)
            tau[i][j] = math.exp(max(-700.0, min(700.0, -energy / (R_J_MOL_K * T))))
    z = 10.0
    rsum = sum(xs[i] * rs[i] for i in range(n))
    qsum = sum(xs[i] * qs[i] for i in range(n))
    phi = [xs[i] * rs[i] / rsum for i in range(n)]
    theta = [xs[i] * qs[i] / qsum for i in range(n)]
    ell = [z / 2.0 * (rs[i] - qs[i]) - (rs[i] - 1.0) for i in range(n)]
    ell_sum = sum(xs[i] * ell[i] for i in range(n))
    gammas = []
    for i in range(n):
        combinatorial = math.log(phi[i] / xs[i]) + z / 2.0 * qs[i] * math.log(theta[i] / phi[i]) + ell[i] - phi[i] / xs[i] * ell_sum
        sum_tau_ji = sum(theta[j] * tau[j][i] for j in range(n))
        residual_tail = sum(theta[j] * tau[i][j] / max(sum(theta[k] * tau[k][j] for k in range(n)), 1e-300) for j in range(n))
        residual = qs[i] * (1.0 - math.log(max(sum_tau_ji, 1e-300)) - residual_tail)
        gammas.append(math.exp(max(-700.0, min(700.0, combinatorial + residual))))
    return gammas


def _activity_gammas(model: str, xs: list[float], T: float, components: list[Component], rows: list[dict[str, Any]], ledger: dict[str, Any], mixture: Any) -> list[float]:
    if model == "PURE":
        return [1.0]
    if model == "NRTL":
        return _nrtl_gammas(xs, T, components, rows)
    if model == "UNIQUAC":
        return _uniquac_gammas(xs, T, components, rows, ledger)
    try:
        from thermo.unifac import UNIFAC_gammas

        groups = getattr(mixture, "UNIFAC_groups", None)
        if not groups:
            raise RuntimeError("thermo 未能为所有组分生成 UNIFAC 基团结构。")
        gammas = UNIFAC_gammas(T=T, xs=xs, chemgroups=groups)
        if len(gammas) != len(xs) or any(not math.isfinite(float(g)) or float(g) <= 0 for g in gammas):
            raise RuntimeError("UNIFAC 返回无效活度系数。")
        return [float(g) for g in gammas]
    except Exception as exc:
        raise RuntimeError(f"UNIFAC 计算失败：{exc}") from exc


def _bubble_point(xs: list[float], P_Pa: float, components: list[Component], model: str, rows: list[dict[str, Any]], ledger: dict[str, Any], mixture: Any) -> float:
    """以改进拉乌尔定律和所选活度模型求解泡点，不对区间外外推。"""
    if Chemical is None:
        raise RuntimeError("thermo.Chemical 不可用，无法计算泡点。")
    try:
        from scipy.optimize import brentq
    except ImportError as exc:
        raise RuntimeError("缺少 scipy，无法数值求解泡点。") from exc

    def residual(T: float) -> float:
        gammas = _activity_gammas(model, xs, T, components, rows, ledger, mixture)
        total = 0.0
        for i, component in enumerate(components):
            pure = Chemical(component.cas, T=T, P=P_Pa)
            psat = float(pure.Psat)
            if not math.isfinite(psat) or psat <= 0:
                raise RuntimeError(f"{component.input_name} 在 {T:.2f} K 无有效饱和蒸气压。")
            total += xs[i] * gammas[i] * psat
        return total / P_Pa - 1.0

    low, high = T_MIN_K, T_MAX_K
    f_low, f_high = residual(low), residual(high)
    if f_low == 0:
        return low
    if f_high == 0:
        return high
    if f_low * f_high > 0:
        raise RuntimeError(f"泡点未在 {low:.2f}–{high:.2f} K 安全区间内形成有效括区；未进行外推。")
    value = float(brentq(residual, low, high, xtol=1e-5, maxiter=100))
    if not math.isfinite(value):
        raise RuntimeError("泡点求解器未返回有限结果。")
    return value


def _number_from_mixture(mixture: Any, property_name: str, attribute: str, multiplier: float = 1.0) -> dict[str, Any]:
    value = getattr(mixture, attribute, None)
    if value is None:
        raise RuntimeError(f"thermo 未返回 {property_name} ({attribute})。")
    number = float(value) * multiplier
    if not math.isfinite(number):
        raise RuntimeError(f"{property_name} 计算结果不是有限值。")
    return {"value": number}


def _parse_nist_table(text: str, expected_T_K: float | None = None, expected_P_MPa: float | None = None) -> dict[str, float]:
    """解析 NIST Fluid CGI 返回的制表符数据表，不猜测页面中的数值。"""
    lines = [line for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        headers = [value.strip().lower() for value in next(csv.reader([line], delimiter="\t"))]
        if not any("temperature" in header for header in headers) or not any("pressure" in header for header in headers):
            continue
        for candidate in lines[index + 1:]:
            cells = [value.strip() for value in next(csv.reader([candidate], delimiter="\t"))]
            if len(cells) != len(headers):
                continue
            row_conditions: dict[str, float] = {}
            for header, cell in zip(headers, cells):
                if "temperature" not in header and "pressure" not in header:
                    continue
                try:
                    row_conditions["temperature" if "temperature" in header else "pressure"] = float(cell)
                except ValueError:
                    continue
            if expected_T_K is not None and (
                "temperature" not in row_conditions
                or abs(row_conditions["temperature"] - expected_T_K) > max(0.02, expected_T_K * 1e-5)
            ):
                continue
            if expected_P_MPa is not None and (
                "pressure" not in row_conditions
                or abs(row_conditions["pressure"] - expected_P_MPa) > max(1e-7, expected_P_MPa * 1e-4)
            ):
                continue
            found: dict[str, float] = {}
            for header, cell in zip(headers, cells):
                key = None
                if "density" in header:
                    key = "density"
                elif "enthalpy" in header:
                    key = "enthalpy"
                elif "viscosity" in header:
                    key = "viscosity"
                elif "thermal conductivity" in header or "thermal_conductivity" in header:
                    key = "thermal_conductivity"
                elif "surface tension" in header or "surface_tension" in header:
                    key = "surface_tension"
                elif "temperature" in header:
                    key = "temperature"
                if key and cell:
                    try:
                        number = float(cell)
                        if math.isfinite(number):
                            found[key] = number
                    except ValueError:
                        continue
            if found:
                found.update(row_conditions)
                return found
    return {}


def _nist_fluid_request(component: Component, data_type: str, T_K: float, P_Pa: float, timeout_s: float) -> dict[str, float]:
    """请求 NIST 纯流体表；只请求固定状态点或给定压力饱和点。"""
    cas_digits = "".join(character for character in component.cas if character.isdigit())
    if not cas_digits:
        return {}
    params = {
        # Fluid CGI 的 ID 要求 CAS 数字串格式，例如水为 C7732185，而非带连字符形式。
        "Action": "Data", "Wide": "on", "ID": f"C{cas_digits}", "Type": data_type,
        "Digits": "8", "PLow": f"{P_Pa / 1_000_000.0:.12g}",
        "PHigh": f"{P_Pa / 1_000_000.0:.12g}", "PInc": "0",
        "T": f"{T_K:.12g}", "RefState": "DEF", "TUnit": "K", "PUnit": "MPa",
        "DUnit": "kg/m3", "HUnit": "kJ/mol", "WUnit": "m/s",
        "VisUnit": "uPa*s", "STUnit": "N/m",
    }
    url = "https://webbook.nist.gov/cgi/fluid.cgi"
    headers = {"User-Agent": "PropertyDataAgent/1.0 (research use)"}
    try:
        response = requests.get(url, params=params, timeout=timeout_s, headers=headers)
    except requests.exceptions.SSLError:
        # 部分 Windows 网络代理与该 NIST 主机的 TLS 握手不兼容；仅在握手失败时
        # 尝试系统直连，避免长期绕过用户网络代理设置。
        direct = requests.Session()
        direct.trust_env = False
        response = direct.get(url, params=params, timeout=timeout_s, headers=headers)
    # Fluid CGI 对未收录的流体 ID 返回 HTTP 400 + 这段固定语法错误文本；
    # 这表示该纯组分未覆盖，不代表网络连接失败。
    if response.status_code == 400 and "syntax error in the query" in response.text.casefold():
        return {}
    response.raise_for_status()
    return _parse_nist_table(response.text, expected_T_K=T_K, expected_P_MPa=P_Pa / 1_000_000.0)


def _nist_lookup(component: Component, T_K: float, P_Pa: float, timeout_s: float = 8.0) -> dict[str, Any]:
    """访问 NIST WebBook 纯流体状态点数据；覆盖范围取决于 WebBook 流体库。"""
    try:
        isothermal = _nist_fluid_request(component, "IsoTherm", T_K, P_Pa, timeout_s)
        # 未确认 NIST 饱和表的压力语义前，不将其他状态点混作泡点或表面张力参考。
        # NIST 的 fluid CGI 返回特定流体的表格；不支持该 CAS 或未返回表格时留空。
        data: dict[str, Any] = {}
        conversions = {
            "density": ("density", 1.0, "kg/m3"),
            "enthalpy": ("enthalpy", 1.0, "kJ/mol"),
            "viscosity": ("viscosity", 1e-3, "mPa·s"),
            "thermal_conductivity": ("thermal_conductivity", 1.0, "W/(m·K)"),
        }
        for property_name, (source_key, multiplier, unit) in conversions.items():
            if source_key in isothermal:
                data[property_name] = {"value": isothermal[source_key] * multiplier, "unit": unit, "source_id": f"nist_webbook_fluid:{component.cas}"}
        if not data:
            return {"found": False, "message": "NIST WebBook 流体库未覆盖该纯组分，或未返回给定状态点的数据。", "data": {}}
        return {
            "found": True,
            "message": "已查询 NIST WebBook 纯流体状态点数据；各物性仅在对应表格返回时参与比较。",
            "source_id": f"nist_webbook_fluid:{component.cas}",
            "data": data,
        }
    except requests.Timeout:
        return {"found": False, "timeout": True, "message": "NIST WebBook 请求超时；参考值未覆盖。"}
    except requests.RequestException as exc:
        detail = str(exc).splitlines()[0][:240]
        return {"found": False, "unavailable": True, "message": f"NIST WebBook 请求失败（{exc.__class__.__name__}）：{detail}；此次无法判断数据库是否覆盖该状态点。"}


THERMOML_SEARCH_URL = "https://trc.nist.gov/ThermoML-API/objects"
THERMOML_JSON_URL = "https://trc.nist.gov/ThermoML"
THERMOML_ARCHIVE_URL = "https://data.nist.gov/od/id/mds2-2422"
THERMOML_METADATA_URL = "https://data.nist.gov/od/id/mds2-2422"
THERMOML_SEARCH_INFO_URL = "https://trc.nist.gov/ThermoML/Search"


def _as_list(value: Any) -> list[Any]:
    """ThermoML JSON 中的单元素数组有时会被序列化成对象，统一展开。"""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


@lru_cache(maxsize=96)
def _thermoml_search_page(query: str, page: int, page_size: int, timeout_s: float) -> dict[str, Any]:
    """调用 NIST/TRC 官方 Cordra 搜索 API（该 API 提供元数据，不包含数值点）。"""
    context = json.dumps({"Citation": True, "data_summary": True, "Compound": True}, separators=(",", ":"))
    response = requests.get(
        THERMOML_SEARCH_URL,
        params={"query": query, "pageNum": page, "pageSize": page_size, "sortFields": "", "requestContext": context},
        timeout=timeout_s,
        headers={"Accept": "application/json", "User-Agent": "PropertyDataAgent/1.0 (NIST ThermoML lookup)"},
    )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or not isinstance(body.get("results", []), list):
        raise ValueError("NIST ThermoML Search API returned an unexpected response.")
    return body


@lru_cache(maxsize=256)
def _thermoml_document(doi: str, timeout_s: float) -> dict[str, Any]:
    """按官方网页公开的 DOI 路径读取单篇完整 ThermoML JSON（含实验数据点）。"""
    safe_doi = quote(doi.strip(), safe="/.-_()")
    response = requests.get(
        f"{THERMOML_JSON_URL}/{safe_doi}.json", timeout=timeout_s,
        headers={"Accept": "application/json", "User-Agent": "PropertyDataAgent/1.0 (NIST ThermoML lookup)"},
    )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError("NIST ThermoML JSON entry is not an object.")
    return body


def _lucene_term(value: str) -> str:
    """转义用户/数据库中的组分名称，防止破坏公开 Lucene 查询表达式。"""
    escaped = re.sub(r'([+\-!(){}\[\]^"~*?:\\/])', r"\\\1", value.strip())
    return f'"{escaped}"' if " " in escaped else escaped


def _thermoml_component_names(record: dict[str, Any]) -> set[str]:
    names = record.get("sCommonName", [])
    return {str(name).strip().casefold() for name in _as_list(names) if str(name).strip()}


def _thermoml_name_candidates(component: dict[str, Any]) -> set[str]:
    candidates = {str(component.get("name", "")).strip().casefold(), str(component.get("canonical_name", "")).strip().casefold()}
    cas = str(component.get("cas", "")).strip()
    for alias, alias_cas in _load_component_aliases().items():
        if alias_cas == cas:
            candidates.add(alias.casefold())
    return {item for item in candidates if item}


def _thermoml_summary_supports_request(summary: Any, requested: list[str]) -> bool:
    """先用搜索元数据的数据点摘要筛掉完全不含目标物性的文献。"""
    fragments: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            fragments.extend(str(key).casefold() for key in value)
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(summary)
    searchable = " ".join(fragments)
    terms = {
        "density": ("density",),
        "enthalpy": ("enthalpy",),
        "viscosity": ("viscosity",),
        "surface_tension": ("surface tension",),
        "thermal_conductivity": ("thermal conductivity", "heat conductivity"),
    }
    supported_targets = [key for key in requested if key in terms]
    if not searchable or not supported_targets:
        return bool(searchable)
    return any(any(term in searchable for term in terms[key]) for key in supported_targets)


def _thermoml_record_component_map(
    body: dict[str, Any], components: list[dict[str, Any]],
) -> dict[int, int] | None:
    """将用户体系的 CAS/名称匹配到 ThermoML Compound 序号。"""
    records = _as_list(body.get("Compound"))
    if len(records) < len(components):
        return None
    matched: dict[int, int] = {}
    used_org_numbers: set[int] = set()
    for index, component in enumerate(components):
        candidate_names = _thermoml_name_candidates(component)
        matches: list[int] = []
        for record in records:
            if not isinstance(record, dict) or not (_thermoml_component_names(record) & candidate_names):
                continue
            try:
                org_number = int(record.get("RegNum", {}).get("nOrgNum"))
            except (TypeError, ValueError, AttributeError):
                continue
            if org_number not in used_org_numbers:
                matches.append(org_number)
        if len(matches) != 1:
            return None
        matched[index] = matches[0]
        used_org_numbers.add(matches[0])
    return matched


def _unit_conversion(property_name: str, descriptor: str) -> tuple[float, str] | None:
    """只转换能从 ThermoML 原始字段明确读出的单位；未知单位不参与差异计算。"""
    unit_text = descriptor.casefold().replace("μ", "u").replace("µ", "u").replace("·", " ")
    compact = re.sub(r"[\s*^]", "", unit_text)
    if property_name == "density":
        if "kg/m3" in compact or "kgm-3" in compact:
            return 1.0, "kg/m³"
        if "g/cm3" in compact or "gml-1" in compact or "g/ml" in unit_text:
            return 1000.0, "kg/m³"
    elif property_name == "enthalpy":
        if "kj/mol" in compact or "kjmol-1" in compact:
            return 1.0, "kJ/mol"
        if "j/mol" in compact or "jmol-1" in compact:
            return 1e-3, "kJ/mol"
    elif property_name == "viscosity" and "kinematic" not in unit_text:
        if "mpas" in compact or "mpa.s" in unit_text:
            return 1.0, "mPa·s"
        if "upas" in compact or "upa.s" in unit_text:
            return 1e-3, "mPa·s"
        if "pas" in compact or "pa.s" in unit_text:
            return 1000.0, "mPa·s"
    elif property_name == "surface_tension":
        if "mn/m" in compact:
            return 1.0, "mN/m"
        if "n/m" in compact:
            return 1000.0, "mN/m"
    elif property_name == "thermal_conductivity":
        if "w/(m k)" in unit_text or "w/m/k" in compact or "wm-1k-1" in compact:
            return 1.0, "W/(m·K)"
        if "mw/(m k)" in unit_text:
            return 1e-3, "W/(m·K)"
    return None


def _thermoml_property_descriptor(property_row: dict[str, Any]) -> str:
    method = property_row.get("Property-MethodID", {})
    group = method.get("PropertyGroup", {}) if isinstance(method, dict) else {}
    parts: list[str] = []
    if isinstance(group, dict):
        for value in group.values():
            if isinstance(value, dict):
                parts.extend(str(value[key]) for key in ("ePropName", "eMethodName") if value.get(key))
    return " ".join(parts)


def _thermoml_property_kind(descriptor: str) -> str | None:
    text = descriptor.casefold()
    if "density" in text and "relative" not in text:
        return "density"
    if "enthalpy" in text:
        return "enthalpy"
    if "viscosity" in text and "kinematic" not in text:
        return "viscosity"
    if "surface tension" in text:
        return "surface_tension"
    if "thermal conductivity" in text or "heat conductivity" in text:
        return "thermal_conductivity"
    return None


def _thermoml_field_unit(descriptor: str) -> str:
    # 官方文件通常将单位写在 PropertyGroup 的 ePropName 中；仅接受已知类型。
    return descriptor


def _thermoml_unit_scale(label: str, expected: str) -> float | None:
    text = label.casefold().replace("μ", "u").replace("µ", "u").replace("·", " ")
    compact = re.sub(r"[\s*^]", "", text)
    if "temperature" in text:
        return 1.0 if "k" in text else None
    if "pressure" in text:
        if "mpa" in compact:
            return 1e6
        if "kpa" in compact:
            return 1e3
        if re.search(r"(?:^|[, ])pa(?:$|[, ])", text):
            return 1.0
        if "bar" in text:
            return 1e5
        if "atm" in text:
            return 101325.0
    return None


def _thermoml_block_matches_state(
    block: dict[str, Any], body: dict[str, Any], component_map: dict[int, int],
    fractions: list[float], T_K: float, P_Pa: float,
) -> list[dict[str, Any]]:
    """仅返回体系、组成、温度和压力均匹配的原始实验点，不做插值/外推。"""
    block_components = _as_list(block.get("Component"))
    try:
        block_org_numbers = {int(row.get("RegNum", {}).get("nOrgNum")) for row in block_components}
    except (TypeError, ValueError, AttributeError):
        return []
    expected_org_numbers = set(component_map.values())
    if block_org_numbers != expected_org_numbers:
        return []

    temperature_variable: int | None = None
    pressure_variable: int | None = None
    variable_scales: dict[int, float] = {}
    composition_variables: dict[int, int] = {}
    for row in _as_list(block.get("Variable")):
        if not isinstance(row, dict):
            continue
        number = row.get("nVarNumber")
        try:
            number = int(number)
        except (TypeError, ValueError):
            continue
        identity = row.get("VariableID", {})
        kind_row = identity.get("VariableType", {}) if isinstance(identity, dict) else {}
        description = " ".join(str(value) for key, value in kind_row.items() if key != "tml_elements") if isinstance(kind_row, dict) else ""
        org_num = None
        try:
            if identity.get("RegNum", {}).get("nOrgNum") is not None:
                org_num = int(identity["RegNum"]["nOrgNum"])
        except (TypeError, ValueError, AttributeError):
            pass
        if "temperature" in description.casefold():
            temperature_variable = number
            variable_scales[number] = _thermoml_unit_scale(description, "temperature") or 1.0
        elif "pressure" in description.casefold():
            pressure_variable = number
            scale = _thermoml_unit_scale(description, "pressure")
            if scale is not None:
                variable_scales[number] = scale
        elif "composition" in description.casefold() or "mole fraction" in description.casefold():
            if org_num is not None:
                composition_variables[number] = org_num

    constraint_state: dict[str, float] = {}
    for row in _as_list(block.get("Constraint")):
        if not isinstance(row, dict):
            continue
        type_obj = row.get("ConstraintID", {}).get("ConstraintType", {})
        description = " ".join(str(value) for key, value in type_obj.items() if key != "tml_elements") if isinstance(type_obj, dict) else ""
        try:
            number = float(row.get("nConstraintValue"))
        except (TypeError, ValueError):
            continue
        if "pressure" in description.casefold():
            scale = _thermoml_unit_scale(description, "pressure")
            if scale is not None:
                constraint_state["pressure"] = number * scale
        elif "temperature" in description.casefold():
            scale = _thermoml_unit_scale(description, "temperature")
            if scale is not None:
                constraint_state["temperature"] = number * scale

    output: list[dict[str, Any]] = []
    for point in _as_list(block.get("NumValues")):
        if not isinstance(point, dict):
            continue
        values: dict[int, float] = {}
        for row in _as_list(point.get("VariableValue")):
            try:
                values[int(row.get("nVarNumber"))] = float(row.get("nVarValue"))
            except (TypeError, ValueError, AttributeError):
                continue
        point_t = values.get(temperature_variable) * variable_scales[temperature_variable] if temperature_variable in values and temperature_variable in variable_scales else constraint_state.get("temperature")
        point_p = values.get(pressure_variable) * variable_scales[pressure_variable] if pressure_variable in values and pressure_variable in variable_scales else constraint_state.get("pressure")
        if point_t is None or point_p is None:
            continue
        if abs(point_t - T_K) > max(0.02, T_K * 1e-5):
            continue
        # 文献通常将常压报告为 101 kPa，故允许 0.5% 的实验状态点取整差；不允许跨越条件做外推。
        if abs(point_p - P_Pa) > max(1000.0, P_Pa * 0.005):
            continue
        composition_ok = True
        seen_org_numbers: set[int] = set()
        for variable_num, org_num in composition_variables.items():
            if variable_num not in values:
                composition_ok = False
                break
            try:
                component_index = next(index for index, mapped_org_num in component_map.items() if mapped_org_num == org_num)
            except StopIteration:
                composition_ok = False
                break
            if abs(values[variable_num] - fractions[component_index]) > 1e-4:
                composition_ok = False
                break
            seen_org_numbers.add(org_num)
        if len(component_map) == 1:
            composition_ok = True
        elif len(component_map) == 2 and len(composition_variables) == 1 and seen_org_numbers:
            composition_ok = True
        elif len(seen_org_numbers) != len(component_map):
            composition_ok = False
        if not composition_ok:
            continue
        point_properties: dict[str, Any] = {}
        for property_row in _as_list(block.get("Property")):
            if not isinstance(property_row, dict):
                continue
            kind = _thermoml_property_kind(_thermoml_property_descriptor(property_row))
            if not kind:
                continue
            conversion = _unit_conversion(kind, _thermoml_field_unit(_thermoml_property_descriptor(property_row)))
            if conversion is None:
                continue
            scale, unit = conversion
            try:
                prop_num = int(property_row.get("nPropNumber"))
                prop_point = next(row for row in _as_list(point.get("PropertyValue")) if int(row.get("nPropNumber")) == prop_num)
                prop_value = float(prop_point.get("nPropValue")) * scale
            except (TypeError, ValueError, StopIteration, AttributeError):
                continue
            if not math.isfinite(prop_value):
                continue
            combined_uncertainty = prop_point.get("CombinedUncertainty", {})
            uncertainty = combined_uncertainty.get("nCombExpandUncertValue") if isinstance(combined_uncertainty, dict) else None
            ref = {"value": prop_value, "unit": unit, "source_id": "", "reference_state": None}
            try:
                uncertainty = float(uncertainty) * scale
                if math.isfinite(uncertainty) and uncertainty >= 0:
                    ref["uncertainty"] = uncertainty
            except (TypeError, ValueError):
                pass
            point_properties[kind] = ref
        if point_properties:
            output.append(point_properties)
    return output


def _thermoml_lookup(payload: dict[str, Any], timeout_s: float = 8.0) -> dict[str, Any]:
    """使用 NIST 官方公开搜索 API + 单篇 ThermoML JSON 获取混合物实验数据。"""
    components = payload.get("components", [])
    fractions = payload.get("mole_fractions", [])
    requested = payload.get("target_properties", list(TARGET_PROPERTIES))
    try:
        if not components:
            return {"covered": False, "message": "未提供可检索的组分。", "data": {}, "records": []}
        if not any(key in {"density", "enthalpy", "viscosity", "surface_tension", "thermal_conductivity"} for key in requested):
            return {
                "covered": False,
                "message": "ThermoML 查询已连接；当前请求的物性暂无已验证的直接数值解析器，因此不发起无效的文献下载。",
                "data": {}, "records": [], "applicable": False,
                "search_url": THERMOML_SEARCH_INFO_URL, "archive_url": THERMOML_ARCHIVE_URL,
            }
        terms = [f"/Compound/\\_/sCommonName/\\_:{_lucene_term(str(item.get('canonical_name') or item.get('name') or ''))}" for item in components]
        query = "type:TRCTml4 AND (" + " AND ".join(terms) + ")"
        collected: list[dict[str, Any]] = []
        total = 0
        component_map: dict[int, int] | None = None
        inspected = 0
        # NIST 排序首批候选先验证；每次最多获取 10 个完整 JSON，避免宽查询拖慢对话。
        for page in range(1):
            search = _thermoml_search_page(query, page, 20, timeout_s)
            total = int(search.get("size", 0) or 0)
            results = search.get("results", [])
            if not results:
                break
            for result in results:
                content = result.get("content", {}) if isinstance(result, dict) else {}
                citation = content.get("Citation", {}) if isinstance(content, dict) else {}
                if not _thermoml_summary_supports_request(content.get("data_summary", {}), requested):
                    continue
                doi = str(citation.get("sDOI", "")).strip()
                if not doi or not re.match(r"^10\.\d{4,9}/", doi):
                    continue
                compound_records = _as_list(content.get("Compound"))
                searchable_names = [_thermoml_component_names(row) for row in compound_records if isinstance(row, dict)]
                matched_indices = [
                    index for index, names in enumerate(searchable_names)
                    if any(_thermoml_name_candidates(component) & names for component in components)
                ]
                # Full documents may contain several separate systems (including isotopes).
                # Require all requested compounds in the summary, then verify each data block below.
                if len(set(matched_indices)) < len(components):
                    continue
                inspected += 1
                try:
                    body = _thermoml_document(doi, timeout_s)
                except requests.Timeout:
                    raise
                except requests.RequestException:
                    continue
                component_map = _thermoml_record_component_map(body, components)
                if component_map is None:
                    continue
                # 只接受文章中恰好属于请求组分的实验块，避免把含其它溶质的三元体系误当成二元体系。
                expected_org_numbers = set(component_map.values())
                title = str(citation.get("sTitle", ""))
                article_data: dict[str, Any] = {}
                for block in _as_list(body.get("PureOrMixtureData")):
                    if not isinstance(block, dict):
                        continue
                    try:
                        block_numbers = {
                            int(row.get("RegNum", {}).get("nOrgNum"))
                            for row in _as_list(block.get("Component"))
                        }
                    except (TypeError, ValueError, AttributeError):
                        continue
                    if block_numbers != expected_org_numbers:
                        continue
                    for point_data in _thermoml_block_matches_state(
                        block, body, component_map, fractions, float(payload["T_K"]), float(payload["P_Pa"]),
                    ):
                        for prop, reference in point_data.items():
                            reference.update({
                                "source_id": f"thermoml:{doi}",
                                "doi": doi,
                                "title": title,
                                "url": f"{THERMOML_JSON_URL}/{quote(doi, safe='/.-_()')}.json",
                            })
                            article_data.setdefault(prop, reference)
                if article_data:
                    collected.append({"doi": doi, "title": title, "data": article_data})
                if len(collected) >= 3:
                    break
            break
        data: dict[str, Any] = {}
        for record in collected:
            for prop, value in record["data"].items():
                data.setdefault(prop, value)
        if data:
            return {
                "covered": True,
                "message": f"NIST ThermoML Archive 检索到 {total} 条候选记录，检查排序前 {len(results)} 条后找到 {len(collected)} 篇在指定组成、温度和压力下有精确数据点的文献。",
                "data": data,
                "records": [{"doi": row["doi"], "title": row["title"], "url": f"{THERMOML_JSON_URL}/{quote(row['doi'], safe='/.-_()')}.json"} for row in collected],
                "search_url": THERMOML_SEARCH_INFO_URL,
                "archive_url": THERMOML_ARCHIVE_URL,
                "metadata_url": THERMOML_METADATA_URL,
                "candidate_count": total,
                "documents_inspected": inspected,
            }
        if total == 0:
            message = "NIST ThermoML Archive 未检索到匹配的组分记录。"
        elif inspected == 0:
            message = "NIST ThermoML Search API 有候选记录，但未找到完整匹配的组分组合。"
        else:
            message = f"在检索排序前 {min(total, 20)} 条候选中找到相关 ThermoML 文献，但没有当前组成、温度、压力的直接数据点；未做插值或外推。" if inspected else "ThermoML 官方检索结果未发现可直接核验的组分组合。"
        return {
            "covered": False, "message": message, "data": {}, "records": [],
            "search_url": THERMOML_SEARCH_INFO_URL, "archive_url": THERMOML_ARCHIVE_URL,
            "metadata_url": THERMOML_METADATA_URL,
            "candidate_count": total, "documents_inspected": inspected,
        }
    except requests.Timeout:
        return {"covered": False, "timeout": True, "message": "NIST ThermoML API/数据文件请求超时。", "data": {}, "records": [], "search_url": THERMOML_SEARCH_INFO_URL}
    except requests.RequestException as exc:
        return {"covered": False, "message": f"NIST ThermoML 官方接口暂不可用（{exc.__class__.__name__}）。", "data": {}, "records": [], "search_url": THERMOML_SEARCH_INFO_URL}
    except (ValueError, KeyError, TypeError) as exc:
        return {"covered": False, "message": f"NIST ThermoML 响应无法安全解析（{exc.__class__.__name__}）。", "data": {}, "records": [], "search_url": THERMOML_SEARCH_INFO_URL}


def query_properties(
    components: list[str],
    mole_fractions: list[float] | None,
    T_K: float,
    P_Pa: float,
    target_properties: list[str],
) -> dict[str, Any]:
    """查询任意组分的物性；错误状态均显式返回，不使用经验兜底数值。"""
    if not isinstance(components, list) or not components or any(not isinstance(x, str) or not x.strip() for x in components):
        return _failure("needs_input", "请提供至少一个非空组分名称。")
    if mole_fractions is None:
        return _failure("needs_input", "缺少摩尔分数；请按组分输入顺序提供 mole_fractions。")
    if not isinstance(mole_fractions, list) or len(mole_fractions) != len(components):
        return _failure("needs_input", "组分数与摩尔分数个数必须一致，且顺序一一对应。")
    try:
        xs = [_finite_number(x, f"mole_fractions[{i}]") for i, x in enumerate(mole_fractions)]
        T = _finite_number(T_K, "T_K")
        P = _finite_number(P_Pa, "P_Pa")
    except ValueError as exc:
        return _failure("needs_input", str(exc))
    if any(x < 0 or x > 1 for x in xs) or abs(sum(xs) - 1.0) > 1e-6:
        return _failure("needs_input", "摩尔分数必须位于 [0,1]，且加和为 1.0（容差 1e-6）；系统不会自动归一化。")
    if T < T_MIN_K or T > T_MAX_K or P < P_MIN_PA or P > P_MAX_PA:
        return _failure("out_of_domain", f"拒绝外推：安全温度范围为 {T_MIN_K:.2f}–{T_MAX_K:.2f} K，压力范围为 {P_MIN_PA:g}–{P_MAX_PA:g} Pa。")
    if not isinstance(target_properties, list) or not target_properties:
        return _failure("needs_input", "请指定至少一个目标物性。")
    if any(not isinstance(name, str) for name in target_properties):
        return _failure("needs_input", "目标物性必须使用字符串名称。")
    invalid = [name for name in target_properties if name not in TARGET_PROPERTIES]
    if invalid:
        return _failure("needs_input", "不支持的目标物性：" + "、".join(map(str, invalid)))
    if Mixture is None or Chemical is None:
        return _failure("solver_failed", "未安装 thermo/chemicals，底层计算未执行；请按 requirements.txt 安装依赖。")
    try:
        resolved = _resolve_components(components)
    except LookupError as exc:
        return _failure("needs_input", str(exc))
    except Exception as exc:
        return _failure("solver_failed", f"组分解析失败：{exc}")

    ledger = _load_ledger()
    model, rationale, parameter_rows = _model_coverage(resolved, ledger)
    try:
        # CAS 与 xs 保持同一索引顺序，避免库内部按名称排序导致组成错配。
        mixture = Mixture(IDs=[c.cas for c in resolved], zs=xs, T=T, P=P)
        phase = str(getattr(mixture, "phase", "")).lower()
        gas_phase = phase in {"g", "gas", "v", "vapor"}
        if gas_phase:
            return _failure(
                "out_of_domain",
                "当前温度和压力下该组分处于气态，液相密度、液相粘度和表面张力不适用；请降低温度或提高压力后再查液相数据。",
                phase_state="single_gas_detected",
                components=[{"name": c.input_name, "cas": c.cas} for c in resolved],
                mole_fractions=xs,
                T_K=T,
                P_Pa=P,
            )
        if phase and phase not in {"l", "liquid"}:
            return _failure("solver_failed", "当前状态无法确认液相，未返回不可靠的液相数据。")
        bubble_temperature = _bubble_point(xs, P, resolved, model, parameter_rows, ledger, mixture)
        if T >= bubble_temperature:
            return _failure(
                "out_of_domain",
                f"当前状态已超过泡点（约 {bubble_temperature:.2f} K），物质不是液体；液相物性未返回。请降低温度或提高压力。",
                phase_state="single_gas_or_vapor_detected",
                components=[{"name": c.input_name, "cas": c.cas} for c in resolved],
                mole_fractions=xs,
                T_K=T,
                P_Pa=P,
                bubble_point_K=bubble_temperature,
            )

        # 只计算并返回用户勾选/请求的物性；泡点仍在内部计算，用于确认输入状态确实为液相。
        requested = list(dict.fromkeys(target_properties))
        property_calculators: dict[str, Any] = {
            "density": lambda: {**_number_from_mixture(mixture, "液相密度", "rhol"), "unit": "kg/m3", "source_ids": ["thermo_Mixture_default_v1"]},
            "enthalpy": lambda: {**_number_from_mixture(mixture, "摩尔焓", "Hm", 1e-3), "unit": "kJ/mol", "reference_state": "thermo 库默认焓基准；使用前须与下游模型对齐零点", "source_ids": ["thermo_Mixture_default_v1"]},
            "viscosity": lambda: {**_number_from_mixture(mixture, "动力粘度", "mul", 1e3), "unit": "mPa·s", "source_ids": ["thermo_Mixture_default_v1"]},
            "surface_tension": lambda: {**_number_from_mixture(mixture, "表面张力", "sigma", 1e3), "unit": "mN/m", "source_ids": ["thermo_Mixture_default_v1"]},
            "thermal_conductivity": lambda: {**_number_from_mixture(mixture, "热导率", "kl"), "unit": "W/(m·K)", "source_ids": ["thermo_Mixture_default_v1"]},
            "bubble_point": lambda: {"value": bubble_temperature, "unit": "K", "source_ids": [f"thermo_{model}_bubble_v1"] + [str(row["source_id"]) for row in parameter_rows]},
        }
        properties: dict[str, Any] = {key: property_calculators[key]() for key in requested}
        thermo_source = {
            "model": model,
            "label": ("NRTL (系统检测到文献参数)" if model == "NRTL" else
                      "UNIQUAC (系统检测到文献参数)" if model == "UNIQUAC" else
                      "UNIFAC (未检测到实验参数，已降级估算)" if model == "UNIFAC" else "纯组分"),
            "decision_basis": rationale,
            "parameter_source_ids": sorted({str(row["source_id"]) for row in parameter_rows}),
            "model_scope": "所选活度模型用于改进拉乌尔定律泡点计算（假设理想气相）；rho、Hm、mu、sigma、k 使用 thermo 的默认属性相关式。",
        }
        warnings = [
            "相态字段为 single_liquid_assumed；本工具未执行严格液液平衡（LLE）稳定性校核。",
            "摩尔焓采用 thermo 库默认基准；下游流程模型使用前必须对齐焓零点。",
            "泡点使用改进拉乌尔定律，气相按理想气体处理；高压或强气相非理想体系需另行校核。",
        ]
        if model == "UNIFAC":
            warnings.extend(["未找到覆盖全部二元对的文献参数，当前非理想性/泡点为 UNIFAC 模型估算，置信度低。", "低置信度，建议实验校核。"])
        elif model == "NRTL" and all(_pair_record(ledger, resolved[i].cas, resolved[j].cas, "UNIQUAC") for i in range(len(resolved)) for j in range(i + 1, len(resolved))):
            warnings.append("台账同时存在 UNIQUAC 参数；可用其进行交叉验证。")

        nist = [_nist_lookup(resolved[0], T, P)] if len(resolved) == 1 else []
        reference_request = {
            "components": [{"name": c.input_name, "canonical_name": c.canonical_name, "cas": c.cas} for c in resolved],
            "mole_fractions": xs,
            "T_K": T,
            "P_Pa": P,
            "target_properties": requested,
        }
        thermoml = (
            _thermoml_lookup(reference_request)
            if len(resolved) > 1
            else {"covered": False, "applicable": False, "message": "纯组分的参考数据由 NIST WebBook 提供。", "data": {}, "records": []}
        )
        if any(item.get("timeout") for item in nist) or thermoml.get("timeout"):
            return _failure("solver_failed", "外部参考数据库请求超时；本次查询按规则标记失败，未向界面发布部分结果。", references={"nist": nist, "thermoml": thermoml})
        source_references = {
            "nist": {key: value for key, value in (nist[0].get("data", {}) if nist else {}).items() if key in requested},
            "thermoml": dict(thermoml.get("data", {})) if isinstance(thermoml.get("data", {}), dict) else {},
        }
        references = {prop: item for source in source_references.values() for prop, item in source.items()}
        comparison_warnings: list[str] = []
        for source_name, source_data in source_references.items():
            for prop, ref in source_data.items():
                if prop not in properties or not isinstance(ref, dict) or isinstance(ref.get("value"), bool):
                    continue
                try:
                    ref_value = float(ref["value"])
                except (KeyError, TypeError, ValueError):
                    continue
                if not math.isfinite(ref_value):
                    continue
                properties[prop].setdefault("external_references", {})[source_name] = ref
                reference_basis_matches = (
                    prop != "enthalpy"
                    or ref.get("reference_state") == properties[prop].get("reference_state")
                )
                if not reference_basis_matches:
                    comparison_warnings.append("焓参考态未确认一致，已展示参考值但不计算相对偏差。")
                elif ref_value != 0 and _unit_key(ref.get("unit")) == _unit_key(properties[prop]["unit"]):
                    model_value = properties[prop]["value"]
                    deviation = abs(model_value - ref_value) / abs(ref_value) * 100.0
                    properties[prop].setdefault("relative_deviation_pct_by_source", {})[source_name] = deviation
                    properties[prop].setdefault("deviation_warning_by_source", {})[source_name] = deviation > 5.0
                    uncertainty = ref.get("uncertainty")
                    if isinstance(uncertainty, (int, float)) and not isinstance(uncertainty, bool) and uncertainty >= 0:
                        properties[prop].setdefault("within_reference_uncertainty_by_source", {})[source_name] = abs(model_value - ref_value) <= float(uncertainty)
                elif _unit_key(ref.get("unit")) != _unit_key(properties[prop]["unit"]):
                    comparison_warnings.append(f"{TARGET_PROPERTIES[prop]}参考值单位不匹配，已展示但不计算偏差。")
                properties[prop].setdefault("reference", ref)
        if not references:
            warnings.append("外部参考源没有返回本状态点可直接比较的数值；参考栏标明未覆盖，不以模型值替代。")
        warnings.extend(comparison_warnings)
        return {
            "status": "ok",
            "components": [{"name": c.input_name, "cas": c.cas} for c in resolved],
            "mole_fractions": xs,
            "T_K": T,
            "P_Pa": P,
            "properties": properties,
            "requested_properties": requested,
            "phase_state": "single_liquid_assumed",
            "thermo_model": thermo_source,
            "references": {"nist": nist, "thermoml": thermoml},
            "warnings": warnings,
        }
    except Exception as exc:
        return _failure("solver_failed", f"底层物性计算失败；没有返回猜测值。原因：{exc.__class__.__name__}: {exc}")


def query_properties_tool_schema() -> dict[str, Any]:
    """返回供兼容 OpenAI Chat Completions 工具调用的函数定义。"""
    return {
        "type": "function",
        "function": {
            "name": "query_properties",
            "description": "查询任意化工组分或混合物的物性数据，支持多源对比。",
            "parameters": {
                "type": "object",
                "properties": {
                    "components": {"type": "array", "items": {"type": "string"}, "description": "组分名称列表，顺序由用户输入决定并严格保留。"},
                    "mole_fractions": {"type": "array", "items": {"type": "number"}, "description": "与组分一一对应的摩尔分数，纯物质为 [1.0]。"},
                    "T_K": {"type": "number", "description": "温度，K。"},
                    "P_Pa": {"type": "number", "description": "压力，Pa。"},
                    "target_properties": {"type": "array", "items": {"type": "string", "enum": list(TARGET_PROPERTIES)}, "description": "目标物性。"},
                },
                "required": ["components", "mole_fractions", "T_K", "P_Pa", "target_properties"],
                "additionalProperties": False,
            },
        },
    }
