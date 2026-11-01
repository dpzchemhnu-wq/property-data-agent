"""物性后端的无网络边界测试，可用 python -m unittest 运行。"""

import unittest
import os
from unittest.mock import patch
import json
import io
import zipfile
import threading
from types import SimpleNamespace
from xml.etree import ElementTree as ET

from aspen_connector import (
    AspenConnectionError,
    _case_id,
    _read_selected_on_com_thread,
    _read_streams,
    _scan_on_com_thread,
    _to_kelvin,
    _to_kmol_h,
    _to_pascal,
)
from aspen_mcp_server import dispatch
from local_launcher import _python312_command
from llm_client import LLMServiceError, run_tool_loop
from local_parser import parse_explicit_query, split_values
from excel_export import build_stream_workbook
from simulation import (
    INPUT_JSON_SCHEMA,
    OUTPUT_JSON_SCHEMA,
    SimulationInputError,
    build_simulation_request,
    enrich_streams,
)
from tools import (
    AMBIGUOUS_COMPONENT_CAS,
    Component,
    _model_coverage,
    _parse_nist_table,
    _pubchem_name_lookup,
    _resolve_components,
    _thermoml_block_matches_state,
    _thermoml_lookup,
    _thermoml_record_component_map,
    query_properties,
)


class QueryValidationTests(unittest.TestCase):
    def test_missing_fractions_returns_needs_input(self):
        result = query_properties(["water"], None, 298.15, 101325, ["density"])
        self.assertEqual(result["status"], "needs_input")

    @patch("tools._thermoml_lookup", return_value={"covered": False, "data": {}, "records": []})
    def test_positive_fraction_sum_is_normalized(self, _thermoml_lookup):
        result = query_properties(["water", "ethanol"], [0.6, 0.3], 298.15, 101325, ["density"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["input_mole_fractions"], [0.6, 0.3])
        self.assertAlmostEqual(sum(result["mole_fractions"]), 1.0)
        self.assertAlmostEqual(result["mole_fractions"][0], 2 / 3)
        self.assertAlmostEqual(result["mole_fractions"][1], 1 / 3)
        self.assertTrue(any("归一化" in warning for warning in result["warnings"]))

    def test_negative_fraction_is_rejected(self):
        result = query_properties(["water", "ethanol"], [1.2, -0.2], 298.15, 101325, ["density"])
        self.assertEqual(result["status"], "needs_input")

    def test_all_zero_fractions_are_rejected(self):
        result = query_properties(["water", "ethanol"], [0.0, 0.0], 298.15, 101325, ["density"])
        self.assertEqual(result["status"], "needs_input")

    def test_temperature_domain_is_enforced_before_solver(self):
        result = query_properties(["water"], [1.0], 451.0, 101325, ["density"])
        self.assertEqual(result["status"], "out_of_domain")

    def test_vapor_state_returns_clear_liquid_property_message(self):
        result = query_properties(["water"], [1.0], 400.0, 101325.0, ["density"])
        self.assertEqual(result["status"], "out_of_domain")
        self.assertIn("气态", result["message"])
        self.assertEqual(result["phase_state"], "single_gas_detected")

    def test_vapor_state_keeps_safe_gas_properties_and_excludes_liquid_only_properties(self):
        result = query_properties(["water"], [1.0], 400.0, 101325.0, ["density", "enthalpy"])
        self.assertEqual(result["status"], "out_of_domain")
        self.assertIn("enthalpy", result["partial_properties"])
        self.assertIn("density", result["partial_properties"])
        self.assertEqual(result["partial_properties"]["density"]["phase"], "gas")
        self.assertNotIn("surface_tension", result["partial_properties"])
    @patch.dict("os.environ", {"COMPONENT_ALIAS_FILE": "X:/missing/component_aliases.json"})
    def test_builtin_chinese_aliases_work_without_alias_json_file(self):
        from tools import _resolve_components
        resolved = _resolve_components(["水", "乙醇"])
        self.assertEqual([component.cas for component in resolved], ["7732-18-5", "64-17-5"])

    def test_full_editable_alias_catalog_resolves_to_its_declared_cas(self):
        from pathlib import Path
        alias_file = Path(__file__).with_name("component_aliases.json")
        payload = json.loads(alias_file.read_text(encoding="utf-8"))
        alias_map = payload["aliases"]
        checked = 0
        for name, expected_cas in alias_map.items():
            if expected_cas in AMBIGUOUS_COMPONENT_CAS:
                continue
            with self.subTest(name=name):
                resolved = _resolve_components([name])
                self.assertEqual(resolved[0].cas, expected_cas)
                checked += 1
        self.assertGreaterEqual(checked, 200)

    def test_mixed_xylene_name_requests_isomer_instead_of_silent_wrong_mapping(self):
        result = query_properties(["二甲苯"], [1.0], 298.15, 101325.0, ["density"])
        self.assertEqual(result["status"], "needs_input")
        self.assertIn("请指定", result["message"])

    def test_methanol_chinese_name_resolves_through_catalog(self):
        resolved = _resolve_components(["甲醇"])
        self.assertEqual(resolved[0].cas, "67-56-1")

    def test_common_chinese_ethylbenzene_name_resolves(self):
        resolved = _resolve_components(["乙苯"])
        self.assertEqual(resolved[0].cas, "100-41-4")

    def test_ethylbenzene_alias_preserves_user_name(self):
        result = query_properties(["乙苯"], [1.0], 298.15, 101325.0, ["density"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["components"][0]["name"], "乙苯")

    @patch("tools.search_chemical")
    @patch("tools.requests.get")
    def test_pubchem_fallback_checks_cas_before_returning(self, get, search):
        class Response:
            status_code = 200

            def __init__(self, body):
                self.body = body

            def raise_for_status(self):
                return None

            def json(self):
                return self.body

        get.side_effect = [
            Response({"IdentifierList": {"CID": [887]}}),
            Response({"InformationList": {"Information": [{"Synonym": ["methyl alcohol", "67-56-1"]}]}}),
        ]
        search.return_value = type("Record", (), {"CAS": 67561, "common_name": "methanol"})()
        _pubchem_name_lookup.cache_clear()
        self.assertEqual(_pubchem_name_lookup("rare methanol synonym"), ("67-56-1", "methanol"))
        self.assertEqual(get.call_count, 2)

    def test_bulk_common_chemical_identifiers_across_categories(self):
        samples = {
            "solvents": ["甲醇", "methanol", "乙醇", "ethanol", "丙酮", "acetone", "乙腈", "acetonitrile", "四氢呋喃", "tetrahydrofuran", "二甲基亚砜", "DMSO", "二甲基甲酰胺", "DMF", "N-甲基吡咯烷酮", "NMP"],
            "alcohols_and_glycols": ["异丙醇", "isopropanol", "正丁醇", "1-butanol", "乙二醇", "ethylene glycol", "丙二醇", "propylene glycol", "甘油", "glycerol"],
            "hydrocarbons": ["苯", "benzene", "甲苯", "toluene", "邻二甲苯", "o-xylene", "间二甲苯", "m-xylene", "对二甲苯", "p-xylene", "环己烷", "cyclohexane", "正己烷", "n-hexane", "正庚烷", "n-heptane", "苯乙烯", "styrene"],
            "acids_and_bases": ["乙酸", "acetic acid", "甲酸", "formic acid", "硫酸", "sulfuric acid", "硝酸", "nitric acid", "氢氧化钠", "sodium hydroxide", "氨", "ammonia"],
            "gases": ["甲烷", "methane", "乙烷", "ethane", "乙烯", "ethylene", "丙烷", "propane", "二氧化碳", "carbon dioxide", "氮气", "nitrogen", "氢气", "hydrogen"],
            "esters_and_ethers": ["乙酸乙酯", "ethyl acetate", "乙酸甲酯", "methyl acetate", "乙酸丁酯", "butyl acetate", "乙醚", "diethyl ether", "甲基叔丁基醚", "MTBE"],
        }
        checked = 0
        for category, names in samples.items():
            with self.subTest(category=category):
                for name in names:
                    with self.subTest(name=name):
                        resolved = _resolve_components([name])
                        self.assertTrue(resolved[0].cas)
                        checked += 1
        self.assertGreaterEqual(checked, 80)


class ParameterCoverageTests(unittest.TestCase):
    def test_nrtl_selected_only_with_full_pair_and_source(self):
        components = [Component("A", "64-17-5", "A"), Component("B", "67-56-1", "B")]
        row = {
            "model": "NRTL", "CAS1": "64-17-5", "CAS2": "67-56-1",
            "tau_a_ij": 0.1, "tau_a_ji": 0.2, "tau_b_ij": 10.0,
            "tau_b_ji": 20.0, "alpha_ij": 0.3, "source_id": "paper:test",
        }
        model, _, _ = _model_coverage(components, {"pairs": [row]})
        self.assertEqual(model, "NRTL")

    def test_missing_pair_falls_back_to_unifac(self):
        components = [Component("A", "64-17-5", "A"), Component("B", "67-56-1", "B")]
        model, _, _ = _model_coverage(components, {"pairs": []})
        self.assertEqual(model, "UNIFAC")

    def test_placeholder_citation_does_not_count_as_literature_parameter(self):
        components = [Component("A", "64-17-5", "A"), Component("B", "67-56-1", "B")]
        row = {
            "model": "NRTL", "CAS1": "64-17-5", "CAS2": "67-56-1",
            "tau_a_ij": 0.0, "tau_a_ji": 0.0, "tau_b_ij": 0.0,
            "tau_b_ji": 0.0, "alpha_ij": 0.3,
            "source_id": "literature:replace-with-verified-citation",
        }
        model, _, _ = _model_coverage(components, {"pairs": [row]})
        self.assertEqual(model, "UNIFAC")


class NistParsingTests(unittest.TestCase):
    def test_parses_tabular_state_point_without_guessing(self):
        response = """NIST output\nTemperature (K)\tPressure (MPa)\tDensity (kg/m3)\tEnthalpy (kJ/mol)\n298.15\t0.101325\t997.0\t-285.8\n"""
        parsed = _parse_nist_table(response)
        self.assertEqual(parsed["density"], 997.0)
        self.assertEqual(parsed["enthalpy"], -285.8)

    @patch("tools.requests.Session")
    @patch("tools.requests.get", side_effect=__import__("requests").exceptions.SSLError("TLS handshake failed"))
    def test_nist_connection_failure_is_not_reported_as_no_coverage(self, _get, session_factory):
        from tools import _nist_lookup
        session_factory.return_value.get.side_effect = __import__("requests").exceptions.SSLError("direct TLS handshake failed")
        component = Component("ethyl acetate", "141-78-6", "ethyl acetate")
        result = _nist_lookup(component, 298.15, 101325.0)
        self.assertFalse(result["found"])
        self.assertTrue(result["unavailable"])
        self.assertIn("无法判断数据库是否覆盖", result["message"])

    @patch("tools.requests.Session")
    @patch("tools.requests.get", side_effect=__import__("requests").exceptions.SSLError("proxy TLS handshake failed"))
    def test_nist_retries_directly_after_proxy_tls_failure(self, _get, session_factory):
        from tools import _nist_fluid_request
        session_factory.return_value.get.return_value.status_code = 200
        session_factory.return_value.get.return_value.text = (
            "Temperature (K)\tPressure (MPa)\tDensity (kg/m3)\n298.15\t0.101325\t997.0\n"
        )
        session_factory.return_value.get.return_value.raise_for_status.return_value = None
        component = Component("water", "7732-18-5", "water")
        data = _nist_fluid_request(component, "IsoTherm", 298.15, 101325.0, 1.0)
        self.assertEqual(data["density"], 997.0)
        self.assertFalse(session_factory.return_value.trust_env)

    @patch("tools.requests.get")
    def test_nist_fluid_cgi_syntax_error_means_not_covered_not_connection_failure(self, get):
        from tools import _nist_lookup
        get.return_value.status_code = 400
        get.return_value.text = "Query Error: There is a syntax error in the query which was sent to the database system."
        component = Component("ethyl acetate", "141-78-6", "ethyl acetate")
        result = _nist_lookup(component, 298.15, 101325.0)
        self.assertFalse(result["found"])
        self.assertNotIn("unavailable", result)
        self.assertIn("未覆盖", result["message"])

    def test_matches_requested_temperature_and_pressure(self):
        response = """Temperature (K)\tPressure (MPa)\tDensity (kg/m3)\n273.15\t0.101325\t999.8\n298.15\t0.101325\t997.0\n"""
        parsed = _parse_nist_table(response, expected_T_K=298.15, expected_P_MPa=0.101325)
        self.assertEqual(parsed["density"], 997.0)

    @patch("tools.requests.get")
    def test_nist_fluid_cgi_receives_digit_only_cas_identifier(self, get):
        from tools import _nist_fluid_request
        get.return_value.text = "Temperature (K)\tPressure (MPa)\tDensity (kg/m3)\n298.15\t0.101325\t900.0\n"
        get.return_value.raise_for_status.return_value = None
        component = Component("ethyl acetate", "141-78-6", "ethyl acetate")
        _nist_fluid_request(component, "IsoTherm", 298.15, 101325.0, 1.0)
        self.assertEqual(get.call_args.kwargs["params"]["ID"], "C141786")


class InputParsingTests(unittest.TestCase):
    def test_fullwidth_and_chinese_separators_are_supported(self):
        self.assertEqual(split_values("水，乙醇、甲苯；丙酮"), ["水", "乙醇", "甲苯", "丙酮"])

    def test_screenshot_style_request_extracts_order_ratio_and_conditions(self):
        parsed, message = parse_explicit_query(
            "查一下300K、常压下，乙醇和水摩尔比0.4:0.6混合物的全套流程设计所需物性数据"
        )
        self.assertIsNotNone(parsed, message)
        self.assertEqual(parsed["components"], ["乙醇", "水"])
        self.assertEqual(parsed["mole_fractions"], [0.4, 0.6])
        self.assertEqual(parsed["T_K"], 300.0)
        self.assertEqual(parsed["P_Pa"], 101325.0)
        self.assertEqual(len(parsed["target_properties"]), 6)

    def test_local_parser_asks_for_missing_conditions(self):
        parsed, message = parse_explicit_query("查询水和乙醇的密度")
        self.assertIsNone(parsed)
        self.assertIn("温度", message)
        self.assertIn("压力", message)

    def test_local_parser_extracts_component_attached_fractions(self):
        parsed, message = parse_explicit_query(
            "查水0.6、乙醇0.4在300 K常压下的全套物性"
        )
        self.assertIsNotNone(parsed, message)
        self.assertEqual(parsed["components"], ["水", "乙醇"])
        self.assertEqual(parsed["mole_fractions"], [0.6, 0.4])

    def test_local_parser_resolves_unlisted_english_name_without_llm(self):
        parsed, message = parse_explicit_query("Find density of methanol at 298 K and 1 atm")
        self.assertIsNotNone(parsed, message)
        self.assertEqual(parsed["components"], ["methanol"])
        self.assertEqual(parsed["mole_fractions"], [1.0])

    def test_local_parser_recognizes_added_chinese_methanol_alias(self):
        parsed, message = parse_explicit_query("查300 K常压下甲醇的全套物性")
        self.assertIsNotNone(parsed, message)
        self.assertEqual(parsed["components"], ["甲醇"])

    def test_local_parser_converts_celsius_to_kelvin(self):
        parsed, message = parse_explicit_query("查 25℃、常压下水的密度")
        self.assertIsNotNone(parsed, message)
        self.assertAlmostEqual(parsed["T_K"], 298.15)

    def test_local_parser_converts_fahrenheit_to_kelvin(self):
        parsed, message = parse_explicit_query("查 77°F、常压下水的密度")
        self.assertIsNotNone(parsed, message)
        self.assertAlmostEqual(parsed["T_K"], 298.15)


class ThermoMLIntegrationTests(unittest.TestCase):
    def test_maps_thermoml_components_by_common_name_and_org_number(self):
        body = {"Compound": [
            {"RegNum": {"nOrgNum": 1}, "sCommonName": ["water"]},
            {"RegNum": {"nOrgNum": 2}, "sCommonName": ["ethanol", "ethyl alcohol"]},
        ]}
        components = [
            {"name": "水", "canonical_name": "water", "cas": "7732-18-5"},
            {"name": "乙醇", "canonical_name": "ethanol", "cas": "64-17-5"},
        ]
        self.assertEqual(_thermoml_record_component_map(body, components), {0: 1, 1: 2})

    def test_extracts_only_exact_thermoml_state_and_composition(self):
        body = {"Compound": [
            {"RegNum": {"nOrgNum": 1}, "sCommonName": ["water"]},
            {"RegNum": {"nOrgNum": 2}, "sCommonName": ["ethanol"]},
        ]}
        components = [
            {"name": "water", "canonical_name": "water", "cas": "7732-18-5"},
            {"name": "ethanol", "canonical_name": "ethanol", "cas": "64-17-5"},
        ]
        mapping = _thermoml_record_component_map(body, components)
        block = {
            "Component": [
                {"RegNum": {"nOrgNum": 1}}, {"RegNum": {"nOrgNum": 2}},
            ],
            "Variable": [
                {"nVarNumber": 1, "VariableID": {"VariableType": {"eTemperature": "Temperature, K"}}},
                {"nVarNumber": 2, "VariableID": {"RegNum": {"nOrgNum": 1}, "VariableType": {"eComponentComposition": "Mole fraction"}}},
            ],
            "Constraint": [{"ConstraintID": {"ConstraintType": {"ePressure": "Pressure, kPa"}}, "nConstraintValue": 101.3}],
            "Property": [{
                "nPropNumber": 1,
                "Property-MethodID": {"PropertyGroup": {"VolumetricProp": {"ePropName": "Mass density, kg/m3"}}},
            }],
            "NumValues": [{
                "VariableValue": [
                    {"nVarNumber": 1, "nVarValue": 298.15},
                    {"nVarNumber": 2, "nVarValue": 0.6},
                ],
                "PropertyValue": [{
                    "nPropNumber": 1, "nPropValue": 950.0,
                    "CombinedUncertainty": {"nCombExpandUncertValue": 0.5},
                }],
            }],
        }
        exact = _thermoml_block_matches_state(block, body, mapping, [0.6, 0.4], 298.15, 101325.0)
        self.assertEqual(exact[0]["density"]["value"], 950.0)
        rounded_atmosphere = _thermoml_block_matches_state(block, body, mapping, [0.6, 0.4], 298.15, 100000.0)
        self.assertEqual(rounded_atmosphere[0]["density"]["value"], 950.0)
        wrong_fraction = _thermoml_block_matches_state(block, body, mapping, [0.4, 0.6], 298.15, 101325.0)
        self.assertEqual(wrong_fraction, [])

    @patch("tools._thermoml_search_page", side_effect=__import__("requests").Timeout)
    def test_thermoml_timeout_is_marked_without_fabricating_data(self, _search):
        result = _thermoml_lookup({"components": [{"name": "water", "canonical_name": "water", "cas": "7732-18-5"}], "mole_fractions": [1.0], "T_K": 298.15, "P_Pa": 101325.0})
        self.assertFalse(result["covered"])
        self.assertTrue(result["timeout"])
        self.assertEqual(result["data"], {})


class ThermoIntegrationTests(unittest.TestCase):
    def test_external_reference_selection_skips_unselected_sources(self):
        with patch("tools._nist_lookup") as nist, patch("tools._thermoml_lookup") as thermoml, patch("tools._coolprop_lookup") as coolprop:
            result = query_properties(["water"], [1.0], 298.15, 101325, ["density"], reference_sources=[])
        self.assertEqual(result["status"], "ok")
        nist.assert_not_called()
        thermoml.assert_not_called()
        coolprop.assert_not_called()
        self.assertIn("未选择外部参考数据库", " ".join(result["warnings"]))

    def test_selected_coolprop_is_a_pure_component_only_source(self):
        fake = {"found": True, "data": {"density": {"value": 997.0, "unit": "kg/m3", "source_id": "coolprop:test"}}}
        with patch("tools._coolprop_lookup", return_value=fake) as coolprop, patch("tools._nist_lookup", return_value={"found": False, "data": {}}):
            result = query_properties(["water"], [1.0], 298.15, 101325, ["density"], reference_sources=["coolprop"])
        self.assertEqual(result["status"], "ok")
        coolprop.assert_called_once()
        self.assertEqual(result["properties"]["density"]["external_references"]["coolprop"]["value"], 997.0)

    @patch("tools._thermoml_lookup", return_value={"covered": False, "message": "offline", "data": {}})
    @patch("tools._nist_lookup", return_value={"found": False, "message": "offline", "data": {}})
    def test_water_and_ethanol_preserve_order_and_return_only_requested_property(self, _nist, _thermoml):
        result = query_properties(["water", "ethanol"], [0.6, 0.4], 298.15, 101325, ["density"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual([item["name"] for item in result["components"]], ["water", "ethanol"])
        self.assertEqual(result["mole_fractions"], [0.6, 0.4])
        self.assertEqual(set(result["properties"]), {"density"})
        self.assertEqual(result["requested_properties"], ["density"])

    @patch("tools._thermoml_lookup", return_value={"covered": False, "message": "offline", "data": {}})
    @patch("tools._nist_lookup", return_value={"found": False, "message": "offline", "data": {}})
    def test_density_and_bubble_point_request_returns_only_those_two(self, _nist, _thermoml):
        result = query_properties(["water", "ethanol"], [0.6, 0.4], 298.15, 101325, ["density", "bubble_point"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(set(result["properties"]), {"density", "bubble_point"})

    @patch("tools._thermoml_lookup", return_value={"covered": False, "message": "offline", "data": {}})
    @patch("tools._nist_lookup", return_value={"found": False, "message": "offline", "data": {}})
    def test_pure_water_computation_returns_ok(self, _nist, _thermoml):
        result = query_properties(["water"], [1.0], 298.15, 101325, ["density"])
        self.assertEqual(result["status"], "ok")
        self.assertGreater(result["properties"]["density"]["value"], 900)
        self.assertNotIn("bubble_point", result["properties"])

    @patch("tools._thermoml_lookup", return_value={"covered": False, "message": "offline", "data": {}})
    @patch("tools._nist_lookup", return_value={"found": False, "message": "offline", "data": {}})
    def test_chinese_aliases_resolve_and_preserve_input_order(self, _nist, _thermoml):
        result = query_properties(["水", "乙醇"], [0.6, 0.4], 298.15, 101325, ["density"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual([item["name"] for item in result["components"]], ["水", "乙醇"])
        self.assertEqual([item["cas"] for item in result["components"]], ["7732-18-5", "64-17-5"])
        self.assertEqual(result["mole_fractions"], [0.6, 0.4])


class LLMIntegrationTests(unittest.TestCase):
    def test_missing_temperature_and_pressure_is_asked_before_model_call(self):
        from llm_client import _missing_state_condition
        self.assertEqual(_missing_state_condition("查一下水的物性参数"), ["温度", "压力"])
        self.assertEqual(_missing_state_condition("查 25℃ 常压下水的密度"), [])

    @patch("llm_client.chat_completion")
    def test_missing_state_never_calls_model_tool(self, chat):
        results, reply = run_tool_loop("查一下水的物性参数", [], {"base_url": "local", "model": "test"})
        self.assertEqual(results, [])
        self.assertIn("温度和压力", reply)
        chat.assert_not_called()

    @patch("llm_client.chat_completion")
    def test_ratio_is_recognized_but_missing_pressure_still_blocks_query(self, chat):
        results, reply = run_tool_loop(
            "查一下 300K 下，乙醇和水按 0.4:0.7 混合的密度",
            [], {"base_url": "local", "model": "test"},
        )
        self.assertEqual(results, [])
        self.assertIn("压力", reply)
        self.assertNotIn("摩尔比", reply)
        chat.assert_not_called()
    def test_explicit_property_names_are_extracted_without_expanding_to_full_packet(self):
        from llm_client import _explicit_target_properties
        self.assertEqual(_explicit_target_properties("查335 K常压下乙酸乙酯的密度和泡点"), ["density", "bubble_point"])
        self.assertEqual(len(_explicit_target_properties("查询水的全套物性")), 6)
        self.assertIsNone(_explicit_target_properties("查乙酸乙酯物性"))

    def test_system_prompt_covers_unit_conversion_and_ratio_normalization(self):
        from llm_client import SYSTEM_PROMPT
        self.assertIn("摄氏度 t 转成 t+273.15", SYSTEM_PROMPT)
        self.assertIn("[5/6, 1/6]", SYSTEM_PROMPT)
        self.assertIn("质量比", SYSTEM_PROMPT)
        self.assertIn("乙苯”→“ethylbenzene", SYSTEM_PROMPT)

    @patch("llm_client.chat_completion")
    def test_tool_result_is_returned_without_second_llm_call(self, chat):
        chat.return_value = {
            "tool_calls": [{
                "id": "call-1",
                "function": {
                    "name": "query_properties",
                    "arguments": json.dumps({
                        "components": ["water"], "mole_fractions": [1.0],
                        "T_K": 298.15, "P_Pa": 101325,
                        "target_properties": ["density"],
                    }),
                },
            }]
        }
        expected = {"status": "ok", "properties": {"density": {"value": 1.0}}}
        query = lambda **kwargs: expected
        results, reply = run_tool_loop("查298 K常压下水的密度", [], {"base_url": "local", "model": "test"}, property_query=query)
        self.assertEqual(results, [expected])
        self.assertIn("后端物性查询", reply)
        self.assertEqual(chat.call_count, 1)

    @patch("llm_client.chat_completion")
    def test_explicit_targets_override_model_returning_every_property(self, chat):
        chat.return_value = {
            "tool_calls": [{"id": "call-targets", "function": {
                "name": "query_properties",
                "arguments": json.dumps({
                    "components": ["water"], "mole_fractions": [1.0], "T_K": 298.15,
                    "P_Pa": 101325, "target_properties": list(__import__("tools").TARGET_PROPERTIES),
                }),
            }}]
        }
        received = {}
        def query(**kwargs):
            received.update(kwargs)
            return {"status": "ok"}
        run_tool_loop("查询298 K常压下水的密度", [], {"base_url": "local", "model": "test"}, property_query=query)
        self.assertEqual(received["target_properties"], ["density"])

    @patch("llm_client.requests.post")
    def test_http_401_returns_actionable_error_without_secret(self, post):
        response = post.return_value
        response.ok = False
        response.status_code = 401
        response.json.return_value = {"error": {"message": "invalid token"}}
        from llm_client import chat_completion
        with self.assertRaises(LLMServiceError) as error:
            chat_completion([], [], {"base_url": "https://example.invalid/v1", "model": "model", "api_key": "secret"})
        self.assertIn("HTTP 401", str(error.exception))
        self.assertIn("检查 API Key", str(error.exception))
        self.assertNotIn("secret", str(error.exception))

    @patch("llm_client.requests.post")
    def test_http_503_explains_model_channel_problem(self, post):
        response = post.return_value
        response.ok = False
        response.status_code = 503
        response.json.return_value = {"error": {"message": "No available channel for model gpt-5.6-sol under group default (distributor)"}}
        from llm_client import chat_completion
        with self.assertRaises(LLMServiceError) as error:
            chat_completion([], [], {"base_url": "https://api.example/v1", "model": "gpt-5.6-sol", "api_key": "secret"})
        self.assertIn("获取模型目录", str(error.exception))

    @patch("llm_client.requests.get")
    def test_model_catalog_uses_configured_base_url_and_auth(self, get):
        from llm_client import list_models
        response = get.return_value
        response.ok = True
        response.json.return_value = {"data": [{"id": "model-a"}, {"id": "model-b"}, {"id": "model-a"}]}
        models = list_models({"base_url": "https://provider.example/v1", "api_key": "secret", "model": ""})
        self.assertEqual(models, ["model-a", "model-b"])
        self.assertEqual(get.call_args.args[0], "https://provider.example/v1/models")
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer secret")

    @patch("llm_client.requests.get")
    def test_model_catalog_accepts_common_relay_response_layout(self, get):
        from llm_client import list_models
        response = get.return_value
        response.ok = True
        response.json.return_value = {"success": True, "data": {"models": [{"model_name": "relay-model"}]}}
        self.assertEqual(list_models({"base_url": "https://provider.example/v1", "api_key": "", "model": ""}), ["relay-model"])


class SimulationContractTests(unittest.TestCase):
    @patch("local_launcher.shutil.which", return_value="py.exe")
    @patch("local_launcher.subprocess.run", return_value=SimpleNamespace(stdout="3.12"))
    def test_launcher_accepts_supported_python_launcher(self, run, _which):
        self.assertEqual(_python312_command(), ["py.exe", "-3.12"])
        self.assertEqual(run.call_args.args[0][:2], ["py.exe", "-3.12"])

    @patch("local_launcher.shutil.which", return_value="py.exe")
    @patch("local_launcher.subprocess.run", return_value=SimpleNamespace(stdout="3.9"))
    def test_launcher_rejects_python_below_minimum_version(self, _run, _which):
        self.assertIsNone(_python312_command())

    @patch("aspen_connector.requests.post")
    def test_session_specific_mcp_credentials_are_used_for_each_user(self, post):
        from aspen_connector import _mcp_request
        post.return_value.json.return_value = {"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": {"status": "connected"}}}
        result = _mcp_request("tools/call", {"name": "list_aspen_cases", "arguments": {}}, mcp_url="https://user-a.trycloudflare.com", mcp_token="a" * 44)
        self.assertEqual(result["status"], "connected")
        self.assertEqual(post.call_args.args[0], "https://user-a.trycloudflare.com/mcp")
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer " + "a" * 44)

    def test_session_mcp_url_rejects_arbitrary_hosts(self):
        from aspen_connector import AspenConnectionError, _mcp_request
        with self.assertRaisesRegex(AspenConnectionError, "安全规则"):
            _mcp_request("tools/call", {}, mcp_url="http://169.254.169.254", mcp_token="b" * 44)

    def test_aspen_mcp_exposes_typed_read_only_tools(self):
        initialized = dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(initialized["result"]["serverInfo"]["name"], "property-agent-aspen")
        listed = dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        names = {tool["name"] for tool in listed["result"]["tools"]}
        self.assertEqual(names, {"list_aspen_cases", "read_aspen_streams"})

    def test_aspen_mcp_rejects_untyped_case_id_before_com(self):
        response = dispatch({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "read_aspen_streams", "arguments": {"case_id": "not-valid"}}})
        self.assertEqual(response["error"]["code"], -32602)
    def _request(self, **updates):
        payload = {
            "components": ["water", "ethanol"],
            "mole_fractions": [0.6, 0.4],
            "flow_kmol_h": 100.0,
            "T_K": 298.15,
            "P_Pa": 101325.0,
            "engine": "Aspen Plus",
        }
        payload.update(updates)
        return build_simulation_request(**payload)

    def test_composition_is_normalized_and_component_order_is_preserved(self):
        request = self._request(mole_fractions=[6, 4])
        self.assertEqual(request["feed"]["components"], ["water", "ethanol"])
        self.assertEqual(request["feed"]["mole_fractions"], [0.6, 0.4])
        self.assertTrue(request["validation"]["composition_normalized"])

    def test_negative_or_zero_composition_is_rejected(self):
        for fractions in ([-1, 2], [0, 0]):
            with self.subTest(fractions=fractions), self.assertRaises(SimulationInputError):
                self._request(mole_fractions=fractions)

    def test_mismatched_component_and_fraction_counts_are_rejected(self):
        with self.assertRaisesRegex(SimulationInputError, "数量"):
            self._request(mole_fractions=[1.0])

    def test_nonpositive_feed_flow_is_rejected(self):
        with self.assertRaisesRegex(SimulationInputError, "进料摩尔流量"):
            self._request(flow_kmol_h=0)

    def test_schemas_define_explicit_engine_status_and_stream_output(self):
        self.assertEqual(INPUT_JSON_SCHEMA["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertIn("streams", OUTPUT_JSON_SCHEMA["required"])

    def test_excel_export_is_a_valid_two_sheet_workbook_with_provenance_columns(self):
        workbook_bytes = build_stream_workbook(
            [{"stream_id": "S1", "stream_name": "进料", "phase": "liquid", "T_K": 298.15}],
            [{"stream_id": "S1", "status": "ok", "density": 997.0, "density_source_ids": "thermo_Mixture_default_v1"}],
        )
        with zipfile.ZipFile(io.BytesIO(workbook_bytes)) as archive:
            self.assertIn("xl/workbook.xml", archive.namelist())
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            sheet_names = [sheet.attrib["name"] for sheet in workbook.findall("x:sheets/x:sheet", namespace)]
            self.assertEqual(sheet_names, ["Aspen流股数据", "Agent物性数据"])
            for path in ("xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml", "xl/styles.xml"):
                ET.fromstring(archive.read(path))

    def test_aspen_unit_conversions_require_known_units(self):
        self.assertAlmostEqual(_to_kelvin(25.0, "C"), 298.15)
        self.assertAlmostEqual(_to_pascal(1.0, "bar"), 100000.0)
        self.assertAlmostEqual(_to_kmol_h(1000.0, "mol/hr"), 1.0)
        self.assertIsNone(_to_pascal(10.0, "unknown pressure unit"))

    @patch("simulation.query_properties")
    def test_enrichment_routes_single_phase_and_skips_two_phase(self, query):
        query.side_effect = [
            {"status": "ok", "phase_state": "single_liquid_assumed", "properties": {"density": {"value": 995, "unit": "kg/m3", "source_ids": ["thermo_liquid"]}}},
            {"status": "out_of_domain", "phase_state": "single_gas_detected", "partial_properties": {"density": {"value": 1.2, "unit": "kg/m3", "phase": "gas", "source_ids": ["thermo_gas"]}}},
        ]
        streams = [
            {"stream_id": "L", "stream_name": "Liquid", "phase": "liquid", "components": ["water"], "mole_fractions": [1.0], "flow_kmol_h": 1, "T_K": 298.15, "P_Pa": 101325, "read_status": "ok"},
            {"stream_id": "G", "stream_name": "Vapor", "phase": "gas", "components": ["water"], "mole_fractions": [1.0], "flow_kmol_h": 1, "T_K": 400.0, "P_Pa": 101325, "read_status": "ok"},
            {"stream_id": "2P", "stream_name": "Two phase", "phase": "two_phase", "components": ["water"], "mole_fractions": [1.0], "flow_kmol_h": 1, "T_K": 350.0, "P_Pa": 101325, "read_status": "ok"},
        ]
        source_rows, property_rows = enrich_streams(streams)
        self.assertEqual(len(source_rows), 3)
        self.assertEqual([row["status"] for row in property_rows], ["ok", "gas_partial", "two_phase_skipped"])
        self.assertEqual(query.call_count, 2)
        self.assertEqual(query.call_args_list[1].kwargs["include_references"], False)

    def test_active_case_requires_explicit_zero_errors_and_stopped_engine(self):
        class FakeCollection:
            def __init__(self, items):
                self.items = items
                self.Count = len(items)

            def Item(self, index):
                if index < 0 or index >= len(self.items):
                    raise IndexError(index)
                return self.items[index]

        class FakeNode:
            def __init__(self, name="", value=None, elements=()):
                self.Name = name
                self.Value = value
                self.Elements = FakeCollection(list(elements))
                self.Units = ""

        stream = FakeNode("S1")
        error_node = FakeNode("PER_ERROR", 0)

        class FakeTree:
            def FindNode(self, path):
                if path == r"\Data\Results Summary\Run-Status\Output\PER_ERROR":
                    return error_node
                if path == r"\Data\Streams":
                    return FakeNode("Streams", elements=[stream])
                if path == r"\Unit Table":
                    return None
                return None

        document = SimpleNamespace(FullName=r"C:\Cases\case.bkp", Engine=SimpleNamespace(IsRunning=False), Tree=FakeTree())
        case_record = ("!Apwn.Document.38.0", document.FullName, document)
        with patch("aspen_connector._running_documents", return_value=[case_record]):
            inventory = _scan_on_com_thread()
        self.assertEqual(inventory["cases"][0]["run_status"], "converged")
        self.assertTrue(inventory["cases"][0]["eligible"])

        document.Engine.IsRunning = True
        with patch("aspen_connector._running_documents", return_value=[case_record]):
            with self.assertRaisesRegex(AspenConnectionError, "正在运行"):
                _read_selected_on_com_thread(_case_id(case_record[0], case_record[1]))

    def test_scan_distinguishes_missing_aspen_from_no_open_cases(self):
        with patch("aspen_connector._running_documents", return_value=[]), patch("aspen_connector._aspen_com_registered", return_value=False):
            missing = _scan_on_com_thread()
        self.assertEqual(missing["status"], "aspen_not_detected")
        self.assertEqual(missing["cases"], [])

        with patch("aspen_connector._running_documents", return_value=[]), patch("aspen_connector._aspen_com_registered", return_value=True):
            no_cases = _scan_on_com_thread()
        self.assertEqual(no_cases["status"], "no_active_cases")
        self.assertEqual(no_cases["cases"], [])

    def test_stream_values_are_converted_to_si_and_phase_is_classified(self):
        class FakeCollection:
            def __init__(self, items):
                self.items = items
                self.Count = len(items)

            def Item(self, index):
                return self.items[index]

        class FakeNode:
            def __init__(self, name="", value=None, units="", elements=()):
                self.Name = name
                self.Value = value
                self.Units = units
                self.Elements = FakeCollection(list(elements))

            def FindNode(self, name):
                return next((item for item in self.Elements.items if item.Name == name), None)

        fractions = FakeNode("MIXED", elements=[FakeNode("water", 0.6), FakeNode("ethanol", 0.4)])
        nodes = {
            r"\Data\Streams": FakeNode("Streams", elements=[FakeNode("S1")]),
            r"\Data\Streams\S1\Output\TEMP_OUT\MIXED": FakeNode("MIXED", 25.0, "C"),
            r"\Data\Streams\S1\Output\PRES_OUT\MIXED": FakeNode("MIXED", 1.0, "bar"),
            r"\Data\Streams\S1\Output\MOLEFLMX\MIXED": FakeNode("MIXED", 1.0, "mol/s"),
            r"\Data\Streams\S1\Output\MOLEFRAC\MIXED": fractions,
            r"\Data\Streams\S1\Output\VFRAC\MIXED": FakeNode("MIXED", 0.4),
        }
        tree = SimpleNamespace(FindNode=lambda path: nodes.get(path))
        payload = _read_streams(SimpleNamespace(Tree=tree), "0123456789abcdefabcd")
        stream = payload["streams"][0]
        self.assertAlmostEqual(stream["T_K"], 298.15)
        self.assertAlmostEqual(stream["P_Pa"], 100000.0)
        self.assertAlmostEqual(stream["flow_kmol_h"], 3.6)
        self.assertEqual(stream["components"], ["water", "ethanol"])
        self.assertEqual(stream["phase"], "two_phase")


if __name__ == "__main__":
    unittest.main()
