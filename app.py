"""物性数据 Web Agent：本地优先解析、固定模型辅助与独立结构化查询。"""

from __future__ import annotations

import os
import re
from typing import Any

import streamlit as st

from llm_client import LLMServiceError, run_tool_loop
from local_parser import parse_explicit_query, split_values
from simulation_ui import render_simulation_page
from tools import TARGET_PROPERTIES, query_properties


# 固定服务商设置；密钥仅从本机 Streamlit secrets 或进程环境读取，不写入代码。
LLM_BASE_URL = "https://api.bblabu.ai/v1"
LLM_MODEL_NAME = "deepseek-v4.1-flash"


def _load_llm_config() -> dict[str, str]:
    try:
        api_key = str(st.secrets.get("API_KEY", "")).strip()
    except Exception:
        api_key = ""
    api_key = api_key or os.getenv("API_KEY", "").strip()
    return {"base_url": LLM_BASE_URL, "model": LLM_MODEL_NAME, "api_key": api_key}


def _safe_clarification(text: str) -> str:
    """防止把模型普通文本中的数字误当作可信物性结果展示。"""
    if re.search(r"\d|kg/m|kJ/mol|mPa|mN/m|W/\(|\bK\b|\bPa\b|%", text, flags=re.IGNORECASE):
        return "请补充完整查询参数；物性数值只能由后端工具计算后展示。"
    return text


def _compact_notice(text: str, *, success: bool = False) -> str:
    """主页面只显示简短状态；具体错误/数值统一放入“查看详情”面板。"""
    if success:
        return "查询已完成。请点击右侧“查看详情”查看数据、来源和提醒。"
    if not text:
        return "本次查询未完成。请点击右侧“查看详情”查看原因。"
    first_line = str(text).splitlines()[0].strip()
    if len(first_line) > 90:
        first_line = first_line[:87] + "…"
    return f"本次查询未完成：{first_line}。详细原因请点击右侧“查看详情”。"


def _missing_parameter_warning(prompt: str, backend_message: str = "") -> str:
    """把后端/模型的泛化缺参信息整理成用户可直接补填的清单。"""
    _, local_message = parse_explicit_query(prompt)
    if "请补充：" in local_message:
        missing = local_message.split("请补充：", 1)[1].rstrip("。")
        return f"无法开始查询，还缺少：{missing}。请补齐后重新提交。"

    message = str(backend_message or "")
    # 工具校验信息可能只指出一项；保留原意，同时把提示说清楚。
    replacements = (
        ("缺少摩尔分数", "摩尔分数/摩尔比"),
        ("请提供至少一个非空组分名称", "组分名称"),
        ("请指定至少一个目标物性", "目标物性"),
        ("缺少温度", "温度（K）"),
        ("缺少压力", "压力（Pa）或明确的‘常压’"),
    )
    for source, item in replacements:
        if source in message:
            return f"无法开始查询，还缺少：{item}。请补齐后重新提交。"
    if message:
        return f"无法开始查询：{message}。请补齐缺少参数后重新提交。"
    return "无法开始查询，还缺少必要参数。请补充组分、摩尔分数、温度、压力和目标物性。"


def _normalization_notice(result: dict[str, Any]) -> str:
    """为已归一化的结果生成主页面上的简短检查提醒。"""
    raw = result.get("input_mole_fractions")
    normalized = result.get("mole_fractions")
    if not isinstance(raw, list) or not isinstance(normalized, list) or not raw:
        return ""
    if abs(sum(float(value) for value in raw) - 1.0) <= 1e-6:
        return ""
    total = sum(float(value) for value in raw)
    return f"组分组成总和为 {total:.6g}，已自动归一化后计算；请检查输入组成是否正确。"


def _reference_for(property_data: dict[str, Any], source: str) -> dict[str, Any] | None:
    refs = property_data.get("external_references", {})
    item = refs.get(source) if isinstance(refs, dict) else None
    if isinstance(item, dict):
        return item
    legacy = property_data.get("reference")
    return legacy if source == "nist" and isinstance(legacy, dict) else None


