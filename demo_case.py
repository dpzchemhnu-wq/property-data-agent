"""用于课堂演示的、明确标注为示例的流股数据。

示例只演示网页的数据链路，不冒充 Aspen 结果。真实项目应使用 Aspen COM
读取或上传到装有 Aspen Plus 的 Windows 主机后再分析 BKP。
"""

from __future__ import annotations

from typing import Any


def demo_streams() -> list[dict[str, Any]]:
    return [
        {
            "stream_id": "DEMO-FEED",
            "stream_name": "示例进料（教学数据）",
            "phase": "liquid",
            "vapor_fraction": 0.0,
            "components": ["water", "ethanol"],
            "mole_fractions": [0.6, 0.4],
            "flow_kmol_h": 100.0,
            "T_K": 298.15,
            "P_Pa": 101325.0,
            "read_status": "ok",
            "read_message": "",
        },
        {
            "stream_id": "DEMO-VAPOR",
            "stream_name": "示例气相产物（教学数据）",
            "phase": "gas",
            "vapor_fraction": 1.0,
            "components": ["ethanol"],
            "mole_fractions": [1.0],
            "flow_kmol_h": 40.0,
            "T_K": 360.0,
            "P_Pa": 101325.0,
            "read_status": "ok",
            "read_message": "",
        },
        {
            "stream_id": "DEMO-TWOPHASE",
            "stream_name": "示例气液两相（教学数据）",
            "phase": "two_phase",
            "vapor_fraction": 0.35,
            "components": ["water", "ethanol"],
            "mole_fractions": [0.6, 0.4],
            "flow_kmol_h": 60.0,
            "T_K": 350.0,
            "P_Pa": 101325.0,
            "read_status": "ok",
            "read_message": "",
        },
    ]


def demo_payload() -> dict[str, Any]:
    return {
        "status": "demo",
        "case_id": "demo-classroom-case",
        "file_path": "（内置教学数据，不是 Aspen BKP）",
        "streams": demo_streams(),
    }

