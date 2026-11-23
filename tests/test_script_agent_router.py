import io
import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from unittest.mock import patch

from script_agent.openai_client import OPENROUTER_URL, ModelCallError, StructuredModelClient
from script_agent.pipeline import AnalysisConfig, _normalize_scene_evidence, _resolve_citations
from script_agent.segmenter import numbered_scene_text, split_scenes


SCHEMA = {"type": "object", "properties": {"value": {"type": "number", "maximum": 1}},
          "required": ["value"], "additionalProperties": False}
REQUEST = dict(model="openai/test", instructions="system", input_text="scene",
               schema_name="test", schema=SCHEMA, max_output_tokens=900)


def response(content, reason="stop"):
    value = {"choices": [{"finish_reason": reason, "message": {"content": content}}]}
    return io.BytesIO(json.dumps(value).encode())


class OpenRouterTests(unittest.TestCase):
    def test_uses_provider_schema_model_and_private_key(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-secret", "OPENROUTER_TEXT_MODEL": "openai/test"}):
            client = StructuredModelClient()
            self.assertEqual(AnalysisConfig().extract_model, "openai/test")
        with patch.object(client._opener, "open", return_value=response('{"value":1}')) as send:
            self.assertEqual(client.generate(**REQUEST), {"value": 1})
        req = send.call_args.args[0]
        self.assertEqual(req.full_url, OPENROUTER_URL)
        self.assertEqual(req.get_header("Authorization"), "Bearer test-secret")
        body = json.loads(req.data)
        self.assertEqual(body["response_format"]["json_schema"], {"name": "test", "strict": True, "schema": SCHEMA})
        self.assertEqual(body["max_tokens"], 900)
        self.assertEqual(body["provider"], {"require_parameters": True})
        self.assertEqual(body["model"], "openai/test")
        self.assertNotIn("test-secret", req.data.decode())

    def test_invalid_or_incomplete_output_is_not_cached_or_retried(self):
        client = StructuredModelClient("test")
        for body in (response("{}"), response('{"value":true}'), response('{"value":2}'),
                     response('{"value":0}', "length"), io.BytesIO(b"bad json")):
            with self.subTest(), patch.object(client._opener, "open", return_value=body) as send:
                with self.assertRaises(ModelCallError):
                    client.generate(**REQUEST)
                send.assert_called_once()

    def test_http_errors_do_not_show_private_provider_bodies_or_bootstrap_retries(self):
        client = StructuredModelClient("test")
        error = HTTPError(OPENROUTER_URL, 402, "confidential details", {}, io.BytesIO(b"secret"))
        with patch.object(client._opener, "open", side_effect=error) as send:
            with self.assertRaises(ModelCallError) as failure:
                client.generate(**REQUEST)
        self.assertIn("402", str(failure.exception))
        self.assertNotIn("confidential", str(failure.exception))
        self.assertNotIn("secret", str(failure.exception))
        send.assert_called_once()

    def test_call_limit_applies_before_dispatch_under_concurrency(self):
        with patch.dict(os.environ, {"SCRIPT_AGENT_MAX_MODEL_CALLS": "2"}):
            client = StructuredModelClient("test")
        def submit(_):
            try:
                client.generate(**REQUEST)
                return True
            except ModelCallError:
                return False
        with patch.object(client._opener, "open", side_effect=lambda *a, **k: response('{"value":0}')) as send:
            with ThreadPoolExecutor(max_workers=4) as pool:
                self.assertEqual(sum(pool.map(submit, range(7))), 2)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(client.call_counts(), {"test": 2})


class GroundingTests(unittest.TestCase):
    def test_only_verbatim_dialogue_from_cited_lines_survives(self):
        scene = split_scenes("\n\n甲：灯灭了。\n乙：开门。\n")[0]
        self.assertTrue(numbered_scene_text(scene).startswith("L000003 "))
        def quote(key, location, text):
            return {"evidence_id": key, "source_locator": location, "quote_or_action": text, "kind": "台词"}
        analysis = {
            "characters": [{"explicit_words": [quote("EA", "L000003", "灯灭了。"),
                            quote("EB", "L000004", "灯灭了。"), quote("EC", "L000003", "开灯。"),
                            quote("ED", "L000008", "开门。")]}],
            "story_facts": [{"evidence_ids": ["EA", "EB", "EC", "ED"]}],
        }
        _normalize_scene_evidence(analysis, scene)
        self.assertEqual(len(analysis["characters"][0]["explicit_words"]), 1)
        self.assertEqual(analysis["story_facts"][0]["evidence_ids"], ["S0001-E001"])
        self.assertEqual(len(analysis["unresolved_questions"]), 1)

    def test_unknown_synthesis_citation_downgrades_assertion(self):
        item = {"claim": "猜测", "review_status": "人物理解", "evidence_ids": ["missing"],
                "counterevidence_ids": ["real", "other"]}
        _resolve_citations(item, {"real": "real"})
        self.assertEqual(item["review_status"], "待确认")
        self.assertEqual(item["evidence_ids"], [])
        self.assertEqual(item["counterevidence_ids"], ["real"])
