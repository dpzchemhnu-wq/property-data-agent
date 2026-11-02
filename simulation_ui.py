"""Aspen Plus active-case selection and stream-property workflow."""

from __future__ import annotations

import html
import os
from pathlib import Path

import streamlit as st

from aspen_connector import AspenConnectionError, read_streams, scan_cases
from excel_export import build_stream_workbook
from simulation import enrich_streams
from upstream_aspen_agent import AGENT_NAME, REPOSITORY


def _render_result(result: dict) -> None:
    st.markdown(
        f'<div class="result-banner"><div><span class="eyebrow">已锁定案例</span><strong>{html.escape(result["file_name"])}</strong></div>'
        '<span class="tag ok">完整结果</span></div>',
        unsafe_allow_html=True,
    )
    st.markdown('<div class="section-heading"><b>Aspen 流股数据</b><span>完整结果 · 单位已换算为 SI</span></div>', unsafe_allow_html=True)
    st.dataframe(result["source_rows"], width="stretch", hide_index=True)
    st.markdown('<div class="section-heading"><b>物性处理结果</b><span>上游 Aspen Co-pilot 只读接入</span></div>', unsafe_allow_html=True)
    st.dataframe(result["property_rows"], width="stretch", hide_index=True)
    st.info("气相与液相分别按 Aspen 输出的状态计算；气液两相保留 Aspen 的 MIXED 总量、组成和汽相分率，并跳过单相物性模型。")
    workbook = build_stream_workbook(result["source_rows"], result["property_rows"])
    bkp_name = result.get("file_name", "Aspen案例").rsplit(".", 1)[0]
    st.download_button(
        "下载流股与物性结果 Excel",
        data=workbook,
        file_name=f"{bkp_name}_流股物性结果.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        width="stretch",
    )


def _case_tag(item: dict) -> tuple[str, str]:
    if item.get("eligible"):
        return "已停止", "ok"
    if item.get("run_status") == "running":
        return "正在计算", "warn"
    if item.get("error_count") not in (None, 0):
        return f"{item.get('error_count')} 个错误", "bad"
    return "暂不可读", "warn"


def _render_case_inventory(cases: list[dict]) -> None:
    available_count = sum(bool(item.get("eligible")) for item in cases)
    complete_count = sum(int(item.get("complete_stream_count", 0) or 0) for item in cases)
    total_count = sum(int(item.get("stream_count", 0) or 0) for item in cases)
    rows: list[str] = []
    for item in cases:
        status, kind = _case_tag(item)
        validation = "通过" if item.get("eligible") else "拒绝读取"
        validation_kind = "ok" if item.get("eligible") else "bad"
        selected = " selected" if item.get("eligible") and not any(" selected" in row for row in rows) else ""
        rows.append(
            f'<div class="case-row{selected}">'
            f'<div><b>{html.escape(str(item.get("file_name", "")))}</b>'
            f'<span class="case-path">{html.escape(str(item.get("file_path", "")))}</span></div>'
            f'<div><span class="tag {kind}">{html.escape(status)}</span></div>'
            f'<div>{html.escape(str(item.get("error_count", "—")))}</div>'
            f'<div>{int(item.get("complete_stream_count", 0) or 0)} / {int(item.get("stream_count", 0) or 0)}</div>'
            f'<div><span class="tag {validation_kind}">{validation}</span></div>'
            f'<div><input type="radio" disabled{" checked" if selected else ""}></div></div>'
        )
    st.markdown(
        f'<div class="case-shell"><div class="shell-heading"><div><span class="eyebrow">CASE INVENTORY</span><h2>正在运行的 Aspen 案例</h2></div><span class="inventory-count">{available_count} 个可读取</span></div>'
            f'<div class="sub">{html.escape(REPOSITORY)}　·　按完整路径区分同名 BKP　·　扫描到 {len(cases)} 个实例　·　完整流股 {complete_count} / {total_count}</div>'
        f'<div class="case-list"><div class="case-row header"><div>案例文件</div><div>计算状态</div><div>错误数</div><div>流股完整度</div><div>状态校验</div><div>选择</div></div>{"".join(rows)}</div></div>',
        unsafe_allow_html=True,
    )


