import json
import tempfile
import threading
import unittest
from pathlib import Path

from script_agent.pipeline import (
    AnalysisConfig,
    analyze_character_profile,
    analyze_quick_preview,
    analyze_run,
    prepare_script,
)


def schema_value(schema):
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "object":
        return {key: schema_value(child) for key, child in schema.get("properties", {}).items()}
    if kind == "array":
        return []
    if kind == "string":
        return ""
    if kind == "number":
        return 0.8
    if kind == "integer":
        return 1
    if kind == "boolean":
        return False
    return None


class FakeClient:
    def generate(self, *, schema_name, schema, input_text, **kwargs):
        result = schema_value(schema)
        source = json.loads(input_text)
        if schema_name == "scene_analysis":
            result["scene_id"] = source["scene_id"]
            result["heading"] = source["heading"]
            result["synopsis"] = "甲与乙交谈。"
            result["characters"] = [
                self._character(schema["properties"]["characters"]["items"], "甲", source["scene_id"], "E-A"),
                self._character(schema["properties"]["characters"]["items"], "乙", source["scene_id"], "E-B"),
            ]
            relationship_schema = schema["properties"]["relationships"]["items"]
            relationship = schema_value(relationship_schema)
            relationship["person_a"] = "甲"
            relationship["person_b"] = "乙"
            relationship["stage"] = "试探"
            relationship["evidence_ids"] = ["E-A", "E-B"]
            result["relationships"] = [relationship]
        elif schema_name == "quick_script_preview":
            character_schema = schema["properties"]["characters"]["items"]
            first = schema_value(character_schema)
            first.update(
                name="甲",
                one_line_portrait="克制谨慎，习惯先观察再行动",
                evidence_scene_ids=["S0001"],
                confidence=0.78,
            )
            second = schema_value(character_schema)
            second.update(
                name="乙",
                one_line_portrait="态度直接，会主动推动关系",
                evidence_scene_ids=["S0001"],
                confidence=0.75,
            )
            result["characters"] = [first, second]
            relation_schema = schema["properties"]["relationships"]["items"]
            relation = schema_value(relation_schema)
            relation.update(
                person_a="甲",
                person_b="乙",
                relation_label="试探",
                one_line_summary="乙更主动，甲仍在保持距离",
                evidence_scene_ids=["S0001"],
                confidence=0.72,
            )
            result["relationships"] = [relation]
        elif schema_name in {"character_window", "character_card"}:
            result["character_name"] = source["character_name"]
        elif schema_name in {"relationship_window", "relationship_file"}:
            result["person_a"] = source["person_a"]
            result["person_b"] = source["person_b"]
            if schema_name == "relationship_file":
                result["current_stage"] = "试探"
        return result

    @staticmethod
    def _character(schema, name, scene_id, evidence_id):
        result = schema_value(schema)
        result["name"] = name
        evidence_schema = schema["properties"]["actions"]["items"]
        evidence = schema_value(evidence_schema)
        evidence["evidence_id"] = evidence_id
        evidence["scene_id"] = scene_id
        evidence["source_locator"] = "L000001"
        evidence["kind"] = "行为"
        evidence["quote_or_action"] = "发生一个可观察行为"
        result["actions"] = [evidence]
        result["confidence"] = 0.8
        result["review_status"] = "人物理解"
        return result


class CountingFakeClient(FakeClient):
    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()

    def generate(self, *, schema_name, **kwargs):
        with self.lock:
            self.calls.append(schema_name)
        return super().generate(schema_name=schema_name, **kwargs)


