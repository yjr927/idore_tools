import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("openai_analysis", Path(__file__).resolve().parents[1] / "scripts" / "analyze_tiktok_candidates.py")
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)


def data(count=1):
    return {"videos": [{"id": "tiktok-" + str(i), "title": "untrusted caption", "author": "creator", "motion_verified": False, "trend_verified": False} for i in range(count)], "runs": [{"status": "succeeded"}], "last_success_at": "preserve"}


def response(items):
    return {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({"items": items})}]}]}


class OpenAIAnalysisTests(unittest.TestCase):
    def test_missing_configuration_never_calls_api_and_preserves_candidates(self):
        original = data()
        for key, model in (("", "model"), ("key", "")):
            with patch.object(analysis, "request_openai") as request:
                result = analysis.analyze_data(original, api_key=key, model=model)
                request.assert_not_called()
            self.assertEqual(result["analysis_status"], "not_configured")
            self.assertEqual(result["videos"], original["videos"])
            self.assertEqual(result["runs"], original["runs"])
            self.assertNotIn("analysis_status", original)

    def test_one_bounded_call_only_allowed_text_summary(self):
        original = data(12)
        def request(payload, key):
            rows = json.loads(payload["input"])["untrusted_candidate_metadata"]
            self.assertEqual(len(rows), 8)
            self.assertEqual(payload["max_output_tokens"], 1500)
            self.assertFalse(payload["store"])
            self.assertNotIn("api-key-for-test", json.dumps(payload))
            return response([{"id": rows[0]["id"], "summary": "文字线索：标题提及舞蹈，需看原片。", "suggested_category": "hands"}])
        result = analysis.analyze_data(original, api_key="api-key-for-test", model="example-model", request_fn=request)
        self.assertEqual(result["analysis_status"], "completed")
        self.assertEqual(result["analysis"]["analyzed_count"], 1)
        self.assertEqual(result["videos"][0]["metadata_analysis"]["basis"], "metadata_only")
        self.assertTrue(all(v["motion_verified"] is False and v["trend_verified"] is False for v in result["videos"]))
        self.assertEqual(result["videos"][1:], original["videos"][1:])

    def test_model_cannot_inject_new_ids_or_verification_flags(self):
        for item in (
            {"id": "injected", "summary": "文字线索：未知", "suggested_category": "unknown"},
            {"id": "tiktok-0", "summary": "文字线索：未知", "suggested_category": "unknown", "motion_verified": True},
            {"id": "tiktok-0", "summary": "confirmed single dancer", "suggested_category": "hands"},
        ):
            original = data()
            result = analysis.analyze_data(original, api_key="key", model="model", request_fn=lambda *args: response([item]))
            self.assertEqual(result["analysis_status"], "failed")
            self.assertEqual(result["videos"], original["videos"])

    def test_incomplete_or_error_responses_preserve_data_without_raw_error(self):
        original = data()
        result = analysis.analyze_data(original, api_key="key", model="model", request_fn=lambda *args: {"status": "incomplete"})
        self.assertEqual(result["analysis_status"], "failed")
        self.assertEqual(result["videos"], original["videos"])
        def fail(*args):
            raise RuntimeError("api-key-for-test private response")
        result = analysis.analyze_data(original, api_key="key", model="model", request_fn=fail)
        self.assertNotIn("api-key-for-test", json.dumps(result))
        self.assertNotIn("private response", json.dumps(result))

    def test_empty_data_never_calls_api(self):
        with patch.object(analysis, "request_openai") as request:
            result = analysis.analyze_data(data(0), api_key="key", model="model")
        request.assert_not_called()
        self.assertEqual(result["analysis_status"], "empty")

    def test_cli_missing_key_writes_status_and_same_candidate_data(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            source = Path(tmp) / "source.json"
            target = Path(tmp) / "nested" / "result.json"
            original = data()
            source.write_text(json.dumps(original))
            self.assertEqual(analysis.main(["--input", str(source), "--output", str(target)]), 0)
            result = json.loads(target.read_text())
            self.assertEqual(result["analysis_status"], "not_configured")
            self.assertEqual(result["videos"], original["videos"])


if __name__ == "__main__":
    unittest.main()
