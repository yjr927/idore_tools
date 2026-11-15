from contextlib import redirect_stderr, redirect_stdout
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("cloud_creative_runner",
    Path(__file__).resolve().parents[1] / "scripts" / "run_creative_generation.py")
cloud = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud)
CREATIVE_ID = "creative-" + "a" * 32
VIDEO_ID = "video-" + "b" * 32
REMOTE_ID = "provider-task-12345"
IMAGE_MODEL = "gpt-image-test"
TEXT_MODEL = "gpt-text-test"
VIDEO_MODEL = "bytedance/seedance-2.0"
REFERENCE = "https://public.example/character-reference.png"
PNG = b"\x89PNG\r\n\x1a\nrunner-image-fixture"


class FakeCreative:
    def __init__(self, timeline):
        self.directory = None
        self.timeline = timeline
        self.calls = []
        self.image_status = "completed"
        self.image_error = self.draft_error = None
        self.oversized = False
        self.available_text = True

    def bind(self, directory):
        self.directory = Path(directory)
        return self

    def models(self):
        self.timeline.append("creative-models")
        return {"image_models": [{"id": IMAGE_MODEL}],
                "text_models": [{"id": TEXT_MODEL}] if self.available_text else []}

    def generate_images(self, body):
        self.timeline.append("images")
        self.calls.append(("images", copy.deepcopy(body)))
        if self.image_error:
            raise self.image_error
        generated = self.directory / "generated"; generated.mkdir(exist_ok=True)
        name = "style-" + "a" * 32
        (generated / (name + "-before.png")).write_bytes(PNG + b"x" * cloud.MAX_FRAME_BYTES if self.oversized else PNG)
        if self.image_status == "completed":
            (generated / (name + "-after.png")).write_bytes(PNG)
        return {"id": CREATIVE_ID, "kind": "images", "status": self.image_status,
            "before_url": "/generated/" + name + "-before.png", "after_url": "/generated/" + name + "-after.png",
            "prompt": "private-input-prompt", "reference_image": REFERENCE,
            "raw_response": {"key": "private-openai-key"}}

    def generate_draft(self, body):
        self.timeline.append("draft")
        self.calls.append(("draft", copy.deepcopy(body)))
        if self.draft_error:
            raise self.draft_error
        return {"id": "creative-" + "c" * 32, "kind": "draft", "status": "completed",
            "description": "private-input-prompt", "raw_response": {"key": "private-openai-key"},
            "draft": {"captions": ["New look, same energy.", "Wait for the switch.", "Which style wins?"],
                "hashtags": [{"tag": "#VirtualIdol", "reason": "Describes the character content.", "category": "content", "source_url": None, "observed_at": None},
                    {"tag": "#idore", "reason": "The supplied character brand.", "category": "brand", "source_url": None, "observed_at": None},
                    {"tag": "#StyleSwitch", "reason": "Describes the outfit transition.", "category": "content", "source_url": None, "observed_at": None}],
                "trend_note": "Relevant content tags; no live TikTok ranking was verified."}, "sources": []}


class FakeVideo:
    def __init__(self, timeline):
        self.timeline = timeline
        self.directory = None
        self.calls = []
        self.supported = True
        self.status = "completed"
        self.refresh_error = self.download_error = None
        self.write_file = True

    def bind(self, directory):
        self.directory = Path(directory)
        return self

    def connect(self, body):
        self.timeline.append("video-key-check")
        return {"connected": True}

    def models(self):
        self.timeline.append("video-models")
        return {"models": [{"id": VIDEO_MODEL, "supported_durations": [5], "supported_resolutions": ["720p"],
            "supported_aspect_ratios": ["9:16"], "supported_frame_images": ["first_frame", "last_frame"]}] if self.supported else []}

    def job(self):
        return {"id": VIDEO_ID, "remote_id": REMOTE_ID, "model": VIDEO_MODEL, "status": self.status,
                "prompt": "private-input-prompt", "reference": REFERENCE, "key": "private-openrouter-key"}

    def submit(self, body):
        self.timeline.append("video-submit")
        self.calls.append(("submit", copy.deepcopy(body)))
        return self.job()

    def refresh(self, ident):
        self.timeline.append("video-refresh")
        self.calls.append(("refresh", ident))
        if self.refresh_error:
            raise self.refresh_error
        return self.job()

    def download(self, ident):
        self.timeline.append("video-download")
        self.calls.append(("download", ident))
        if self.download_error:
            raise self.download_error
        if self.write_file:
            (self.directory / "generated" / (VIDEO_ID + ".mp4")).write_bytes(b"fake-mp4-runner-fixture")
        return {**self.job(), "result_url": "/generated/" + VIDEO_ID + ".mp4"}


class CloudCreativeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.output = Path(self.tmp.name) / "generation-output"
        self.timeline = []
        self.creative, self.video = FakeCreative(self.timeline), FakeVideo(self.timeline)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cloud(self, settings=None, environment=None, clock=None):
        inputs = {"task": "styles", "model": IMAGE_MODEL, "character": "idore", "description": "A character changes outfit.",
            "before_prompt": "private-input-prompt before", "after_prompt": "private-input-prompt after",
            "reference_image_url": REFERENCE, "search_trends": False, "allow_paid": True, "video_model": VIDEO_MODEL}
        inputs.update(settings or {})
        env = {"CREATIVE_INPUTS": json.dumps(inputs), "CREATIVE_OUTPUT_DIR": str(self.output),
            "OPENAI_API_KEY": "private-openai-key", "OPENAI_IMAGE_MODEL": IMAGE_MODEL, "OPENAI_MODEL": TEXT_MODEL,
            "GITHUB_RUN_ATTEMPT": "1", "GITHUB_RUN_ID": "12345"}
        env.update(environment or {})
        stdout, stderr = io.StringIO(), io.StringIO()
        clock_args = {"side_effect": clock} if clock is not None else {"return_value": 0}
        with patch.dict(os.environ, env, clear=True), \
                patch.object(cloud, "CreativeService", side_effect=self.creative.bind) as creative_constructor, \
                patch.object(cloud, "VideoService", side_effect=self.video.bind) as video_constructor, \
                patch.object(cloud.time, "sleep") as sleep, patch.object(cloud.time, "monotonic", **clock_args), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            status = cloud.main()
        summary = json.loads((self.output / "creative-summary.json").read_text())
        return status, summary, stdout.getvalue() + stderr.getvalue(), creative_constructor, video_constructor, sleep

    def assert_safe(self, summary, logs):
        for private in ("private-openai-key", "private-openrouter-key", "private-input-prompt", REFERENCE, "raw_response"):
            self.assertNotIn(private, json.dumps(summary) + logs)

    def test_explicit_paid_consent_and_rerun_guard_stop_every_paid_step(self):
        for settings, env in (({"allow_paid": False}, {}), ({"allow_paid": "true"}, {}),
                              ({"task": "full_video"}, {"GITHUB_RUN_ATTEMPT": "2"})):
            with self.subTest(settings=settings, env=env):
                status, summary, logs, creative, video, _ = self.run_cloud(settings, env)
                self.assertEqual(status, 1); self.assertEqual(summary["status"], "not_completed")
                creative.assert_not_called(); video.assert_not_called()
                self.assertFalse(self.creative.calls); self.assertFalse(self.video.calls)
                self.assert_safe(summary, logs)

    def test_styles_use_only_openai_and_export_two_images_without_private_inputs(self):
        status, summary, logs, _, video, _ = self.run_cloud()
        self.assertEqual(status, 0); self.assertEqual(summary["status"], "completed")
        self.assertEqual(len(summary["image_files"]), 2)
        video.assert_not_called()
        self.assertEqual(self.creative.calls[0][0], "images")
        body = self.creative.calls[0][1]
        self.assertEqual(body["reference_image"], REFERENCE)
        self.assertEqual(body["model"], IMAGE_MODEL)
        self.assertTrue(body["consent"])
        self.assertIn("Character: idore", body["before_prompt"])
        self.assert_safe(summary, logs)
        self.assertFalse((self.output / "publication-draft.json").exists())

    def test_draft_uses_configured_text_model_and_exports_intended_english_copy_only(self):
        status, summary, logs, _, video, _ = self.run_cloud({"task": "draft", "model": "", "reference_image_url": "", "search_trends": True})
        self.assertEqual(status, 0); video.assert_not_called()
        body = self.creative.calls[0][1]
        self.assertEqual(body["model"], TEXT_MODEL); self.assertTrue(body["search_trends"])
        self.assertNotIn("frames", body)
        draft = json.loads((self.output / "publication-draft.json").read_text())
        self.assertEqual(draft["language"], "en"); self.assertEqual(draft["target_market"], "US")
        self.assertEqual(len(draft["captions"]), 3)
        self.assertEqual(draft["sources"], [])
        self.assertNotIn("description", draft); self.assertNotIn("raw_response", draft)
        self.assert_safe(summary, logs)

    def test_missing_key_model_and_draft_image_input_fail_before_service_calls(self):
        cases = [({}, {"OPENAI_API_KEY": ""}), ({"model": ""}, {"OPENAI_IMAGE_MODEL": ""}),
                 ({"task": "draft", "reference_image_url": REFERENCE}, {})]
        for settings, env in cases:
            with self.subTest(settings=settings, env=env):
                status, summary, logs, creative, video, _ = self.run_cloud(settings, env)
                self.assertEqual(status, 1); creative.assert_not_called(); video.assert_not_called()
                self.assert_safe(summary, logs)

    def test_cloud_reference_must_be_unsigned_public_https_and_is_not_fetched_by_runner(self):
        for url in ("http://public.example/a.png", "https://user:secret@public.example/a.png",
                    REFERENCE + "?signature=secret", "https://127.0.0.1/a.png", "https://10.0.0.1/a.png",
                    "https://localhost/a.png", "data:image/png;base64,AAAA"):
            with self.subTest(url=url):
                status, _, logs, creative, video, _ = self.run_cloud({"reference_image_url": url})
                self.assertEqual(status, 1); creative.assert_not_called(); video.assert_not_called()
                self.assertNotIn(url, logs)

    def test_raw_provider_failure_is_sanitized_and_never_automatically_retried(self):
        self.creative.image_error = RuntimeError("private-openai-key " + REFERENCE + " private-input-prompt")
        status, summary, logs, _, _, _ = self.run_cloud()
        self.assertEqual(status, 1); self.assertEqual(len(self.creative.calls), 1)
        self.assert_safe(summary, logs)
        self.assertNotIn("Traceback", logs)

    def test_partial_style_pair_preserves_existing_image_and_reports_failure(self):
        self.creative.image_status = "partial"
        status, summary, logs, _, _, _ = self.run_cloud()
        self.assertEqual(status, 1); self.assertEqual(summary["status"], "partial")
        self.assertEqual(len(summary["image_files"]), 1)
        self.assertEqual(len(self.creative.calls), 1)
        self.assert_safe(summary, logs)

    def test_full_pipeline_validates_both_providers_then_images_video_download_and_copy(self):
        status, summary, logs, _, _, sleep = self.run_cloud({"task": "full_video"}, {"OPENROUTER_API_KEY": "private-openrouter-key"})
        self.assertEqual(status, 0); self.assertEqual(summary["status"], "completed")
        self.assertEqual(self.timeline, ["creative-models", "video-key-check", "video-models", "images", "video-submit", "video-download", "draft"])
        body = self.video.calls[0][1]
        self.assertEqual(body["duration"], 5); self.assertEqual(body["resolution"], "720p")
        self.assertEqual(body["aspect_ratio"], "9:16"); self.assertFalse(body["generate_audio"])
        self.assertEqual(body["model"], VIDEO_MODEL); self.assertEqual(body["mode"], "frames")
        self.assertTrue(body["first_frame"].startswith("data:image/png;base64,"))
        draft_body = self.creative.calls[-1][1]
        self.assertEqual(draft_body["model"], TEXT_MODEL); self.assertEqual(len(draft_body["frames"]), 2)
        self.assertIn("not frames extracted from the finished movie", draft_body["description"])
        draft = json.loads((self.output / "publication-draft.json").read_text())
        self.assertEqual(draft["frame_basis"], "generated_style_images_only")
        self.assertEqual(summary["video"]["remote_id"], REMOTE_ID)
        self.assertTrue((self.output / "generated" / (VIDEO_ID + ".mp4")).is_file())
        sleep.assert_not_called(); self.assert_safe(summary, logs)

    def test_full_pipeline_missing_other_key_or_text_model_stops_before_paid_images(self):
        for env in ({"OPENROUTER_API_KEY": ""}, {"OPENROUTER_API_KEY": "private-openrouter-key", "OPENAI_MODEL": ""}):
            with self.subTest(env=env):
                status, _, _, creative, video, _ = self.run_cloud({"task": "full_video"}, env)
                self.assertEqual(status, 1); creative.assert_not_called(); video.assert_not_called()
        self.creative.available_text = False
        status, _, _, _, _, _ = self.run_cloud({"task": "full_video"}, {"OPENROUTER_API_KEY": "private-openrouter-key"})
        self.assertEqual(status, 1); self.assertFalse(self.creative.calls); self.assertFalse(self.video.calls)
        self.creative.available_text = True; self.video.supported = False
        status, _, _, _, _, _ = self.run_cloud({"task": "full_video"}, {"OPENROUTER_API_KEY": "private-openrouter-key"})
        self.assertEqual(status, 1); self.assertFalse(self.creative.calls); self.assertFalse(self.video.calls)

    def test_full_pipeline_timeout_keeps_images_and_remote_id_without_caption_or_retry(self):
        self.video.status = "pending"
        status, summary, logs, _, _, sleep = self.run_cloud({"task": "full_video"},
            {"OPENROUTER_API_KEY": "private-openrouter-key"}, clock=[0, 0, 1501])
        self.assertEqual(status, 1); self.assertEqual(summary["stage"], "video")
        self.assertEqual(summary["video"]["remote_id"], REMOTE_ID)
        self.assertEqual(summary["video"]["status"], "pending")
        self.assertEqual(len(summary["image_files"]), 2)
        self.assertEqual([call[0] for call in self.creative.calls], ["images"])
        self.assertEqual([call[0] for call in self.video.calls], ["submit", "refresh"])
        sleep.assert_called_once_with(30); self.assert_safe(summary, logs)

    def test_full_pipeline_refresh_failure_preserves_recovery_id_and_omits_raw_error(self):
        self.video.status = "pending"
        self.video.refresh_error = RuntimeError("private-openrouter-key " + REFERENCE)
        status, summary, logs, _, _, _ = self.run_cloud({"task": "full_video"},
            {"OPENROUTER_API_KEY": "private-openrouter-key"}, clock=[0, 0])
        self.assertEqual(status, 1); self.assertEqual(summary["video"]["remote_id"], REMOTE_ID)
        self.assertEqual([call[0] for call in self.creative.calls], ["images"])
        self.assert_safe(summary, logs)

    def test_cancelled_and_expired_video_statuses_remain_in_recovery_summary(self):
        for terminal in ("cancelled", "expired"):
            self.video.status = terminal
            with self.subTest(status=terminal):
                status, summary, logs, _, _, sleep = self.run_cloud({"task": "full_video"},
                    {"OPENROUTER_API_KEY": "private-openrouter-key"})
                self.assertEqual(status, 1)
                self.assertEqual(summary["video"]["status"], terminal)
                self.assertEqual(summary["video"]["remote_id"], REMOTE_ID)
                self.assertEqual(summary["stage"], "video")
                self.assertFalse(any(call[0] == "draft" for call in self.creative.calls))
                sleep.assert_not_called()
                self.assert_safe(summary, logs)

    def test_full_pipeline_oversized_image_does_not_submit_video(self):
        self.creative.oversized = True
        status, summary, logs, _, _, _ = self.run_cloud({"task": "full_video"}, {"OPENROUTER_API_KEY": "private-openrouter-key"})
        self.assertEqual(status, 1); self.assertEqual(len(summary["image_files"]), 2)
        self.assertFalse(self.video.calls)
        self.assertEqual([call[0] for call in self.creative.calls], ["images"])
        self.assert_safe(summary, logs)

    def test_full_pipeline_download_or_caption_failure_keeps_paid_results(self):
        self.video.write_file = False
        status, summary, _, _, _, _ = self.run_cloud({"task": "full_video"}, {"OPENROUTER_API_KEY": "private-openrouter-key"})
        self.assertEqual(status, 1); self.assertEqual(summary["video"]["remote_id"], REMOTE_ID)
        self.assertEqual([call[0] for call in self.creative.calls], ["images"])
        self.video.write_file = True
        self.creative.draft_error = RuntimeError("private-openai-key " + REFERENCE)
        status, summary, logs, _, _, _ = self.run_cloud({"task": "full_video"}, {"OPENROUTER_API_KEY": "private-openrouter-key"})
        self.assertEqual(status, 1); self.assertEqual(summary["stage"], "draft")
        self.assertEqual(summary["video"]["status"], "completed")
        self.assertTrue((self.output / "generated" / (VIDEO_ID + ".mp4")).is_file())
        self.assert_safe(summary, logs)

    def test_workflow_is_manual_and_has_ten_inputs_with_only_intended_artifacts(self):
        workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "creative-generation.yml").read_text()
        names = [line for line in workflow.splitlines() if __import__("re").fullmatch(r"      [a-z_]+:", line)]
        self.assertEqual(len(names), 10)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("schedule:", workflow); self.assertNotIn("pull_request:", workflow)
        self.assertIn("secrets.OPENAI_API_KEY", workflow); self.assertIn("secrets.OPENROUTER_API_KEY", workflow)
        artifacts = workflow.split("          path: |", 1)[1].split("          if-no-files-found", 1)[0]
        self.assertNotIn("jobs.json", artifacts); self.assertNotIn(".env", artifacts)
        self.assertIn("publication-draft.json", artifacts); self.assertIn("generated/video-*.mp4", artifacts)


if __name__ == "__main__":
    unittest.main()
