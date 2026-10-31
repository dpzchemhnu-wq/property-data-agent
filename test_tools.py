"""物性后端的无网络边界测试，可用 python -m unittest 运行。"""

import unittest
from unittest.mock import patch
import json

from llm_client import LLMServiceError, run_tool_loop
from local_parser import parse_explicit_query, split_values
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

    def test_fraction_sum_is_not_silently_normalized(self):
        result = query_properties(["water", "ethanol"], [0.6, 0.3], 298.15, 101325, ["density"])
        self.assertEqual(result["status"], "needs_input")

    def test_temperature_domain_is_enforced_before_solver(self):
        result = query_properties(["water"], [1.0], 451.0, 101325, ["density"])
        self.assertEqual(result["status"], "out_of_domain")

    def test_vapor_state_returns_clear_liquid_property_message(self):
        result = query_properties(["water"], [1.0], 400.0, 101325.0, ["density"])
        self.assertEqual(result["status"], "out_of_domain")
        self.assertIn("气态", result["message"])
        self.assertEqual(result["phase_state"], "single_gas_detected")

    def test_vapor_state_keeps_safe_partial_enthalpy_only(self):
        result = query_properties(["water"], [1.0], 400.0, 101325.0, ["density", "enthalpy"])
        self.assertEqual(result["status"], "out_of_domain")
        self.assertIn("enthalpy", result["partial_properties"])
        self.assertNotIn("density", result["partial_properties"])

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
        wrong_fraction = _thermoml_block_matches_state(block, body, mapping, [0.4, 0.6], 298.15, 101325.0)
        self.assertEqual(wrong_fraction, [])

    @patch("tools._thermoml_search_page", side_effect=__import__("requests").Timeout)
    def test_thermoml_timeout_is_marked_without_fabricating_data(self, _search):
        result = _thermoml_lookup({"components": [{"name": "water", "canonical_name": "water", "cas": "7732-18-5"}], "mole_fractions": [1.0], "T_K": 298.15, "P_Pa": 101325.0})
        self.assertFalse(result["covered"])
        self.assertTrue(result["timeout"])
        self.assertEqual(result["data"], {})


class ThermoIntegrationTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