def _format_value(value: Any, unit: str = "") -> str:
    if isinstance(value, (int, float)):
        return f"{value:.6g} {unit}".strip()
    return "—"


def _bare_value(value: Any) -> str:
    return f"{value:.6g}" if isinstance(value, (int, float)) else "—"


def _render_issue(message: str, kind: str = "error") -> None:
    """将单条执行错误收进按需展开的简短详情中。"""
    if kind == "notice":
        st.caption("本次查询：🔴 0 项错误，🟡 1 项提醒。")
    else:
        st.caption("本次查询：🔴 1 项错误，🟡 0 项提醒。")
    with st.expander("查看详情", expanded=False):
        (st.info if kind == "notice" else st.error)(message)


def _warning_summary(result: dict[str, Any]) -> tuple[list[str], list[str]]:
    """將錯誤和提醒分級並壓縮為一行摘要；詳細內容留在可展開區。"""
    errors: list[str] = []
    notices = list(result.get("warnings", []))
    for prop, item in result.get("properties", {}).items():
        for source_name, high in item.get("deviation_warning_by_source", {}).items():
            if high:
                notices.append(f"{TARGET_PROPERTIES.get(prop, prop)} 与 {source_name} 的相对偏差超过 5%")
        if item.get("deviation_warning"):
            notices.append(f"{TARGET_PROPERTIES.get(prop, prop)} 与参考值的相对偏差超过 5%")
    if result.get("status") == "solver_failed":
        errors.append(str(result.get("message", "物性计算未成功。")))
    thermoml = result.get("references", {}).get("thermoml", {})
    nist_items = result.get("references", {}).get("nist", [])
    for source in [*nist_items, thermoml]:
        message = source.get("message") if isinstance(source, dict) else None
        if isinstance(source, dict) and source.get("applicable") is False:
            continue
        if message and not source.get("found", source.get("covered", True)):
            notices.append(message)
    return errors, list(dict.fromkeys(notices))


def _coverage_label(nist_status: Any, thermoml_status: Any, pure_component: bool) -> str:
    if pure_component:
        entries = nist_status if isinstance(nist_status, list) else []
        count = sum(len(item.get("data", {})) for item in entries if isinstance(item, dict))
        nist_label = f"{count} 项" if count else "未命中"
        thermoml_label = "不适用"
    else:
        nist_label = "不适用"
        data = thermoml_status.get("data", {}) if isinstance(thermoml_status, dict) else {}
        if isinstance(thermoml_status, dict) and thermoml_status.get("applicable") is False:
            thermoml_label = "暂不支持该物性的直接解析"
        else:
            if data:
                thermoml_label = f"{len(data)} 项"
            elif isinstance(thermoml_status, dict) and thermoml_status.get("candidate_count"):
                thermoml_label = (
                    f"未命中状态点（候选 {thermoml_status.get('candidate_count')}，"
                    f"已核验 {thermoml_status.get('documents_inspected', 0)} 篇）"
                )
            else:
                thermoml_label = "未命中状态点"
    return f"参考源状态：NIST WebBook {nist_label} · ThermoML {thermoml_label}（详情可展开）"