def _demo_inventory() -> dict:
    """Local-only visual QA data; enabled only with ASPN_DEMO_MODE=1."""
    return {
        "status": "connected",
        "agent": {"name": AGENT_NAME, "repository": REPOSITORY},
        "cases": [
            {"case_id": "demo0000000000000001", "file_name": "methanol_loop.bkp", "file_path": r"D:\Process\Methanol\methanol_loop.bkp", "run_status": "converged", "error_count": 0, "stream_count": 12, "complete_stream_count": 12, "eligible": True},
            {"case_id": "demo0000000000000002", "file_name": "methanol_loop.bkp", "file_path": r"D:\Archive\methanol_loop.bkp", "run_status": "running", "error_count": None, "stream_count": 0, "complete_stream_count": 0, "eligible": False},
            {"case_id": "demo0000000000000003", "file_name": "flash_test.bkp", "file_path": r"D:\Process\Flash\flash_test.bkp", "run_status": "converged", "error_count": 2, "stream_count": 6, "complete_stream_count": 6, "eligible": False},
        ],
    }


def _demo_result() -> dict:
    source_rows = [
        {"stream_id": "FEED", "stream_name": "FEED", "phase": "liquid", "flow_kmol_h": 100.0, "T_K": 298.15, "P_Pa": 2500000.0, "components": "METHANOL、WATER", "mole_fractions": "0.8, 0.2"},
        {"stream_id": "VAPOR-OUT", "stream_name": "VAPOR-OUT", "phase": "gas", "flow_kmol_h": 86.32, "T_K": 523.15, "P_Pa": 2400000.0, "components": "METHANOL、WATER", "mole_fractions": "0.75, 0.25"},
        {"stream_id": "FLASH-OUT", "stream_name": "FLASH-OUT", "phase": "two_phase", "flow_kmol_h": 13.68, "T_K": 342.5, "P_Pa": 101300.0, "components": "METHANOL、WATER", "mole_fractions": "0.7, 0.3"},
    ]
    property_rows = [
        {"stream_id": "FEED", "stream_name": "FEED", "phase": "liquid", "phase_state": "single_liquid_assumed", "status": "ok", "density": 791.4, "density_unit": "kg/m3", "viscosity": 0.00062, "viscosity_unit": "Pa·s", "enthalpy": -184.2, "enthalpy_unit": "kJ/kg", "thermal_conductivity": 0.176, "thermal_conductivity_unit": "W/(m·K)"},
        {"stream_id": "VAPOR-OUT", "stream_name": "VAPOR-OUT", "phase": "gas", "phase_state": "single_gas_detected", "status": "gas_partial", "density": 1.2, "density_unit": "kg/m3", "viscosity": 0.00002, "viscosity_unit": "Pa·s", "message": "仅返回可计算的气相属性。"},
        {"stream_id": "FLASH-OUT", "stream_name": "FLASH-OUT", "phase": "two_phase", "status": "two_phase_skipped", "message": "气液两相保留 Aspen MIXED 数据，跳过单相物性模型。"},
    ]
    return {"case_id": "demo0000000000000001", "file_name": "methanol_loop.bkp", "source_rows": source_rows, "property_rows": property_rows}


