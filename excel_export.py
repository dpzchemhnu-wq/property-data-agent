"""Use only the Python standard library to create a two-sheet XLSX workbook."""

from __future__ import annotations

import io
import math
import zipfile
from typing import Any
from xml.etree import ElementTree as ET


MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
ET.register_namespace("", MAIN)
ET.register_namespace("r", REL)

SOURCE_HEADERS = ["stream_id", "stream_name", "phase", "flow_kmol_h", "T_K", "P_Pa", "components", "mole_fractions"]
PROPERTY_HEADERS = [
    "stream_id", "stream_name", "phase", "phase_state", "status", "density", "density_unit", "density_source_ids",
    "enthalpy", "enthalpy_unit", "enthalpy_source_ids", "viscosity", "viscosity_unit", "viscosity_source_ids",
    "surface_tension", "surface_tension_unit", "surface_tension_source_ids", "thermal_conductivity",
    "thermal_conductivity_unit", "thermal_conductivity_source_ids", "message", "warnings",
]


def _cell_reference(row: int, col: int) -> str:
    letters = ""
    while col:
        col, remainder = divmod(col - 1, 26)
        letters = chr(65 + remainder) + letters
    return f"{letters}{row}"


def _worksheet(headers: list[str], rows: list[dict[str, Any]]) -> bytes:
    root = ET.Element(f"{{{MAIN}}}worksheet")
    data = ET.SubElement(root, f"{{{MAIN}}}sheetData")
    all_rows = [headers, *[[row.get(key) for key in headers] for row in rows]]
    for row_index, values in enumerate(all_rows, start=1):
        row_el = ET.SubElement(data, f"{{{MAIN}}}row", {"r": str(row_index)})
        for col_index, value in enumerate(values, start=1):
            if value is None:
                continue
            attrs = {"r": _cell_reference(row_index, col_index)}
            if row_index == 1:
                attrs["s"] = "1"
            cell = ET.SubElement(row_el, f"{{{MAIN}}}c", attrs)
            if isinstance(value, bool):
                cell.set("t", "b")
                ET.SubElement(cell, f"{{{MAIN}}}v").text = "1" if value else "0"
            elif isinstance(value, (int, float)) and math.isfinite(float(value)):
                ET.SubElement(cell, f"{{{MAIN}}}v").text = str(value)
            else:
                cell.set("t", "inlineStr")
                inline = ET.SubElement(cell, f"{{{MAIN}}}is")
                ET.SubElement(inline, f"{{{MAIN}}}t", {"{http://www.w3.org/XML/1998/namespace}space": "preserve"}).text = str(value)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def build_stream_workbook(source_rows: list[dict[str, Any]], property_rows: list[dict[str, Any]]) -> bytes:
    """返回工作簿字节；不依赖 pandas/openpyxl/xlsxwriter。"""
    output = io.BytesIO()
    workbook = ET.Element(f"{{{MAIN}}}workbook")
    sheets = ET.SubElement(workbook, f"{{{MAIN}}}sheets")
    ET.SubElement(sheets, f"{{{MAIN}}}sheet", {"name": "Aspen流股数据", "sheetId": "1", f"{{{REL}}}id": "rId1"})
    ET.SubElement(sheets, f"{{{MAIN}}}sheet", {"name": "Agent物性数据", "sheetId": "2", f"{{{REL}}}id": "rId2"})
    workbook_rels = ET.Element(f"{{{PKG_REL}}}Relationships")
    ET.SubElement(workbook_rels, f"{{{PKG_REL}}}Relationship", {"Id": "rId1", "Type": f"{REL}/worksheet", "Target": "worksheets/sheet1.xml"})
    ET.SubElement(workbook_rels, f"{{{PKG_REL}}}Relationship", {"Id": "rId2", "Type": f"{REL}/worksheet", "Target": "worksheets/sheet2.xml"})
    ET.SubElement(workbook_rels, f"{{{PKG_REL}}}Relationship", {"Id": "rId3", "Type": f"{REL}/styles", "Target": "styles.xml"})
    content_types = ET.Element("{http://schemas.openxmlformats.org/package/2006/content-types}Types")
    ET.SubElement(content_types, "{http://schemas.openxmlformats.org/package/2006/content-types}Default", {"Extension": "rels", "ContentType": "application/vnd.openxmlformats-package.relationships+xml"})
    ET.SubElement(content_types, "{http://schemas.openxmlformats.org/package/2006/content-types}Default", {"Extension": "xml", "ContentType": "application/xml"})
    for part, content_type in (
        ("xl/workbook.xml", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"),
        ("xl/worksheets/sheet1.xml", "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"),
        ("xl/worksheets/sheet2.xml", "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"),
        ("xl/styles.xml", "application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"),
    ):
        ET.SubElement(content_types, "{http://schemas.openxmlformats.org/package/2006/content-types}Override", {"PartName": f"/{part}", "ContentType": content_type})
    root_rels = ET.Element(f"{{{PKG_REL}}}Relationships")
    ET.SubElement(root_rels, f"{{{PKG_REL}}}Relationship", {"Id": "rId1", "Type": f"{REL}/officeDocument", "Target": "xl/workbook.xml"})
    styles = ET.fromstring(b'''<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills><borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>''')

    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", ET.tostring(content_types, encoding="utf-8", xml_declaration=True))
        archive.writestr("_rels/.rels", ET.tostring(root_rels, encoding="utf-8", xml_declaration=True))
        archive.writestr("xl/workbook.xml", ET.tostring(workbook, encoding="utf-8", xml_declaration=True))
        archive.writestr("xl/_rels/workbook.xml.rels", ET.tostring(workbook_rels, encoding="utf-8", xml_declaration=True))
        archive.writestr("xl/styles.xml", ET.tostring(styles, encoding="utf-8", xml_declaration=True))
        archive.writestr("xl/worksheets/sheet1.xml", _worksheet(SOURCE_HEADERS, source_rows))
        archive.writestr("xl/worksheets/sheet2.xml", _worksheet(PROPERTY_HEADERS, property_rows))
    return output.getvalue()