def render_result(result: dict[str, Any]) -> None:
    """将工具返回的确定数值渲染为并排来源卡；LLM 输出不进入数值卡。"""
    status = result.get("status", "solver_failed")
    if status != "ok":
        message = f"{status}: {result.get('message', '')}"
        if result.get("phase_state") in {"single_gas_detected", "single_gas_or_vapor_detected"}:
            st.caption("当前状态：气态；液相物性未返回。")
            partial = result.get("partial_properties", {})
            if partial:
                st.caption("详情中保留了当前气相下仍可计算的部分数据。")
                partial_rows = [{
                    "物性（单位）": f"{TARGET_PROPERTIES.get(key, key)} ({item.get('unit', '')})",
                    "当前状态值": _bare_value(item.get("value")),
                    "相态": item.get("phase", "当前状态"),
                    "来源": ", ".join(item.get("source_ids", [])) or "—",
                } for key, item in partial.items()]
                st.dataframe(partial_rows, width="stretch", hide_index=True)
        if status in {"needs_input", "out_of_domain"}:
            st.caption("本次查询：🔴 0 项错误，🟡 1 项需要补充或检查。")
            with st.expander("查看详情", expanded=False):
                st.warning(result.get("message", "请检查或补充输入参数。"))
        else:
            st.caption("本次查询：🔴 1 项错误，🟡 0 项提醒。")
            with st.expander("查看详情", expanded=False):
                st.error(message)
        return

    model = result["thermo_model"]
    st.subheader("物性结果")
    st.markdown(f"**【热力学模型】: {model['label']}**")
    st.caption(model["decision_basis"] + "。" + model["model_scope"])
    st.caption(
        "组分顺序：" + "、".join(item["name"] for item in result["components"])
        + "；摩尔分数：" + ", ".join(f"{x:.6g}" for x in result["mole_fractions"])
    )
    nist_status = result.get("references", {}).get("nist", [])
    thermoml_status = result.get("references", {}).get("thermoml", {})
    pure_component = len(result.get("components", [])) == 1
    model_rows: list[dict[str, str]] = []
    source_rows: dict[str, list[dict[str, str]]] = {"nist": [], "thermoml": []}
    source_labels = {"nist": "NIST WebBook", "thermoml": "NIST ThermoML"}
    requested_properties = result.get("requested_properties", list(result.get("properties", {})))
    for key in requested_properties:
        label = TARGET_PROPERTIES.get(key, key)
        item = result["properties"].get(key)
        if not item:
            continue
        property_label = f"{label} ({item.get('unit', '')})"
        model_source = ", ".join(item.get("source_ids", [])) or "thermo"
        model_rows.append({"物性（单位）": property_label, "thermo 模型值": _bare_value(item.get("value")), "计算来源": model_source})
        for source in ("nist", "thermoml"):
            reference = _reference_for(item, source)
            deviation = item.get("relative_deviation_pct_by_source", {}).get(source)
            within = item.get("within_reference_uncertainty_by_source", {}).get(source)
            deviation_label = f"{deviation:.2f}%" if deviation is not None else "不比较"
            if deviation is not None and item.get("deviation_warning_by_source", {}).get(source):
                deviation_label += " ⚠"
            uncertainty_label = "—" if within is None else "在区间内" if within else "超出区间"
            source_rows[source].append({
                "物性（单位）": property_label,
                "参考值": _bare_value(reference.get("value")) if reference else "未覆盖",
                "相对 thermo 偏差": deviation_label if reference else "未覆盖",
                "不确定度核对": uncertainty_label,
                "来源标识": (reference.get("doi") or reference.get("source_id") or "—") if reference else "—",
            })
    st.dataframe(model_rows, width="stretch", hide_index=True)
    if pure_component:
        nist_errors = [item.get("message", "") for item in nist_status if isinstance(item, dict) and item.get("unavailable")]
        if nist_errors:
            st.caption("参考源状态：NIST WebBook 连接失败（此次无法判断是否覆盖）；ThermoML 不适用。")
        else:
            st.caption(_coverage_label(nist_status, thermoml_status, pure_component))
    else:
        st.caption(_coverage_label(nist_status, thermoml_status, pure_component))
    for source in ("nist", "thermoml"):
        applicable = not (source == "nist" and not pure_component) and not (
            source == "thermoml" and pure_component
        )
        label = source_labels[source]
        with st.expander(f"{label} 来源对比（点击展开）", expanded=False):
            if not applicable:
                st.caption("该数据源不适用于当前物质类型。")
            else:
                if source_rows[source]:
                    st.dataframe(source_rows[source], width="stretch", hide_index=True)
                else:
                    st.caption("当前请求的物性没有可供比较的参考数据。")
                if source == "thermoml" and isinstance(thermoml_status, dict):
                    for record in thermoml_status.get("records", []):
                        if isinstance(record, dict) and record.get("url"):
                            st.markdown(f"[文献 {record.get('doi', '')}]({record['url']}) · {record.get('title', '')}")
            st.caption("偏差以左侧 thermo 模型值为基准，按该来源逐项比较；thermo 本身不重复计算自身偏差。偏差仅呈现数据差异，不裁决来源优劣；参考态或单位不一致时不计算偏差。")

    errors, notices = _warning_summary(result)
    if errors or notices:
        st.caption(f"本次查询：🔴 {len(errors)} 项错误，🟡 {len(notices)} 项提醒。")
        with st.expander("查看详情", expanded=False):
            if errors:
                st.error("\n\n".join(errors))
            if notices:
                st.info("\n\n".join(notices))
            source_rows = []
            for source_name, source_value in (("NIST WebBook", nist_status), ("NIST ThermoML", thermoml_status)):
                if isinstance(source_value, list):
                    message = "；".join(entry.get("message", "") for entry in source_value if isinstance(entry, dict)) or "混合体系不适用纯组分查询接口。"
                    covered = any(entry.get("found", False) for entry in source_value if isinstance(entry, dict))
                    unavailable = any(entry.get("unavailable", False) or entry.get("timeout", False) for entry in source_value if isinstance(entry, dict))
                    source_id = "; ".join(entry.get("source_id", "") for entry in source_value if isinstance(entry, dict) and entry.get("source_id"))
                    not_applicable = not source_value
                else:
                    message = source_value.get("message", "") if isinstance(source_value, dict) else ""
                    covered = bool(source_value.get("covered", False)) if isinstance(source_value, dict) else False
                    unavailable = bool(source_value.get("timeout", False)) if isinstance(source_value, dict) else False
                    source_id = "; ".join(row.get("doi", "") for row in source_value.get("records", []) if isinstance(row, dict)) if isinstance(source_value, dict) else ""
                    not_applicable = isinstance(source_value, dict) and source_value.get("applicable") is False
                source_rows.append({"数据源": source_name, "状态": "不适用" if not_applicable else "已覆盖" if covered else "连接失败/暂不可用" if unavailable or "请求失败" in message or "暂不可用" in message else "未命中可比数值", "来源/DOI": source_id or "—", "说明": message or "—"})
            st.dataframe(source_rows, width="stretch", hide_index=True)
            if isinstance(thermoml_status, dict):
                st.markdown(f"[NIST ThermoML 搜索说明]({thermoml_status.get('search_url', 'https://trc.nist.gov/ThermoML/Search')}) · [完整数据归档]({thermoml_status.get('archive_url', 'https://data.nist.gov/od/id/mds2-2422')})")
                for record in thermoml_status.get("records", []):
                    if isinstance(record, dict) and record.get("url"):
                        st.markdown(f"- [{record.get('doi', 'ThermoML 文献')}]({record['url']}) · {record.get('title', '')}")
    with st.expander("参数台账映射", expanded=False):
        st.json({
            "model": model.get("model"),
            "decision_basis": model.get("decision_basis"),
            "parameter_source_ids": model.get("parameter_source_ids", []),
            "components": result["components"],
        })