def render_simulation_page() -> None:
    demo_mode = os.getenv("ASPN_DEMO_MODE", "").strip() == "1" and str(st.query_params.get("aspen_demo", "")) == "1"
    st.markdown(
        '<div class="page-top"><div><div class="eyebrow">WORKSPACE / ASPEN PLUS</div>'
        '<h1>流程模拟调用</h1><p>从本机 Aspen Plus 读取已完成计算的流股结果</p></div>'
        f'<div class="agent-mark"><span class="agent-mark-dot"></span><span>{html.escape(AGENT_NAME)}</span><b>只读</b></div></div>',
        unsafe_allow_html=True,
    )

    # 连接器只绑定当前 Streamlit 用户会话。链接中的一次性随机令牌
    # 会立刻转存到 session_state 并从地址栏移除，避免多个用户共用 Render 环境变量。
    query_url = str(st.query_params.get("aspen_mcp_url", "")).strip()
    query_token = str(st.query_params.get("aspen_mcp_token", "")).strip()
    if query_url and query_token:
        if not query_url.startswith("https://") or not query_url.split("/", 3)[2].endswith(".trycloudflare.com"):
            st.error("本机连接地址不符合安全规则，请使用最新的一键启动 BAT 重新连接。")
            return
        st.session_state.aspen_session_mcp_url = query_url.rstrip("/")
        st.session_state.aspen_session_mcp_token = query_token
        st.session_state.pop("aspen_case_inventory", None)
        st.session_state.pop("aspen_case_result", None)
        st.session_state.aspen_auto_scan_pending = True
        for key in ("aspen_mcp_url", "aspen_mcp_token"):
            if key in st.query_params:
                del st.query_params[key]
        if "aspen_open" in st.query_params:
            del st.query_params["aspen_open"]

    session_mcp_url = str(st.session_state.get("aspen_session_mcp_url", "")).strip()
    session_mcp_token = str(st.session_state.get("aspen_session_mcp_token", "")).strip()
    if demo_mode:
        st.markdown(f'<div class="agent-card"><div class="agent-icon">◌</div><div><b>本机 Aspen 连接器</b><small>本地演示模式　·　模拟数据　·　{html.escape(AGENT_NAME)}</small></div><span class="demo-chip">模拟预览</span></div>', unsafe_allow_html=True)
    elif session_mcp_url and session_mcp_token:
        st.markdown(
            f'<div class="agent-card"><div class="agent-icon">✓</div><div><b>本机 Aspen 连接器</b>'
            f'<small>Windows 本地桥接已配对　·　只读模式　·　{AGENT_NAME}</small></div>'
            '<span class="connected-chip">● 已连接</span></div>', unsafe_allow_html=True,
        )
    else:
        st.markdown('<div class="connect-panel"><div class="connect-panel-icon">↗</div><div><strong>连接本机 Aspen Plus</strong></div></div>', unsafe_allow_html=True)
        connector_path = Path(__file__).with_name("Start Aspen Property Agent - One Click.bat")
        if connector_path.is_file():
            st.download_button(
                "下载 Windows 连接器",
                data=connector_path.read_bytes(),
                file_name=connector_path.name,
                mime="application/octet-stream",
                type="primary",
                key="download_aspen_connector",
                width="stretch",
            )
        with st.expander("为什么还需要打开下载的文件？", expanded=False):
            st.write("浏览器不允许网页直接执行电脑上的 BAT 脚本。下载后请在浏览器下载栏点击该文件，并确认 Windows 的安全提示。之后连接器会自动打开已配对的线上页面；本窗口保持打开即可。")

    diag_col, scan_col = st.columns([1, 1.2])
    with diag_col:
        st.button("连接诊断", key="diag_aspen_cases", disabled=not (session_mcp_url and session_mcp_token))
    with scan_col:
        scan_clicked = st.button(
            "扫描本机案例",
            type="primary",
            key="scan_aspen_cases",
        )
    auto_scan = bool(st.session_state.pop("aspen_auto_scan_pending", False))
    if scan_clicked or auto_scan:
        st.session_state.pop("aspen_case_result", None)
        if not (session_mcp_url and session_mcp_token):
            st.session_state.aspen_case_inventory = None
            if scan_clicked:
                st.info("本页面还没有配对本机连接器。请先运行上方下载的 BAT；配对完成后会自动扫描。")
        else:
            try:
                inventory = scan_cases(mcp_url=session_mcp_url, mcp_token=session_mcp_token)
                st.session_state.aspen_case_inventory = inventory
            except AspenConnectionError as exc:
                st.session_state.aspen_case_inventory = None
                st.warning("暂时无法连接本机 Aspen。请确认连接器窗口仍开启，并在详情中查看原因。")
                with st.expander("连接诊断详情", expanded=False):
                    st.code(str(exc))
            except Exception as exc:
                st.session_state.aspen_case_inventory = None
                st.warning("扫描未完成。请确认连接器窗口仍开启，并在详情中查看原因。")
                with st.expander("连接诊断详情", expanded=False):
                    st.code(f"{type(exc).__name__}: {exc}")

    if demo_mode:
        st.session_state.aspen_case_inventory = _demo_inventory()
        st.session_state.aspen_case_result = _demo_result()
    inventory = st.session_state.get("aspen_case_inventory")
    if not inventory:
        return
    cases = inventory.get("cases", [])
    if not cases:
        if inventory.get("status") == "aspen_not_detected":
            st.warning("本机连接器已连接，但未检测到 Aspen Plus。当前电脑未安装 Aspen，或 Aspen 的 COM 接口未注册；请在安装并授权 Aspen Plus 的 Windows 电脑上运行此连接器。")
            return
        st.info("未发现当前用户会话中打开的 Aspen BKP 案例。请先在 Aspen Plus 中打开并运行案例，再重新扫描。")
        return

    m1, m2, m3 = st.columns(3)
    with m1:
        st.markdown(f'<div class="metric-card"><small>检测到的案例</small><strong>{len(cases)}</strong><span>▤</span></div>', unsafe_allow_html=True)
    with m2:
        st.markdown(f'<div class="metric-card"><small>可读取案例</small><strong class="green">{sum(bool(item.get("eligible")) for item in cases)}</strong><span>✓</span></div>', unsafe_allow_html=True)
    with m3:
        st.markdown(f'<div class="metric-card"><small>完整流股数据</small><strong>{sum(int(item.get("complete_stream_count", 0) or 0) for item in cases)} / {sum(int(item.get("stream_count", 0) or 0) for item in cases)}</strong><span>⇄</span></div>', unsafe_allow_html=True)
    _render_case_inventory(cases)
    eligible = [
        item for item in cases
        if item.get("eligible") is True
        and item.get("error_count") == 0
        and item.get("stream_count", 0) > 0
        and item.get("complete_stream_count") == item.get("stream_count")
    ]
    if not eligible:
        st.warning("未发现已停止运行、错误数为 0 且所有流股结果完整（温度、压力、摩尔流量、组成和相态均可读取）的案例；当前不允许选择。")
        return

    st.markdown('<div class="selection-heading"><span class="eyebrow">SELECT SOURCE</span><h3>选择要读取的 BKP</h3><p>点击右侧小圆点选择案例，然后点"读取流股数据"。</p></div>', unsafe_allow_html=True)
    options = {item["case_id"]: f"{item.get('file_name', 'Aspen 案例')} — {item.get('file_path', item.get('case_id', ''))}" for item in eligible}
    selected_id = st.radio(
        "选择要查询的 BKP 案例",
        options=list(options.keys()),
        format_func=lambda cid: options[cid],
        key=f"aspen_case_select_{len(cases)}",
        label_visibility="collapsed",
    )
    selection = next(item for item in eligible if item["case_id"] == selected_id)
    if st.button("读取流股数据", key="read_aspen_streams", type="primary"):
        try:
            payload = read_streams(
                selection["case_id"],
                mcp_url=session_mcp_url or None,
                mcp_token=session_mcp_token or None,
            )
            source_rows, property_rows = enrich_streams(payload.get("streams", []))
            st.session_state.aspen_case_result = {
                "case_id": selection["case_id"],
                "file_name": selection.get("file_name", ""),
                "source_rows": source_rows,
                "property_rows": property_rows,
            }
        except AspenConnectionError as exc:
            st.session_state.aspen_case_result = None
            st.error(str(exc))
        except Exception as exc:
            st.session_state.aspen_case_result = None
            st.error(f"读取 Aspen 流股失败：{type(exc).__name__}: {exc}")

    result = st.session_state.get("aspen_case_result")
    if not result or result.get("case_id") != selection.get("case_id"):
        return
    _render_result(result)
