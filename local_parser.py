"""无模型凭据时的保守本地解析器。

仅接受别名表中精确出现的组分，以及明确写出的摩尔比、温度、压力和目标属性。
解析不完整或有歧义时返回澄清信息，不猜测缺失数据。
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    from chemicals.identifiers import int_to_CAS, search_chemical
except ImportError:  # 本地规则仍可解析别名表中明确配置的组分
    int_to_CAS = None
    search_chemical = None


NUMBER = r"(?:\d+(?:\.\d*)?|\.\d+)"
DELIMITER_RE = re.compile(r"[,，、;；\r\n]+")
NON_COMPONENT_WORDS = {
    "a", "an", "the", "and", "or", "with", "of", "in", "at", "under", "for", "by", "to", "from",
    "find", "query", "search", "check", "calculate", "please", "give", "show", "properties", "property",
    "density", "enthalpy", "viscosity", "bubble", "point", "surface", "tension", "thermal", "conductivity",
    "full", "complete", "data", "all", "liquid", "mixture", "mole", "fraction", "fractions", "ratio",
    "k", "c", "f", "pa", "kpa", "mpa", "bar", "atm", "mol", "mol%", "temperature", "pressure",
}


def split_values(value: str) -> list[str]:
    """结构化表单统一接受中英文标点分隔。"""
    return [part.strip() for part in DELIMITER_RE.split(value or "") if part.strip()]


def _read_aliases() -> dict[str, str]:
    path = Path(__file__).with_name("component_aliases.json")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    aliases = payload.get("aliases", {}) if isinstance(payload, dict) else {}
    return {str(alias): str(cas) for alias, cas in aliases.items()} if isinstance(aliases, dict) else {}


def _ordered_component_matches(text: str, aliases: dict[str, str]) -> list[str]:
    candidates: list[tuple[int, int, str, str]] = []
    folded = text.casefold()
    for alias, cas in aliases.items():
        if not alias.strip():
            continue
        for match in re.finditer(re.escape(alias.casefold()), folded):
            candidates.append((match.start(), match.end(), alias, cas))
    # 对别名表之外的英文名，用 chemicals 自带物质索引逐词/短语检索，仍然不调用网络或 LLM。
    if search_chemical is not None:
        tokens = list(re.finditer(r"[A-Za-z][A-Za-z0-9'()\-/]*", text))
        for start_index, token in enumerate(tokens):
            for size in range(min(3, len(tokens) - start_index), 0, -1):
                span = tokens[start_index:start_index + size]
                if any(item.group(0).casefold() in NON_COMPONENT_WORDS for item in span):
                    continue
                # 不跨越自然语言连接词或中英文标点组合成一个虚假物质名。
                if any(re.search(r"[^\s]", text[left.end():right.start()]) and not re.fullmatch(r"\s+", text[left.end():right.start()]) for left, right in zip(span, span[1:])):
                    continue
                phrase = " ".join(item.group(0) for item in span)
                normalized_phrase = phrase.casefold()
                if normalized_phrase in aliases or not _dynamic_name_is_candidate(phrase):
                    continue
                if normalized_phrase in NON_COMPONENT_WORDS:
                    continue
                candidates.append((span[0].start(), span[-1].end(), phrase, _dynamic_name_is_candidate(phrase)))
    candidates.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    selected: list[tuple[int, int, str, str]] = []
    for candidate in candidates:
        start, end, _, cas = candidate
        if any(start < chosen[1] and end > chosen[0] for chosen in selected):
            continue
        if any(chosen[3] == cas for chosen in selected):
            continue
        selected.append(candidate)
    selected.sort(key=lambda item: item[0])
    return [item[2] for item in selected]


@lru_cache(maxsize=512)
def _dynamic_name_is_candidate(phrase: str) -> str:
    """返回已被本地 chemicals 索引识别的英文组分 CAS；无法识别时为空字符串。"""
    if search_chemical is None:
        return ""
    try:
        record = search_chemical(phrase)
        raw_cas = getattr(record, "CAS", "")
        if raw_cas and str(raw_cas).isdigit() and int_to_CAS is not None:
            return str(int_to_CAS(int(raw_cas)))
        return str(raw_cas or "").strip()
    except Exception:
        return ""


def parse_explicit_query(text: str) -> tuple[dict[str, Any] | None, str]:
    """解析全部关键字段明确的短查询；不完整时返回逐项澄清提示。"""
    aliases = _read_aliases()
    names = _ordered_component_matches(text, aliases)
    missing: list[str] = []
    if not names:
        missing.append("组分名称（目前本地快捷解析识别别名表中的名称）")

    ratio_match = re.search(
        rf"(?:摩尔比|摩尔分数|组成|比例|mole\s*(?:ratio|fractions?))[^\d]{{0,16}}((?:{NUMBER}\s*[:：/]\s*)+{NUMBER})",
        text,
        flags=re.IGNORECASE,
    )
    fractions: list[float] = []
    if ratio_match is None and len(names) > 1:
        # 自然语言常见表达：“乙醇0.4、水0.6”；数值需紧跟组分，且必须全部组分都显式给出。
        values_by_name: dict[str, float] = {}
        for name in names:
            cas = aliases.get(name.casefold())
            alias_forms = sorted((alias for alias, alias_cas in aliases.items() if cas and alias_cas == cas), key=len, reverse=True)
            forms = {name.casefold(), *alias_forms}
            escaped = "(?:" + "|".join(re.escape(form) for form in sorted(forms, key=len, reverse=True)) + ")"
            match = re.search(rf"{escaped}\s*(?:摩尔分数\s*)?(?:为|是|=|:)?\s*({NUMBER})\s*(?:mol%|mol fraction|摩尔分数)?", text, flags=re.IGNORECASE)
            if match:
                values_by_name[name.casefold()] = float(match.group(1))
        if len(values_by_name) == len(names):
            fractions = [values_by_name[name.casefold()] for name in names]
    if len(names) == 1:
        fractions = [1.0]
    elif ratio_match:
        try:
            ratio = [float(part) for part in re.split(r"\s*[:：/]\s*", ratio_match.group(1))]
            if len(ratio) == len(names) and all(part >= 0 for part in ratio) and sum(ratio) > 0:
                denominator = sum(ratio)
                fractions = [part / denominator for part in ratio]
        except ValueError:
            fractions = []
    if names and len(names) > 1 and not fractions:
        # 英文/中文自然语言中常见“water and toluene”没有组成数据时，不擅自按等摩尔处理。
        missing.append("与组分顺序一致的摩尔比/摩尔分数")

    temperature_match = re.search(rf"({NUMBER})\s*(°\s*[CF]|℃|℉|摄氏度|华氏度|K(?![A-Za-z])|开尔文)", text, flags=re.IGNORECASE)
    temperature = None
    if temperature_match:
        value = float(temperature_match.group(1))
        unit = temperature_match.group(2).replace(" ", "").casefold()
        if unit in {"°c", "℃", "摄氏度"}:
            temperature = value + 273.15
        elif unit in {"°f", "℉", "华氏度"}:
            temperature = (value - 32) * 5 / 9 + 273.15
        else:
            temperature = value
    if temperature is None:
        missing.append("温度（K）")

    pressure = None
    if any(token in text.casefold() for token in ("常压", "大气压", "atmospheric pressure")):
        pressure = 101325.0
    else:
        pressure_match = re.search(rf"({NUMBER})\s*(MPa|kPa|Pa|bar|atm)(?![A-Za-z])", text, flags=re.IGNORECASE)
        if pressure_match:
            value = float(pressure_match.group(1))
            scale = {"mpa": 1e6, "kpa": 1e3, "pa": 1.0, "bar": 1e5, "atm": 101325.0}
            pressure = value * scale[pressure_match.group(2).casefold()]
    if pressure is None:
        missing.append("压力（Pa）或明确的‘常压’")

    all_requested = any(word in text.casefold() for word in ("全套", "全部物性", "所有物性", "完整数据包", "流程设计所需物性"))
    property_terms = {
        "density": ("密度", "density"),
        "enthalpy": ("焓", "enthalpy"),
        "viscosity": ("粘度", "黏度", "viscosity"),
        "bubble_point": ("泡点", "bubble point"),
        "surface_tension": ("表面张力", "surface tension"),
        "thermal_conductivity": ("热导率", "导热系数", "thermal conductivity"),
    }
    targets = list(property_terms) if all_requested else [
        prop for prop, terms in property_terms.items()
        if any(term in text.casefold() for term in terms)
    ]
    if not targets:
        missing.append("目标物性，或明确要求‘全套物性’")

    if missing:
        return None, "本地解析不会补猜参数。请补充：" + "；".join(missing) + "。"
    return {
        "components": names,
        "mole_fractions": fractions,
        "T_K": temperature,
        "P_Pa": pressure,
        "target_properties": targets,
    }, "已从明确输入中提取参数；数值仍由物性后端计算。"