st.set_page_config(page_title="物性数据查询", page_icon="⚗️", layout="centered")
st.markdown("""
<style>
/* 学术数据工作台：低饱和底色、窄边框和稳定的表格层级，避免营销式渐变。 */
.block-container {max-width: 1080px; padding-top: 2rem; padding-bottom: 3rem;}
[data-testid="stAppViewContainer"] {background:#eef2f5;}
[data-testid="stHeader"] {background:transparent;}
.hero-title {text-align:center; font-size:2rem; font-weight:700; letter-spacing:0; margin:.55rem 0 .3rem; color:#17324d;}
.hero-caption {text-align:center; color:#536779; margin:0 auto 1.55rem; max-width:720px; font-size:.94rem;}
div[data-testid="stForm"] {border:1px solid #cbd6df; border-radius:8px; padding:1rem; background:#ffffff; box-shadow:0 3px 12px rgba(22,48,72,.07);}
div[data-testid="stPopover"] button {border-radius:7px; border:1px solid #b9c8d5; color:#17324d;}
div[data-testid="stFormSubmitButton"] button, div[data-testid="stButton"] button[kind="primary"] {border-radius:7px; background:#176b78; border-color:#176b78;}
[data-testid="stDataFrame"] {border:1px solid #cbd6df; border-radius:7px; background:#fff;}
div[data-testid="stExpander"] {border:1px solid #cbd6df; border-radius:7px; background:#fff;}
div[data-testid="stAlert"] {border-radius:7px;}
div[data-testid="stButton"] > button, div[data-testid="stDownloadButton"] > button {
  min-height:2.65rem; border:1px solid #b9c8d2; border-radius:5px;
  background:#fbfcfd; color:#17324d; font-weight:600;
  box-shadow:0 1px 2px rgba(20,45,64,.08); transition:background-color .15s ease,border-color .15s ease,box-shadow .15s ease;
}
div[data-testid="stButton"] > button:hover:not(:disabled), div[data-testid="stDownloadButton"] > button:hover:not(:disabled) {
  border-color:#176b78; background:#edf5f5; color:#123f49; box-shadow:0 2px 5px rgba(20,45,64,.12);
}
div[data-testid="stButton"] > button:focus-visible, div[data-testid="stDownloadButton"] > button:focus-visible {
  outline:3px solid rgba(23,107,120,.22); outline-offset:2px;
}
div[data-testid="stButton"] button[kind="primary"] {
  color:#fff; background:#176b78; border-color:#176b78;
}
div[data-testid="stButton"] button[kind="primary"]:hover:not(:disabled) {
  color:#fff; background:#125966; border-color:#125966;
}
@media (max-width: 640px) {
  .block-container {padding:1.1rem .8rem 2rem;}
  .hero-title {font-size:1.6rem;}
}
</style>
""", unsafe_allow_html=True)
selected_view = st.sidebar.radio("工作模块", ["物性数据查询", "流程模拟调用"])
if selected_view == "流程模拟调用":
    render_simulation_page()
    st.stop()
