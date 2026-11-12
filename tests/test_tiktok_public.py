import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("tiktok_public", Path(__file__).resolve().parents[1] / "scripts" / "collect_tiktok_public.py")
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)
URL = "https://www.tiktok.com/@example/video/7699497385185119502"
STAMP = "2026-11-11T12:00:00+00:00"


class TikTokPublicTests(unittest.TestCase):
    def row(self, stamp=STAMP):
        return collector.candidate_from_anchor({"href": URL, "text": "A public dance"}, "hands", "easy dance", "https://www.tiktok.com/search?q=easy+dance", stamp)

    def test_exact_video_url_validation(self):
        self.assertEqual(collector.canonical_video(URL + "?tracking=1")[0], URL)
        for invalid in ("http://127.0.0.1/x", URL.replace("www.tiktok.com", "www.tiktok.com.evil.test"), "https://www.tiktok.com/tag/dance", "https://user:password@www.tiktok.com/@example/video/7699497385185119502", URL.replace(".com/", ".com:bad/")):
            with self.assertRaises(collector.PublicPageError):
                collector.canonical_video(invalid)

    def test_only_visible_anchor_inputs_become_bounded_candidates(self):
        anchors = [{"href": URL}, {"href": URL + "?other=1"}, {"href": "https://example.com/"}, {"href": URL.replace("7699497385185119502", "7699497385185119503")}]
        rows = collector.extract_candidates(anchors, "hands", "dance", "https://www.tiktok.com/search?q=dance", STAMP, 1)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["motion_verified"])
        self.assertFalse(rows[0]["trend_verified"])
        self.assertIsNone(rows[0]["metrics"]["views"])
        self.assertIsNone(rows[0]["published_at"])

    def test_real_login_gates_and_challenges_stop_without_nav_false_positive(self):
        self.assertIsNone(collector.access_blocker("Top Videos Log in For you", "https://www.tiktok.com/search"))
        self.assertTrue(collector.access_blocker("Verify to continue"))
        self.assertTrue(collector.access_blocker("normal text", challenge_visible=True))
        self.assertTrue(collector.access_blocker("Log in", login_dialog_visible=True))
        self.assertTrue(collector.access_blocker("", "https://www.tiktok.com/login"))

    def test_merge_preserves_first_seen_and_favorites(self):
        first = self.row("2026-11-01T00:00:00+00:00")
        first["favorite"] = True
        rows, added = collector.merge_candidates([first], [self.row()])
        self.assertEqual(added, 0)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["favorite"])
        self.assertEqual(rows[0]["first_seen_at"], "2026-11-01T00:00:00+00:00")
        self.assertEqual(rows[0]["last_seen_at"], STAMP)

    def test_blocked_run_retains_previous_videos_without_fabricating_success(self):
        previous = {"videos": [self.row()], "runs": [], "last_success_at": "older"}
        result = collector.build_result(previous, [], [{"status": "blocked", "found": 0}], STAMP, STAMP)
        self.assertEqual(len(result["videos"]), 1)
        self.assertEqual(result["runs"][0]["status"], "blocked")
        self.assertEqual(result["last_success_at"], "older")
        self.assertEqual(result["runs"][0]["found"], 0)

    def test_partial_and_empty_states(self):
        result = collector.build_result({"videos": [], "runs": []}, [self.row()], [{"status": "ok"}, {"status": "failed"}], STAMP, STAMP)
        self.assertEqual(result["runs"][0]["status"], "partial")
        result = collector.build_result({"videos": [], "runs": []}, [], [{"status": "ok"}], STAMP, STAMP)
        self.assertEqual(result["runs"][0]["status"], "empty")

    def test_manual_public_attempt_preserves_work_history_and_import_ids(self):
        historical = {"id": "work-old", "video_snapshots": [{"url": URL, "title": "Original title"}]}
        previous = {"videos": [self.row()], "runs": [historical] + [{"id": str(i)} for i in range(100)],
                    "work_imports": {"work-old": "fingerprint"}}
        result = collector.build_result(previous, [], [{"status": "blocked", "found": 0}], STAMP, STAMP)
        self.assertEqual(len(result["runs"]), 90)
        self.assertEqual(result["runs"][1], historical)
        self.assertEqual(result["work_imports"], previous["work_imports"])
        result["work_imports"]["work-old"] = "changed"
        self.assertEqual(previous["work_imports"]["work-old"], "fingerprint")

    def test_cli_failure_is_saved_without_launching_browser(self):
        async def fail(*args):
            raise RuntimeError("browser unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "data.json"
            with patch.object(collector, "collect", fail):
                self.assertEqual(collector.main(["--output", str(output)]), 0)
            result = json.loads(output.read_text())
            self.assertEqual(result["runs"][0]["status"], "failed")
            self.assertEqual(result["videos"], [])

    def test_diagnostics_directory_rejects_absolute_and_parent_paths(self):
        self.assertEqual(collector.diagnostics_path("artifacts/tiktok"), Path("artifacts/tiktok"))
        for value in ("/tmp/tiktok", "../tiktok", "artifacts/../../tiktok"):
            with self.assertRaises(collector.PublicPageError):
                collector.diagnostics_path(value)

    def test_public_diagnostics_are_bounded_and_do_not_contain_browser_state(self):
        class FakeLocator:
            async def inner_text(self, **kwargs): return "public text " * 2000
            async def count(self): return 0
        class FakePage:
            url = "https://www.tiktok.com/search?q=public"
            async def title(self): return "TikTok" * 100
            def locator(self, selector): return FakeLocator()
            async def screenshot(self, **kwargs): Path(kwargs["path"]).write_bytes(b"fake screenshot")
        report = {"url": FakePage.url, "status": "blocked", "message": "No list"}
        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(collector.save_diagnostics(FakePage(), Path(tmp), 1, report))
            files = sorted(path.name for path in Path(tmp).iterdir())
            self.assertEqual(files, ["search-01.json", "search-01.png"])
            payload = json.loads((Path(tmp) / "search-01.json").read_text())
            self.assertEqual(len(payload["body_text"]), 12000)
            self.assertEqual(len(payload["title"]), 500)
            self.assertEqual(payload["anchor_count"], 0)
            self.assertNotIn("cookies", payload)
            self.assertNotIn("storage_state", payload)

    def test_early_http_block_has_diagnostic_artifacts(self):
        class FakeLocator:
            async def inner_text(self, **kwargs): return "Public access denied"
            async def count(self): return 0
        class FakeResponse:
            status = 403
        class FakePage:
            url = "https://www.tiktok.com/search?q=public"
            async def goto(self, *args, **kwargs): return FakeResponse()
            async def title(self): return "TikTok"
            def locator(self, selector): return FakeLocator()
            async def screenshot(self, **kwargs): Path(kwargs["path"]).write_bytes(b"fake screenshot")
        with tempfile.TemporaryDirectory() as tmp:
            rows, report, stop = asyncio.run(collector.collect_page(FakePage(), "hands", "public", 8, Path(tmp), 3))
            self.assertEqual(rows, [])
            self.assertEqual(report["status"], "blocked")
            self.assertTrue(stop)
            payload = json.loads((Path(tmp) / "search-03.json").read_text())
            self.assertEqual(payload["status"], "blocked")
            self.assertTrue((Path(tmp) / "search-03.png").is_file())

    def test_cli_passes_relative_diagnostic_directory_without_browser(self):
        received = []
        async def fake_collect(queries, limit, diagnostics_dir):
            received.append(diagnostics_dir)
            return [], [{"status": "blocked", "found": 0}]
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "data.json"
            with patch.object(collector, "collect", fake_collect):
                code = collector.main(["--output", str(output), "--diagnostics-dir", "artifacts/tiktok"])
            self.assertEqual(code, 0)
            self.assertEqual(received, [Path("artifacts/tiktok")])
            self.assertEqual(json.loads(output.read_text())["runs"][0]["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
