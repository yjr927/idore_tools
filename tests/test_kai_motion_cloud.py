import asyncio
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch


spec = importlib.util.spec_from_file_location("kai_motion_cloud",
    Path(__file__).resolve().parents[1] / "scripts" / "run_kai_motion_cloud.py")
kai = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kai)
# Reserved example domain: this is a nonfunctional test sentinel, not a media
# capability copied from TikTok or a real signed download address.
REFERENCE = "https://media.example/clip.mp4?opaque=DO_NOT_WRITE_REFERENCE"


class KaiMotionCloudTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.output = Path(self.tmp.name) / "output"

    def tearDown(self):
        self.tmp.cleanup()

    def execute(self, runner=None, resolver=None, environment=None):
        env = {"GITHUB_ACTIONS": "true", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_RUN_ID": "12345",
            "KAI_ALLOW_PAID": "true", "OPENROUTER_API_KEY": "test-private-key",
            "VIDEO_OUTPUT_DIR": str(self.output), "GENERATION_INPUTS": "prior-safe-inputs"}
        env.update(environment or {})
        resolver = resolver or AsyncMock(return_value=(REFERENCE, 7.5))
        runner = runner or Mock(return_value=0)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, env, clear=True), patch.object(kai, "resolve_player", resolver), \
                patch.object(kai, "range_preflight") as preflight, \
                patch.object(kai.run_video_generation, "main", runner), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            result = kai.main()
            self.assertEqual(os.environ.get("GENERATION_INPUTS"), env["GENERATION_INPUTS"])
        summary = json.loads((self.output / "cloud-generation-summary.json").read_text())
        persisted = "".join(path.read_text() for path in self.output.rglob("*.json"))
        self.assertNotIn(REFERENCE, persisted + stdout.getvalue() + stderr.getvalue())
        self.assertNotIn("test-private-key", persisted + stdout.getvalue() + stderr.getvalue())
        return result, summary, resolver, runner, preflight

    def test_authorized_resolved_media_stays_in_memory_and_invokes_one_fixed_video_request(self):
        def run_video():
            settings = json.loads(os.environ["GENERATION_INPUTS"])
            self.assertEqual(settings["reference_video_url"], REFERENCE)
            self.assertEqual(settings["first_frame_url"], kai.IMAGE_URL)
            self.assertEqual(settings["model"], "bytedance/seedance-2.0-fast")
            self.assertEqual(settings["mode"], "references")
            self.assertEqual(settings["duration"], 6)
            self.assertEqual(settings["resolution"], "720p")
            self.assertEqual(settings["aspect_ratio"], "9:16")
            self.assertFalse(settings["generate_audio"])
            self.assertTrue(settings["allow_paid"])
            self.output.mkdir(parents=True, exist_ok=True)
            (self.output / "generation-summary.json").write_text(json.dumps({"id": "video-" + "a" * 32, "cost": 0.1}))
            return 0
        runner = Mock(side_effect=run_video)
        status, summary, _, _, preflight = self.execute(runner)
        self.assertEqual(status, 0); self.assertEqual(summary["status"], "completed")
        self.assertEqual(preflight.call_args_list[0].args, (kai.IMAGE_URL, "image_url"))
        self.assertEqual(preflight.call_args_list[1].args, (REFERENCE, "video_url"))
        runner.assert_called_once_with()

    def test_authorization_challenge_age_gate_and_unsupported_duration_prevent_generation(self):
        for environment in ({"KAI_ALLOW_PAID": "false"}, {"GITHUB_RUN_ATTEMPT": "2"}):
            status, _, resolver, runner, preflight = self.execute(environment=environment)
            self.assertEqual(status, 1); resolver.assert_not_called(); runner.assert_not_called(); preflight.assert_not_called()
        resolver = AsyncMock(side_effect=kai.PreflightStopped("public_player_access_blocked"))
        status, summary, _, runner, _ = self.execute(resolver=resolver)
        self.assertEqual(status, 1); self.assertEqual(summary["reason"], "public_player_access_blocked"); runner.assert_not_called()
        status, summary, _, runner, preflight = self.execute(resolver=AsyncMock(return_value=(REFERENCE, 16)))
        self.assertEqual(status, 1); self.assertEqual(summary["reason"], "reference_video_duration_unsupported")
        runner.assert_not_called(); preflight.assert_not_called()
        class Locator:
            first = None
            async def count(self): return 0
            async def inner_text(self, **kwargs): return "Enter your birthdate to continue"
        class Page:
            url = kai.PLAYER_URL
            def locator(self, selector): return Locator()
            def get_by_role(self, role): return Locator()
        self.assertTrue(asyncio.run(kai.access_blocked(Page())))

    def test_range_checks_have_no_api_authorization_and_reject_nonmedia(self):
        requests = []
        class Response:
            status = 206
            def __init__(self, image):
                self.headers = {"Content-Type": "image/png" if image else "video/mp4"}
                self.raw = b"\x89PNG\r\n\x1a\n" if image else b"\x00\x00\x00\x18ftypmp42"
            def read(self, maximum): return self.raw[:maximum]
            def __enter__(self): return self
            def __exit__(self, *args): return False
        def open_request(request, **kwargs):
            requests.append(request)
            self.assertIsNone(request.get_header("Authorization"))
            self.assertEqual(request.get_header("Range"), "bytes=0-1023")
            self.assertEqual(request.get_method(), "GET")
            return Response(request.full_url == kai.IMAGE_URL)
        opener = Mock(); opener.open.side_effect = open_request
        with patch.object(kai, "build_opener", return_value=opener):
            kai.range_preflight(kai.IMAGE_URL, "image_url")
            kai.range_preflight(REFERENCE, "video_url")
        self.assertEqual(len(requests), 2)
        invalid = Response(False); invalid.headers["Content-Type"] = "text/html"
        opener.open.side_effect = None; opener.open.return_value = invalid
        with patch.object(kai, "build_opener", return_value=opener), self.assertRaises(kai.PreflightStopped):
            kai.range_preflight(REFERENCE, "video_url")

    def test_runner_failure_restores_environment_and_cannot_leak_transient_reference(self):
        def fail():
            self.assertIn(REFERENCE, os.environ["GENERATION_INPUTS"])
            print("Unexpected detail " + REFERENCE, file=__import__("sys").stderr)
            raise RuntimeError(REFERENCE + " test-private-key")
        status, summary, _, runner, _ = self.execute(Mock(side_effect=fail))
        self.assertEqual(status, 1)
        self.assertTrue(summary["generation_runner_invoked"])
        self.assertEqual(summary["status"], "not_completed")
        self.assertEqual(summary["reason"], "preflight_or_generation_failed")
        runner.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