st.markdown('<div class="hero-title">⚗️ 物性数据查询</div>', unsafe_allow_html=True)
st.markdown('<div class="hero-caption">描述组分、配比、温度和压力即可查询。系统会整理单位与参数，物性数值由后端计算。</div>', unsafe_allow_html=True)
llm_config = _load_llm_config()

if "chat_history" not in st.session_state:
    st.session_state.chat_history = []
if "results" not in st.session_state:
    st.session_state.results = []
st.session_state.results = [
    entry if isinstance(entry, dict) and "result" in entry
    else {"prompt": f"查询记录 {index + 1}", "result": entry}
    for index, entry in enumerate(st.session_state.results)
]
if "query_notice" not in st.session_state:
    st.session_state.query_notice = ""
if "input_warning" not in st.session_state:
    st.session_state.input_warning = ""

# 查询记录只保存在会话状态和“查看详情”面板；主页面不渲染历史输入气泡。
# 这样连续查询时不会把用户之前输入的一条条内容铺在页面上。
# 自然语言输入与提交居中；结果明细统一收进右侧详情按钮。
input_col, detail_col = st.columns([10, 1.3], vertical_alignment="bottom")
with input_col:
    with st.form("natural_query_form", clear_on_submit=True, border=False):
        prompt_col, button_col = st.columns([8, 1.2], vertical_alignment="bottom")
        with prompt_col:
            prompt = st.text_input(
                "自然语言查询",
                placeholder="例如：查 335 K、常压下纯乙酸乙酯的密度和泡点",
                label_visibility="collapsed",
            )
        with button_col:
            submitted_prompt = st.form_submit_button("查询", type="primary", use_container_width=True)

