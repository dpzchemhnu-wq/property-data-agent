"""Aspen Plus active-case selection and stream-property workflow."""

from __future__ import annotations

import streamlit as st

from aspen_connector import AspenConnectionError, read_streams, scan_cases
from excel_export import build_stream_workbook
from simulation import enrich_streams


def _render_result(result: dict) -> None:
    st.caption(f"当前案例：{result['file_name']}")
    st.subheader("Aspen 流股数据")
    st.dataframe(result["source_rows"], width="stretch", hide_index=True)
    st.subheader("Agent 物性查询结果")
    st.dataframe(result["property_rows"], width="stretch", hide_index=True)
    st.info("气相、液相分别按对应状态计算；两相或状态不完整的流股保留原始数据并标注未计算。")
    workbook = build_stream_workbook(result["source_rows"], result["property_rows"])
    st.download_button(
        "下载流股与物性结果 Excel",
        data=workbook,
        file_name="Aspen流股与Agent物性结果.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        width="stretch",
    )


def render_simulation_page() -> None:
    st.markdown('<div class="hero-title">流程模拟调用</div>', unsafe_allow_html=True)

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
        for key in ("aspen_mcp_url", "aspen_mcp_token"):
            if key in st.query_params:
                del st.query_params[key]
        if "aspen_open" in st.query_params:
            del st.query_params["aspen_open"]

    session_mcp_url = str(st.session_state.get("aspen_session_mcp_url", "")).strip()
    session_mcp_token = str(st.session_state.get("aspen_session_mcp_token", "")).strip()
    if session_mcp_url and session_mcp_token:
        st.success("本机 Aspen 连接器已配对。此连接属于当前浏览器会话。")
    else:
        st.info("未检测到本机 Aspen 连接。请在安装 Aspen 的电脑运行一键连接器，再通过它打开此线上网页。")

    if st.button("扫描正在运行的 Aspen 案例", type="primary", key="scan_aspen_cases"):
        st.session_state.pop("aspen_case_result", None)
        try:
            inventory = scan_cases(mcp_url=session_mcp_url or None, mcp_token=session_mcp_token or None)
            st.session_state.aspen_case_inventory = inventory
        except AspenConnectionError as exc:
            st.session_state.aspen_case_inventory = None
            st.warning(str(exc))
        except Exception as exc:
            st.session_state.aspen_case_inventory = None
            st.warning(f"扫描 Aspen 案例失败：{type(exc).__name__}: {exc}")

    inventory = st.session_state.get("aspen_case_inventory")
    if not inventory:
        return
    cases = inventory.get("cases", [])
    if not cases:
        st.info("未发现当前用户会话中打开的 Aspen BKP 案例。请先在 Aspen Plus 中打开并运行案例，再重新扫描。")
        return

    st.dataframe(
        [
            {
                "案例文件": item.get("file_name", ""),
                "运行状态": item.get("run_status", "unknown"),
                "错误数": item.get("error_count"),
                "流股数": item.get("stream_count", 0),
                "可读取": bool(item.get("eligible")),
            }
            for item in cases
        ],
        width="stretch",
        hide_index=True,
    )
    eligible = [item for item in cases if item.get("eligible") is True and item.get("error_count") == 0]
    if not eligible:
        st.warning("未发现已停止运行、错误数为 0 且状态可验证的案例；为避免读取不完整结果，当前不允许选择。")
        return

    selection = st.selectbox(
        "选择要查询的 BKP 案例",
        eligible,
        format_func=lambda item: item.get("file_name", item.get("case_id", "Aspen 案例")),
        key=f"aspen_case_select_{len(cases)}",
    )
    if st.button("读取流股并查询物性", key="read_aspen_streams"):
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
