"""Aspen Plus active-case selection and stream-property workflow."""

from __future__ import annotations

import streamlit as st

from aspen_connector import AspenConnectionError, read_bkp_file, read_streams, scan_cases
from demo_case import demo_payload
from excel_export import build_stream_workbook
from simulation import enrich_streams
from stream_import import StreamImportError, parse_stream_file


def _render_result(result: dict) -> None:
    st.caption(f"当前案例：{result['file_name']}")
    if result.get("demo"):
        st.info("这是课堂演示数据，用于展示完整链路；它不代表 Aspen 的实际计算结果。")
    st.subheader("Aspen/演示流股数据")
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

    st.caption("真实模式读取 Aspen Plus 的收敛结果；演示模式使用明确标注的教学流股数据。")
    action_col, upload_col = st.columns(2)
    with action_col:
        demo_clicked = st.button("加载课堂演示案例", key="load_demo_case")
    with upload_col:
        uploaded = st.file_uploader("上传已成功运行的 Aspen BKP", type=["bkp"], key="aspen_bkp_upload")
    imported_file = st.file_uploader("无需 Aspen：上传流股 CSV/JSON", type=["csv", "json"], key="stream_export_upload")
    if demo_clicked:
        payload = demo_payload()
        source_rows, property_rows = enrich_streams(payload["streams"])
        st.session_state.aspen_case_result = {
            "case_id": payload["case_id"], "file_name": "内置课堂演示案例（非 Aspen 文件）",
            "source_rows": source_rows, "property_rows": property_rows,
            "demo": True,
        }
    if uploaded is not None and st.button("分析上传的 BKP", key="analyze_uploaded_bkp"):
        try:
            payload = read_bkp_file(uploaded.name, uploaded.getvalue())
            source_rows, property_rows = enrich_streams(payload.get("streams", []))
            st.session_state.aspen_case_result = {
                "case_id": payload["case_id"], "file_name": uploaded.name,
                "source_rows": source_rows, "property_rows": property_rows,
                "demo": False,
            }
        except AspenConnectionError as exc:
            st.session_state.aspen_case_result = None
            st.warning(str(exc))
        except Exception as exc:
            st.session_state.aspen_case_result = None
            st.warning(f"BKP 分析失败：{type(exc).__name__}: {exc}")
    if imported_file is not None and st.button("分析通用流股文件", key="analyze_stream_file"):
        try:
            payload = parse_stream_file(imported_file.name, imported_file.getvalue())
            source_rows, property_rows = enrich_streams(payload["streams"])
            st.session_state.aspen_case_result = {"case_id": payload["case_id"], "file_name": imported_file.name, "source_rows": source_rows, "property_rows": property_rows, "demo": False, "imported": True}
        except StreamImportError as exc:
            st.session_state.aspen_case_result = None
            st.warning(str(exc))
        except Exception as exc:
            st.session_state.aspen_case_result = None
            st.warning(f"通用流股文件分析失败：{type(exc).__name__}: {exc}")
    immediate_result = st.session_state.get("aspen_case_result")
    if immediate_result and (immediate_result.get("demo") or immediate_result.get("imported") or uploaded is not None):
        _render_result(immediate_result)
        return
    st.divider()

    if st.button("扫描正在运行的 Aspen 案例", type="primary", key="scan_aspen_cases"):
        st.session_state.pop("aspen_case_result", None)
        try:
            inventory = scan_cases()
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
            payload = read_streams(selection["case_id"])
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