if submitted_prompt and prompt.strip():
    prompt = prompt.strip()
    st.session_state.input_warning = ""
    st.session_state.chat_history.append({"role": "user", "content": prompt})
    try:
        history = [
            {"role": item["role"], "content": item["content"]}
            for item in st.session_state.chat_history[-8:]
        ][:-1]
        query_results, response = run_tool_loop(prompt, history, llm_config)
        if query_results:
            for result in query_results:
                st.session_state.results.append({"prompt": prompt, "result": result})
            if all(item.get("status") == "ok" for item in query_results):
                st.session_state.query_notice = _compact_notice("", success=True)
                st.session_state.chat_history.append({"role": "assistant", "content": "查询已完成。请点击右侧“查看详情”查看结果。"})
            else:
                response_text = str(response)
                if any(item.get("status") == "needs_input" for item in query_results):
                    st.session_state.input_warning = _missing_parameter_warning(prompt, response_text)
                st.session_state.query_notice = _compact_notice(response_text)
                st.session_state.chat_history.append({"role": "assistant", "content": str(response)})
        else:
            safe = _safe_clarification(response)
            st.session_state.results.append({"prompt": prompt, "result": {"status": "needs_input", "message": safe}})
            st.session_state.input_warning = _missing_parameter_warning(prompt, safe)
            st.session_state.query_notice = _compact_notice(safe)
            st.session_state.chat_history.append({"role": "assistant", "content": safe})
    except LLMServiceError as exc:
        parsed, local_message = parse_explicit_query(prompt)
        if parsed is not None:
            local_result = query_properties(**parsed)
            st.session_state.results.append({"prompt": prompt, "result": local_result})
            if local_result.get("status") == "needs_input":
                st.session_state.input_warning = _missing_parameter_warning(
                    prompt, str(local_result.get("message", ""))
                )
            st.session_state.query_notice = "语言解析服务暂不可用，已使用本地规则处理。请点击右侧“查看详情”。"
            st.session_state.chat_history.append({"role": "assistant", "content": st.session_state.query_notice})
        else:
            failure = "语言解析服务暂不可用。" + local_message
            st.session_state.results.append({"prompt": prompt, "result": {"status": "needs_input", "message": failure}})
            st.session_state.input_warning = _missing_parameter_warning(prompt, failure)
            st.session_state.query_notice = _compact_notice(failure)
            st.session_state.chat_history.append({"role": "assistant", "content": st.session_state.query_notice})
    except Exception as exc:
        st.session_state.input_warning = "查询暂未完成，请检查组分、摩尔分数、温度、压力和目标物性是否填写完整。"
        st.session_state.query_notice = f"本次查询未完成：{exc.__class__.__name__}: {exc}"
        st.session_state.chat_history.append({"role": "assistant", "content": st.session_state.query_notice})

# 归一化不阻断计算，但在查询区下方明确提醒用户复核原始组成。
if st.session_state.results:
    latest_result = st.session_state.results[-1].get("result", {})
    notice = _normalization_notice(latest_result) if isinstance(latest_result, dict) else ""
    if notice:
        st.warning(notice)

