from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError


spec = importlib.util.spec_from_file_location("clay_video_generation",
    Path(__file__).resolve().parents[1] / "scripts" / "run_clay_video_generation.py")
clay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(clay)
MP4 = (24).to_bytes(4, "big") + b"ftypisom\x00\x00\x00\x00isommp42" + (8).to_bytes(4, "big") + b"moov" + (12).to_bytes(4, "big") + b"mdatdata"
PNG = b"\x89PNG\r\n\x1a\nKAI-test-image"
REFERENCE = "https://raw.githubusercontent.com/yjr927/idore_tools/" + "a" * 40 + "/reference-assets/clay-motion-test.mp4"
HASH = hashlib.sha256(MP4).hexdigest()
KEY = "test-private-openrouter-key"
REMOTE = "gen-clay-test-123"


class Response(io.BytesIO):
    def __init__(self, url, raw, status=200, headers=None):
        super().__init__(raw)
        self.url, self.status = url, status
        self.headers = headers or {"Content-Type": "application/json", "Content-Length": str(len(raw))}

    def geturl(self):
        return self.url


class ClayVideoGenerationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.output = Path(self.tmp.name) / "output"
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def execute(self, overrides=None, submit=None, refresh=None, download_error=False, keep=False):
        env = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": clay.REPOSITORY,
            "GITHUB_RUN_ATTEMPT": "1", "GITHUB_RUN_ID": "123456789", "OPENROUTER_API_KEY": KEY,
            "CLAY_ALLOW_PAID": "true", "CLAY_REVIEWED": "true", "CLAY_REFERENCE_URL": REFERENCE,
            "CLAY_REFERENCE_SHA256": HASH, "VIDEO_OUTPUT_DIR": str(self.output)}
        env.update(overrides or {})
        stdout, stderr = io.StringIO(), io.StringIO()
        clock = {"now": 1_800_000_000.0}
        def advance(seconds):
            clock["now"] += seconds

        def open_request(request, **kwargs):
            url, method = request.full_url, request.get_method()
            self.calls.append((method, url, request.data, dict(request.headers)))
            if url in (REFERENCE, clay.IMAGE_URL):
                self.assertIsNone(request.get_header("Authorization"))
                raw, mime = (MP4, "video/mp4") if url == REFERENCE else (PNG, "image/png")
                return Response(url, raw, headers={"Content-Type": mime, "Content-Length": str(len(raw))})
            if url.endswith("/videos/models"):
                self.assertIsNone(request.get_header("Authorization"))
                return Response(url, json.dumps({"data": [{"id": clay.MODEL,
                    "supported_durations": [6], "supported_resolutions": ["720p"],
                    "supported_aspect_ratios": ["9:16"], "generate_audio": False}]}).encode())
            self.assertEqual(request.get_header("Authorization"), "Bearer " + KEY)
            if method == "POST":
                if isinstance(submit, Exception):
                    raise submit
                if callable(submit):
                    return submit(request)
                result = submit or {"id": REMOTE, "status": "completed", "usage": {"cost": .123,
                    "input_tokens": 12, "secret": KEY, "source": REFERENCE}}
                return Response(url, json.dumps(result).encode())
            if url.endswith("/content"):
                if download_error:
                    raise TimeoutError("provider-secret " + KEY + REFERENCE)
                return Response(url, MP4, headers={"Content-Type": "video/mp4"})
            if isinstance(refresh, Exception):
                raise refresh
            return Response(url, json.dumps(refresh or {"id": REMOTE, "status": "completed"}).encode())

        opener = Mock(); opener.open.side_effect = open_request
        with patch.dict(os.environ, env, clear=True), patch.object(clay, "build_opener", return_value=opener), \
                patch.object(clay, "probe_clay", return_value={"duration": 6, "width": 576, "height": 1024, "audio": False}), \
                patch.object(clay.time, "sleep", side_effect=advance) as sleep, \
                patch.object(clay.time, "time", side_effect=lambda: clock["now"]), \
                patch.object(clay.time, "monotonic", side_effect=lambda: clock["now"]), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            result = clay.main()
        summary = json.loads((self.output / "clay-generation-summary.json").read_text())
        serialized = stdout.getvalue() + stderr.getvalue()
        for file in self.output.rglob("*"):
            if file.is_file() and file.suffix != ".mp4":
                serialized += file.read_text()
        for private in (REFERENCE, clay.IMAGE_URL, KEY, clay.PROMPT, "provider-secret"):
            self.assertNotIn(private, serialized)
        return result, summary, sleep

    def posts(self):
        return [call for call in self.calls if call[0] == "POST"]

    def test_exact_character_and_motion_refs_once_and_only_safe_results_persist(self):
        status, summary, sleep = self.execute()
        self.assertEqual(status, 0)
        self.assertEqual(len(self.posts()), 1)
        payload = json.loads(self.posts()[0][2])
        self.assertEqual(payload["model"], "bytedance/seedance-2.5")
        self.assertEqual(payload["duration"], 6)
        self.assertEqual(payload["resolution"], "720p")
        self.assertEqual(payload["aspect_ratio"], "9:16")
        self.assertFalse(payload["generate_audio"])
        self.assertEqual(payload["provider"], {"only": ["seed", "byteplus"]})
        self.assertEqual(payload["input_references"], [
            {"type": "image_url", "image_url": {"url": clay.IMAGE_URL}},
            {"type": "video_url", "video_url": {"url": REFERENCE}}])
        self.assertEqual(payload["prompt"], clay.PROMPT)
        self.assertNotIn("frame_images", payload)
        self.assertEqual(summary["remote_id"], REMOTE)
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["usage"], {"cost": .123, "input_tokens": 12})
        self.assertTrue(summary["paid_post_attempted"])
        self.assertEqual(summary["clay_reference"]["sha256"], HASH)
        self.assertEqual((self.output / summary["result_file"]).read_bytes(), MP4)
        self.assertFalse(any(p.name == "video-jobs.json" for p in self.output.rglob("*")))
        sleep.assert_not_called()

    def test_all_authorization_and_source_guards_stop_before_any_network(self):
        variants = [{"GITHUB_ACTIONS": "false"}, {"GITHUB_REPOSITORY": "other/repo"},
            {"GITHUB_RUN_ATTEMPT": "2"}, {"GITHUB_RUN_ATTEMPT": ""}, {"GITHUB_RUN_ID": "bad/id"},
            {"CLAY_ALLOW_PAID": "false"}, {"CLAY_REVIEWED": "false"}, {"OPENROUTER_API_KEY": ""},
            {"CLAY_REFERENCE_SHA256": "bad"}, {"CLAY_REFERENCE_URL": "https://example.com/clay.mp4"}]
        for overrides in variants:
            with self.subTest(overrides=overrides):
                self.calls.clear()
                status, summary, _ = self.execute(overrides)
                self.assertEqual(status, 1)
                self.assertEqual(self.calls, [])
                self.assertFalse(summary["paid_post_attempted"])

    def test_only_exact_immutable_repository_clay_namespace_is_accepted(self):
        invalid = [REFERENCE.replace("a" * 40, "main"), REFERENCE.replace("idore_tools", "other"),
            REFERENCE.replace("clay-motion-test", "real-person"), REFERENCE + "?token=secret",
            REFERENCE + "#fragment", REFERENCE.replace("https://", "http://"),
            REFERENCE.replace("raw.githubusercontent.com", "raw.githubusercontent.com.evil.example"),
            REFERENCE.replace("reference-assets", "private-assets"), REFERENCE.replace(".mp4", ".webm")]
        self.assertEqual(clay.approved_clay_url(REFERENCE), REFERENCE)
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(clay.Stopped):
                clay.approved_clay_url(url)

    def test_wrong_download_hash_blocks_generation(self):
        status, summary, _ = self.execute({"CLAY_REFERENCE_SHA256": "0" * 64})
        self.assertEqual(status, 1)
        self.assertEqual(summary["reason"], "clay_reference_hash_mismatch")
        self.assertFalse(self.posts())

    def test_known_video_and_image_rejections_are_distinct_and_never_retried(self):
        for kind in ("Video", "Image"):
            with self.subTest(kind=kind):
                if self.output.exists():
                    __import__("shutil").rmtree(self.output)
                self.calls.clear()
                code = f"Input{kind}SensitiveContentDetected.PrivacyInformation"
                raw = json.dumps({"error": {"message": "Provider rejected input", "metadata": {"raw": json.dumps(
                    {"error": {"code": code, "message": "provider-secret " + KEY + REFERENCE}})}}}).encode()
                error = HTTPError("https://openrouter.ai/api/v1/videos", 400, "bad", {}, io.BytesIO(raw))
                status, summary, _ = self.execute(submit=error)
                self.assertEqual(status, 1)
                self.assertEqual(summary["reason"], "input_" + kind.lower() + "_privacy_rejected")
                self.assertEqual(summary["provider_code"], code)
                self.assertEqual(summary["provider_http_status"], 400)
                self.assertEqual(summary["status"], "failed")
                self.assertEqual(len(self.posts()), 1)
                self.assertFalse(list(self.output.glob("*.mp4")))

    def test_ambiguous_submission_is_durable_and_second_invocation_does_not_post(self):
        status, summary, _ = self.execute(submit=TimeoutError("provider-secret " + KEY + REFERENCE))
        self.assertEqual(status, 1)
        self.assertEqual(summary["status"], "submission_uncertain")
        self.assertEqual(summary["reason"], "submission_uncertain")
        self.assertEqual(len(self.posts()), 1)
        first_id = summary["id"]
        status, summary, _ = self.execute()
        self.assertEqual(status, 1)
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(summary["id"], first_id)
        self.assertEqual(summary["status"], "submission_uncertain")
        self.assertEqual(summary["reason"], "prior_submission_claim_exists_do_not_repeat")

    def test_accepted_remote_id_survives_poll_failure_without_second_paid_request(self):
        status, summary, sleep = self.execute(submit={"id": REMOTE, "status": "pending"},
            refresh=TimeoutError("provider-secret " + KEY + REFERENCE))
        self.assertEqual(status, 1)
        self.assertEqual(summary["remote_id"], REMOTE)
        self.assertEqual(summary["status"], "pending")
        self.assertEqual(len(self.posts()), 1)
        sleep.assert_called_once_with(30)

    def test_completed_remote_id_survives_content_download_failure(self):
        status, summary, _ = self.execute(download_error=True)
        self.assertEqual(status, 1)
        self.assertEqual(summary["remote_id"], REMOTE)
        self.assertEqual(summary["last_known_status"], "completed")
        self.assertEqual(summary["status"], "not_completed")
        self.assertEqual(len(self.posts()), 1)

    def test_http_error_is_read_once_with_a_fixed_cap_at_the_io_boundary(self):
        reads = []
        class TrackedBody(io.BytesIO):
            def read(self, maximum=-1):
                reads.append(maximum)
                return super().read(maximum)
        body = TrackedBody(b"x" * (clay.MAX_ERROR_BYTES + 100))
        error = HTTPError("https://openrouter.ai/api/v1/videos", 400, "bad", {}, body)
        opener = Mock(); opener.open.side_effect = error
        request = clay.Request("https://openrouter.ai/api/v1/videos", data=b"{}", method="POST")
        transport = clay.ObservedTransport()
        with patch.object(clay, "build_opener", return_value=opener):
            status, headers, raw = transport(request, timeout=40, max_bytes=4 * 1024 * 1024)
        self.assertEqual(reads, [clay.MAX_ERROR_BYTES + 1])
        self.assertEqual((status, headers, raw), (400, {}, b""))
        self.assertIsNone(transport.diagnosis)
        self.assertEqual(transport.details, {})
        with self.assertRaises(clay.VideoError):
            transport(request, timeout=40, max_bytes=4 * 1024 * 1024)
        self.assertEqual(opener.open.call_count, 1)

    def test_already_accepted_submission_keeps_recovery_id_when_a_repeat_is_blocked(self):
        status, original, _ = self.execute()
        self.assertEqual(status, 0)
        prior_calls = len(self.calls)
        status, repeated, _ = self.execute()
        self.assertEqual(status, 1)
        self.assertEqual(len(self.calls), prior_calls)
        self.assertEqual(repeated["remote_id"], original["remote_id"])
        self.assertEqual(repeated["cost"], original["cost"])
        self.assertTrue(repeated["paid_post_attempted"])

    def test_oversized_unknown_or_reflected_error_details_are_not_retained(self):
        code = "InputVideoSensitiveContentDetected.PrivacyInformation"
        self.assertIsNone(clay.rejection_diagnosis(b"x" * (clay.MAX_ERROR_BYTES + 1), 400))
        self.assertIsNone(clay.rejection_diagnosis(json.dumps({"prompt": code}).encode(), 400))
        self.assertNotIn("reason", clay.rejection_diagnosis(json.dumps({"error": {"code": code + ".other"}}).encode(), 400))
        self.assertIsNone(clay.rejection_diagnosis(json.dumps({"error": {"code": KEY}}).encode(), 400, KEY))
        raw = json.dumps({"error": {"code": "UnknownProviderError", "param": "input_references.1.video_url",
            "message": "provider-secret " + KEY + REFERENCE}}).encode()
        status, summary, _ = self.execute(submit=HTTPError("https://openrouter.ai/api/v1/videos", 422, "bad", {}, io.BytesIO(raw)))
        self.assertEqual(status, 1)
        self.assertEqual(summary["reason"], "provider_rejected_input")
        self.assertEqual(summary["provider_code"], "UnknownProviderError")
        self.assertEqual(summary["provider_param"], "input_references.1.video_url")

    def test_probe_requires_six_second_silent_vertical_decodable_clip_and_scrubs_child_env(self):
        clip = Path(self.tmp.name) / "clay.mp4"; clip.write_bytes(MP4)
        good = {"streams": [{"codec_type": "video", "width": 576, "height": 1024}], "format": {"duration": "6"}}
        runs = []
        def run(command, **kwargs):
            runs.append(command[0])
            self.assertNotIn("OPENROUTER_API_KEY", kwargs["env"])
            self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
            self.assertNotIn("GENERATION_INPUTS", kwargs["env"])
            return subprocess.CompletedProcess(command, 0, json.dumps(good).encode(), b"")
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": KEY, "OPENAI_API_KEY": KEY, "GENERATION_INPUTS": "private"}), \
                patch.object(clay.subprocess, "run", side_effect=run):
            self.assertEqual(clay.probe_clay(clip)["duration"], 6)
        self.assertEqual(runs, ["ffprobe", "ffmpeg"])
        for bad in ({"streams": good["streams"], "format": {"duration": "7"}},
                {"streams": good["streams"] + [{"codec_type": "audio"}], "format": good["format"]},
                {"streams": [{"codec_type": "video", "width": 1024, "height": 576}], "format": good["format"]}):
            with patch.object(clay.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(bad).encode(), b"")), \
                    self.assertRaises(clay.Stopped):
                clay.probe_clay(clip)


if __name__ == "__main__":
    unittest.main()
