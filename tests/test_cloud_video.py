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

from video_service import VideoError


spec = importlib.util.spec_from_file_location("cloud_video_runner",
    Path(__file__).resolve().parents[1] / "scripts" / "run_video_generation.py")
cloud = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud)
JOB_ID = "video-12345678123412341234123456789abc"
REMOTE_ID = "provider-task-123456"
MODEL = "bytedance/seedance-2.0"


class FakeCloudVideoService:
    def __init__(self, status="completed", refresh_statuses=None):
        self.directory = None
        self.calls = []
        self.initial_status = status
        self.refresh_statuses = list(refresh_statuses or [])
        self.refresh_error = None
        self.download_error = None
        self.job = {"id": JOB_ID, "remote_id": REMOTE_ID, "model": MODEL,
            "status": status, "created_at": "2026-11-15T15:00:00Z", "cost": 0.1,
            "prompt": "private-prompt-marker", "first_frame": "https://media.example/private-image.png",
            "_provider_response": {"secret": "test-private-api-key"}}

    def bind(self, directory):
        self.directory = Path(directory)
        return self

    def submit(self, body):
        self.calls.append(("submit", copy.deepcopy(body)))
        return copy.deepcopy(self.job)

    def recover(self, remote_id, model):
        self.calls.append(("recover", remote_id, model))
        self.job["remote_id"] = remote_id
        return copy.deepcopy(self.job)

    def refresh(self, ident):
        self.calls.append(("refresh", ident))
        if self.refresh_error:
            raise self.refresh_error
        if self.refresh_statuses:
            self.job["status"] = self.refresh_statuses.pop(0)
        return copy.deepcopy(self.job)

    def download(self, ident):
        self.calls.append(("download", ident))
        if self.download_error:
            raise self.download_error
        directory = self.directory / "generated"; directory.mkdir(exist_ok=True)
        (directory / (JOB_ID + ".mp4")).write_bytes(b"fake-mp4-for-runner-only")
        self.job["result_url"] = "/generated/" + JOB_ID + ".mp4"
        return copy.deepcopy(self.job)


class CloudVideoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.output = Path(self.tmp.name) / "generation-output"

    def tearDown(self):
        self.tmp.cleanup()

    def run_cloud(self, settings=None, service=None, attempt="1", clock=None, key="test-private-api-key"):
        service = service or FakeCloudVideoService()
        inputs = {"allow_paid": True, "mode": "text", "model": MODEL,
            "prompt": "private-prompt-marker", "duration": "5", "resolution": "720p"}
        inputs.update(settings or {})
        environment = {"GENERATION_INPUTS": json.dumps(inputs), "VIDEO_OUTPUT_DIR": str(self.output),
            "GITHUB_RUN_ATTEMPT": attempt, "GITHUB_RUN_ID": "123456", "OPENROUTER_API_KEY": key}
        stdout, stderr = io.StringIO(), io.StringIO()
        clock_options = {"side_effect": clock} if clock is not None else {"return_value": 0}
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(cloud, "VideoService", side_effect=service.bind), \
                patch.object(cloud.time, "sleep") as sleep, \
                patch.object(cloud.time, "monotonic", **clock_options), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            status = cloud.main()
        summary = json.loads((self.output / "generation-summary.json").read_text())
        return status, summary, stdout.getvalue() + stderr.getvalue(), service, sleep

    def test_disabled_paid_toggle_and_rerun_never_submit_a_new_job(self):
        for settings, attempt in (({"allow_paid": False}, "1"), ({"allow_paid": "true"}, "1"),
                                  ({"allow_paid": True}, "2")):
            with self.subTest(settings=settings, attempt=attempt):
                status, summary, logs, service, _ = self.run_cloud(settings, attempt=attempt)
                self.assertEqual(status, 1)
                self.assertEqual(summary["status"], "not_completed")
                self.assertFalse(service.calls)
                self.assertNotIn("test-private-api-key", logs)
                self.assertFalse((self.output / "generated").exists())

    def test_missing_key_does_not_submit_and_no_credentials_are_written(self):
        status, summary, logs, service, _ = self.run_cloud(key="")
        self.assertEqual(status, 1)
        self.assertFalse(service.calls)
        self.assertEqual(summary["status"], "not_completed")
        self.assertNotIn("private-prompt-marker", logs)
        self.assertFalse((self.output / "video-jobs.json").exists())

    def test_completed_generation_downloads_only_video_and_safe_summary(self):
        status, summary, logs, service, sleep = self.run_cloud()
        self.assertEqual(status, 0)
        self.assertEqual([call[0] for call in service.calls], ["submit", "download"])
        submitted = service.calls[0][1]
        self.assertEqual(submitted["idempotency_key"], "github-123456")
        self.assertEqual(submitted["duration"], 5)
        self.assertTrue(submitted["consent"])
        self.assertEqual(submitted["aspect_ratio"], "9:16")
        self.assertFalse(submitted["generate_audio"])
        self.assertEqual(summary["remote_id"], REMOTE_ID)
        self.assertEqual(summary["status"], "completed")
        self.assertFalse(sleep.called)
        artifacts = list(self.output.rglob("*"))
        self.assertEqual({path.name for path in artifacts if path.is_file()},
            {"generation-summary.json", JOB_ID + ".mp4"})
        serialized = json.dumps(summary) + logs
        for private in ("test-private-api-key", "private-prompt-marker", "https://media.example", "_provider_response"):
            self.assertNotIn(private, serialized)

    def test_resume_on_rerun_queries_existing_task_without_paid_submission(self):
        service = FakeCloudVideoService("pending", ["completed"])
        status, summary, _, service, sleep = self.run_cloud(
            {"resume_job_id": REMOTE_ID, "allow_paid": False}, service, attempt="2", clock=[0, 0])
        self.assertEqual(status, 0)
        self.assertEqual([call[0] for call in service.calls], ["recover", "refresh", "download"])
        self.assertEqual(service.calls[0], ("recover", REMOTE_ID, MODEL))
        self.assertEqual(summary["remote_id"], REMOTE_ID)
        sleep.assert_called_once_with(30)

    def test_polling_deadline_preserves_task_id_without_download_or_resubmission(self):
        status, summary, logs, service, sleep = self.run_cloud(service=FakeCloudVideoService("pending"), clock=[0, 0, 1501])
        self.assertEqual(status, 1)
        self.assertEqual([call[0] for call in service.calls], ["submit", "refresh"])
        self.assertEqual(summary["remote_id"], REMOTE_ID)
        self.assertEqual(summary["status"], "pending")
        self.assertNotIn("private-prompt-marker", logs)
        sleep.assert_called_once_with(30)

    def test_frames_and_references_build_distinct_explicit_requests(self):
        first = "https://media.example/character.png"
        last = "https://media.example/changed.png"
        reference = "https://media.example/motion.mp4"
        status, _, _, service, _ = self.run_cloud({"mode": "frames", "first_frame_url": first, "last_frame_url": last})
        self.assertEqual(status, 0)
        request = service.calls[0][1]
        self.assertEqual(request["first_frame"], first)
        self.assertEqual(request["last_frame"], last)
        self.assertNotIn("references", request)
        status, _, _, service, _ = self.run_cloud({"mode": "references", "first_frame_url": first,
            "reference_video_url": reference})
        self.assertEqual(status, 0)
        request = service.calls[0][1]
        self.assertEqual(request["references"], [{"type": "image_url", "url": first}, {"type": "video_url", "url": reference}])
        self.assertNotIn("first_frame", request)

    def test_conflicting_media_modes_fail_before_any_paid_submission(self):
        for settings in ({"mode": "text", "first_frame_url": "https://media.example/a.png"},
                {"mode": "frames", "first_frame_url": "https://media.example/a.png", "reference_video_url": "https://media.example/a.mp4"},
                {"mode": "references", "last_frame_url": "https://media.example/b.png"}):
            with self.subTest(settings=settings):
                status, _, logs, service, _ = self.run_cloud(settings)
                self.assertEqual(status, 1)
                self.assertFalse(service.calls)
                self.assertNotIn("https://media.example", logs)

    def test_failed_provider_job_is_not_downloaded(self):
        status, summary, _, service, _ = self.run_cloud(service=FakeCloudVideoService("failed"))
        self.assertEqual(status, 1)
        self.assertEqual(summary["status"], "failed")
        self.assertEqual([call[0] for call in service.calls], ["submit"])

    def test_refresh_or_download_error_retains_recovery_ids_in_safe_summary(self):
        for phase in ("refresh", "download"):
            service = FakeCloudVideoService("pending" if phase == "refresh" else "completed")
            setattr(service, phase + "_error", VideoError("视频服务暂不可用，请查询已有任务。", 502))
            with self.subTest(phase=phase):
                status, summary, logs, _, _ = self.run_cloud(service=service, clock=[0, 0])
                self.assertEqual(status, 1)
                self.assertEqual(summary["id"], JOB_ID)
                self.assertEqual(summary["remote_id"], REMOTE_ID)
                self.assertEqual(summary["status"], "not_completed")
                self.assertEqual(summary["last_known_status"], "pending" if phase == "refresh" else "completed")
                for private in ("test-private-api-key", "private-prompt-marker", "https://media.example"):
                    self.assertNotIn(private, json.dumps(summary) + logs)


if __name__ == "__main__":
    unittest.main()