with detail_col:
    natural_entries = [
        entry for entry in st.session_state.results
        if not (isinstance(entry, dict) and str(entry.get("prompt", "")).startswith("结构化查询："))
    ]
    if natural_entries:
        with st.popover("查看详情", use_container_width=True):
            normalized_entries = natural_entries
            if len(normalized_entries) > 1:
                selected_index = st.selectbox(
                    "选择查询记录",
                    options=list(range(len(normalized_entries))),
                    index=len(normalized_entries) - 1,
                    format_func=lambda index: normalized_entries[index]["prompt"],
                    # 记录数变化时生成新 key，确保新查询自动选中最新一条。
                    key=f"natural_detail_selector_{len(normalized_entries)}",
                )
            else:
                selected_index = 0
            st.caption(normalized_entries[selected_index]["prompt"])
            render_result(normalized_entries[selected_index]["result"])
    else:
        st.button("查看详情", disabled=True, use_container_width=True)

# 不在主页面渲染查询状态气泡；成功、警告和错误均在对应详情面板中查看。

st.markdown("<div style='height:.7rem'></div>", unsafe_allow_html=True)
structured_col, structured_detail_col = st.columns([1, 1], vertical_alignment="top")
with structured_col:
  with st.popover("＋ 结构化查询", use_container_width=True):
    with st.form("property_query_form", clear_on_submit=False):
        component_text = st.text_input("组分名称", placeholder="例如：水，乙醇")
        fractions_text = st.text_input("摩尔分数", placeholder="例如：0.6，0.4（顺序与组分一致）")
        col_t, col_p = st.columns(2)
        with col_t:
            temperature = st.number_input("温度 (K)", min_value=0.0, value=298.15, step=1.0)
        with col_p:
            pressure = st.number_input("压力 (Pa)", min_value=0.0, value=101325.0, step=1000.0)
        target_keys = list(TARGET_PROPERTIES)
        selected_properties = st.multiselect(
            "仅返回所选物性", options=target_keys,
            default=target_keys, format_func=lambda key: TARGET_PROPERTIES[key],
        )
        submitted_structured = st.form_submit_button("查询物性", type="primary", use_container_width=True)

if st.session_state.input_warning:
    st.warning(st.session_state.input_warning)

with structured_detail_col:
    structured_entries = [
        entry for entry in st.session_state.results
        if isinstance(entry, dict) and str(entry.get("prompt", "")).startswith("结构化查询：")
    ]
    if structured_entries:
        with st.popover("结构化结果详情", use_container_width=True):
            if len(structured_entries) > 1:
                selected_structured_index = st.selectbox(
                    "选择结构化查询记录",
                    options=list(range(len(structured_entries))),
                    index=len(structured_entries) - 1,
                    format_func=lambda index: structured_entries[index]["prompt"],
                    key=f"structured_detail_selector_{len(structured_entries)}",
                )
            else:
                selected_structured_index = 0
            st.caption(structured_entries[selected_structured_index]["prompt"])
            render_result(structured_entries[selected_structured_index]["result"])
    else:
        st.button("结构化结果详情", disabled=True, use_container_width=True)

if submitted_structured:
    try:
        component_names = split_values(component_text)
        fractions = [float(part) for part in split_values(fractions_text)]
        structured_missing: list[str] = []
        if not component_names:
            structured_missing.append("组分名称")
        if not fractions:
            structured_missing.append("摩尔分数")
        if not selected_properties:
            structured_missing.append("目标物性")
        if structured_missing:
            st.session_state.input_warning = (
                "无法开始查询，还缺少：" + "、".join(structured_missing) + "。请补齐后重新提交。"
            )
            st.rerun()
        structured_result = query_properties(component_names, fractions, temperature, pressure, selected_properties)
        st.session_state.results.append({"prompt": "结构化查询：" + "、".join(component_names), "result": structured_result})
        if structured_result.get("status") == "needs_input":
            st.session_state.input_warning = _missing_parameter_warning(
                "、".join(component_names), str(structured_result.get("message", ""))
            )
        else:
            st.session_state.input_warning = ""
        st.session_state.query_notice = ""
        st.rerun()
    except ValueError as exc:
        st.session_state.input_warning = f"无法开始查询，摩尔分数格式有误：{exc}。请按组分顺序填写数字。"
        st.session_state.query_notice = st.session_state.input_warning
        st.rerun()
