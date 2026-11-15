from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.request import Request


spec = importlib.util.spec_from_file_location("api_connection_check",
    Path(__file__).resolve().parents[1] / "scripts" / "check_api_connections.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


class APIConnectionTests(unittest.TestCase):
    def setUp(self):
        self.openai = Mock()
        self.router = Mock()
        self.openai.connect.return_value = {"connected": True, "account_id": "private-account", "balance": 55}
        self.router.connect.return_value = {"connected": True, "account_id": "private-account", "balance": 99}
        self.openai.models.return_value = {"text_models": [{"id": "gpt-5.5", "owner": "private-account"}],
            "image_models": [{"id": "gpt-image-2.5-sunburst"}, {"id": "https://unsafe.example/private-openai-key"}]}
        self.router.models.return_value = {"models": [{"id": "bytedance/seedance-2.0-fast",
            "supported_durations": [5, 8, True], "supported_resolutions": ["720p", "https://unsafe.example"],
            "supported_aspect_ratios": ["9:16"], "supported_frame_images": ["first_frame", "last_frame"],
            "generate_audio": False, "account_id": "private-account", "balance": 99,
            "pricing_skus": {"video_tokens": "0.0000042", "video_tokens_without_audio": "0.0000042",
                "video_tokens_with_video_input": "0.000002475", "account_id": "123456"}}]}

    def run_check(self, extra_env=None):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            step = Path(tmp) / "step-summary.md"
            env = {"OPENAI_API_KEY": "private-openai-key", "OPENROUTER_API_KEY": "private-router-key",
                "API_CHECK_OUTPUT_DIR": str(output), "GITHUB_STEP_SUMMARY": str(step)}
            env.update(extra_env or {})
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, env, clear=True), \
                    patch.object(check.creative_service, "CreativeService", return_value=self.openai) as creative_constructor, \
                    patch.object(check.video_service, "VideoService", return_value=self.router), \
                    redirect_stdout(stdout), redirect_stderr(stderr):
                result = check.main()
            summary = json.loads((output / "api-check-summary.json").read_text())
            rendered = step.read_text() if step.exists() else ""
            return result, summary, rendered + stdout.getvalue() + stderr.getvalue(), creative_constructor

    def test_reports_only_public_ids_capabilities_and_numeric_catalog_prices(self):
        result, summary, rendered, _ = self.run_check()
        self.assertEqual(result, 0); self.assertEqual(summary["paid_requests"], 0)
        self.assertEqual(summary["providers"]["openai"]["text_models"], ["gpt-5.5"])
        self.assertEqual(summary["providers"]["openai"]["image_models"], ["gpt-image-2.5-sunburst"])
        model = summary["providers"]["openrouter"]["video_models"][0]
        self.assertEqual(model["supported_durations"], [5, 8])
        self.assertEqual(model["supported_resolutions"], ["720p"])
        self.assertEqual(model["pricing_skus"]["video_tokens"], "0.0000042")
        self.assertNotIn("account_id", model["pricing_skus"])
        for private in ("private-openai-key", "private-router-key", "private-account", "balance", "unsafe.example"):
            self.assertNotIn(private, json.dumps(summary) + rendered)
        self.openai.generate_images.assert_not_called(); self.openai.generate_draft.assert_not_called()
        self.router.submit.assert_not_called(); self.router.refresh.assert_not_called(); self.router.download.assert_not_called()

    def test_provider_failures_are_independent_and_raw_exceptions_are_never_exported(self):
        error = RuntimeError("private-openai-key private-account https://unsafe.example")
        error.status = 403
        self.openai.connect.side_effect = error
        result, summary, rendered, _ = self.run_check()
        self.assertEqual(result, 1); self.assertEqual(summary["status"], "partial")
        self.assertEqual(summary["providers"]["openai"]["reason"], "permission_denied")
        self.assertEqual(summary["providers"]["openrouter"]["status"], "passed")
        self.router.connect.assert_called_once()
        self.assertNotIn(str(error), rendered + json.dumps(summary))
        self.assertNotIn("Traceback", rendered)

    def test_missing_secret_does_not_construct_that_provider_or_block_the_other(self):
        result, summary, _, creative_constructor = self.run_check({"OPENAI_API_KEY": ""})
        self.assertEqual(result, 1)
        creative_constructor.assert_not_called()
        self.assertEqual(summary["providers"]["openai"]["reason"], "missing_secret")
        self.assertEqual(summary["providers"]["openrouter"]["status"], "passed")

    def test_read_only_gate_blocks_paid_and_arbitrary_endpoints_before_transport(self):
        delegate = Mock(return_value=(200, {}, b"{}"))
        read = check.read_only_transport(delegate, check.OPENAI_READS | check.OPENROUTER_READS)
        for url, method, body in (("https://api.openai.com/v1/responses", "POST", b"{}"),
                ("https://api.openai.com/v1/images/edits", "POST", b"{}"),
                ("https://openrouter.ai/api/v1/videos", "POST", b"{}"),
                ("https://unsafe.example/v1/models", "GET", None),
                ("https://api.openai.com/v1/models", "GET", b"{}")):
            with self.subTest(url=url, method=method), self.assertRaises(check.ReadOnlyViolation):
                read(Request(url, method=method, data=body), timeout=120, max_bytes=10 * 1024 * 1024)
        delegate.assert_not_called()
        read(Request("https://api.openai.com/v1/models", method="GET"), timeout=120, max_bytes=10 * 1024 * 1024)
        self.assertEqual(delegate.call_args.kwargs, {"timeout": 30, "max_bytes": check.MAX_BYTES})


if __name__ == "__main__":
    unittest.main()