class PipelineTests(unittest.TestCase):
    def test_end_to_end_with_fake_model(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = root / "sample.txt"
            script.write_text("第一场 客厅 日\n甲：你好。\n乙：你好。\n", encoding="utf-8")
            run_dir = root / "run"
            manifest = prepare_script(script, run_dir)
            self.assertEqual(1, manifest["scene_count"])
            bundle = analyze_run(run_dir, AnalysisConfig(), client=FakeClient())
            self.assertEqual({"甲", "乙"}, set(bundle["characters"].keys()))
            self.assertEqual(1, len(bundle["relationships"]))
            self.assertTrue((run_dir / "output" / "report.html").exists())
            self.assertTrue((run_dir / "output" / "relationship-graph.json").exists())

    def test_fast_mode_uses_only_scene_calls_and_builds_local_preview(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = root / "sample.txt"
            script.write_text("第一场 客厅 日\n甲：你好。\n乙：你好。\n", encoding="utf-8")
            run_dir = root / "run"
            prepare_script(script, run_dir)
            client = CountingFakeClient()
            bundle = analyze_run(
                run_dir,
                AnalysisConfig(fast_mode=True, max_workers=3),
                client=client,
            )
            self.assertEqual(1, client.calls.count("scene_analysis"))
            self.assertNotIn("character_card", client.calls)
            self.assertNotIn("relationship_file", client.calls)
            self.assertNotIn("character_window", client.calls)
            self.assertNotIn("relationship_window", client.calls)
            self.assertTrue(bundle["meta"]["fast_mode"])
            self.assertEqual("preview", bundle["meta"]["analysis_depth"])
            self.assertEqual("preview", bundle["characters"]["甲"]["_meta"]["profile_mode"])
            self.assertEqual("preview", next(iter(bundle["relationships"].values()))["_meta"]["profile_mode"])

    def test_quick_overview_uses_one_short_model_call_for_the_whole_script(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = root / "sample.txt"
            script.write_text(
                "第一场 客厅 日\n甲：你好。\n乙：你好。\n\n"
                "第二场 厨房 夜\n甲：晚安。\n乙：晚安。\n",
                encoding="utf-8",
            )
            run_dir = root / "run"
            prepare_script(script, run_dir)
            client = CountingFakeClient()
            bundle = analyze_quick_preview(run_dir, client=client)
            self.assertEqual(["quick_script_preview"], client.calls)
            self.assertEqual("quick_overview", bundle["meta"]["analysis_depth"])
            self.assertEqual({"甲", "乙"}, set(bundle["characters"]))
            self.assertEqual(1, len(bundle["relationships"]))
            self.assertEqual(
                "克制谨慎，习惯先观察再行动",
                bundle["characters"]["甲"]["one_line_positioning"][0]["claim"],
            )

    def test_quick_overview_can_continue_to_deep_character_analysis(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = root / "sample.txt"
            script.write_text("第一场 客厅 日\n甲：你好。\n乙：你好。\n", encoding="utf-8")
            run_dir = root / "run"
            prepare_script(script, run_dir)
            client = CountingFakeClient()
            analyze_quick_preview(run_dir, client=client)
            card = analyze_character_profile(
                run_dir,
                "甲",
                AnalysisConfig(
                    extract_model="fake-model",
                    synthesis_model="fake-model",
                    max_workers=2,
                ),
                client=client,
            )
            self.assertEqual("full", card["_meta"]["profile_mode"])
            self.assertEqual(1, client.calls.count("scene_analysis"))
            self.assertEqual(1, client.calls.count("character_window"))
            self.assertEqual(1, client.calls.count("character_card"))

    def test_preview_character_can_be_upgraded_on_demand(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = root / "sample.txt"
            script.write_text("第一场 客厅 日\n甲：你好。\n乙：你好。\n", encoding="utf-8")
            run_dir = root / "run"
            prepare_script(script, run_dir)
            client = CountingFakeClient()
            analyze_run(
                run_dir,
                AnalysisConfig(fast_mode=True, max_workers=3),
                client=client,
            )
            card = analyze_character_profile(
                run_dir,
                "甲",
                AnalysisConfig(synthesis_model="fake-model"),
                client=client,
            )
            self.assertEqual("full", card["_meta"]["profile_mode"])
            self.assertEqual(1, client.calls.count("character_window"))
            self.assertEqual(1, client.calls.count("character_card"))
            saved = json.loads((run_dir / "output" / "report.json").read_text(encoding="utf-8"))
            self.assertEqual("full", saved["characters"]["甲"]["_meta"]["profile_mode"])

    def test_partial_analysis_marks_report_and_keeps_total_scene_count(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = root / "sample.txt"
            script.write_text(
                "第一场 客厅 日\n甲：你好。\n乙：你好。\n\n"
                "第二场 厨房 夜\n甲：晚安。\n乙：晚安。\n",
                encoding="utf-8",
            )
            run_dir = root / "run"
            prepare_script(script, run_dir)
            bundle = analyze_run(
                run_dir,
                AnalysisConfig(),
                client=FakeClient(),
                scene_limit=1,
            )
            self.assertTrue(bundle["meta"]["partial_analysis"])
            self.assertEqual(1, bundle["meta"]["scene_count_analyzed"])
            self.assertEqual(2, bundle["meta"]["scene_count_total"])


if __name__ == "__main__":
    unittest.main()
