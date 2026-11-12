import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("work_results", Path(__file__).resolve().parents[1] / "scripts" / "import_work_results.py")
work = importlib.util.module_from_spec(spec)
spec.loader.exec_module(work)
STAMP = "2026-11-11T12:00:00+00:00"
URL = "https://www.tiktok.com/@original/video/7699497385185119502"


def example(author, ident):
    return {"url": f"https://www.tiktok.com/@{author}/video/{ident}", "original_watched": True,
        "same_pattern": True, "note": "已观看，相同短动作。"}


def payload():
    return {"schema_version": 1, "run_id": "work-test-1", "observed_at": STAMP, "status": "succeeded",
        "message": "发现一条动作候选，跟拍关系已记录。", "videos": [{"url": URL, "title": "简单动作候选",
        "kind": "hands", "original_watched": True, "motion_summary": "一位创作者重复上半身动作。",
        "checks": {"solo": True, "repetitive": True, "fixed_camera": None}, "recreation_notes": "需要试做手部动作。",
        "published_at": None, "published_text": "2 days ago", "metrics": {"views": 100, "likes": None, "comments": 0, "shares": None},
        "related_videos": [], "music_name": "可见音乐名称"}]}


class WorkResultsTests(unittest.TestCase):
    def test_normalizes_queries_and_derives_motion_checks(self):
        raw = payload(); raw["videos"][0]["url"] += "?tracking=1"
        result = work.merge_work_results({"videos": [], "runs": []}, raw)
        row = result["videos"][0]
        self.assertEqual(row["url"], URL)
        self.assertEqual(row["source_kind"], "chatgpt_work")
        self.assertTrue(row["motion_verified"])
        self.assertFalse(row["trend_verified"])
        self.assertEqual(result["runs"][0]["sources"][0]["url"], URL)

    def test_unwatched_original_or_missing_checks_cannot_verify_motion(self):
        for field, value in (("original_watched", False), ("motion_summary", "")):
            raw = payload(); raw["videos"][0][field] = value
            row = work.merge_work_results({}, raw)["videos"][0]
            self.assertFalse(row["motion_verified"])
        raw = payload(); raw["videos"][0]["checks"]["solo"] = None
        self.assertFalse(work.merge_work_results({}, raw)["videos"][0]["motion_verified"])

    def test_trend_needs_three_distinct_watched_creators_same_pattern(self):
        raw = payload()
        raw["videos"][0]["related_videos"] = [example("second", "7699497385185119503"), example("third", "7699497385185119504")]
        self.assertTrue(work.merge_work_results({}, raw)["videos"][0]["trend_verified"])
        raw["videos"][0]["related_videos"][1]["original_watched"] = False
        self.assertFalse(work.merge_work_results({}, raw)["videos"][0]["trend_verified"])
        raw["videos"][0]["related_videos"][1] = example("SECOND", "7699497385185119504")
        self.assertFalse(work.merge_work_results({}, raw)["videos"][0]["trend_verified"])
        raw["videos"][0]["related_videos"][1] = example("original", "7699497385185119504")
        self.assertFalse(work.merge_work_results({}, raw)["videos"][0]["trend_verified"])

    def test_extra_fields_and_bad_metrics_dates_urls_duplicates_are_rejected(self):
        mutations = [
            lambda p: p.update(token="secret"),
            lambda p: p["videos"][0].update(motion_verified=True),
            lambda p: p["videos"][0]["checks"].update(extra=True),
            lambda p: p["videos"][0]["metrics"].update(views=True),
            lambda p: p["videos"][0]["metrics"].update(likes=float("nan")),
            lambda p: p["videos"][0]["metrics"].update(shares=-1),
            lambda p: p["videos"][0]["metrics"].update(views=10 ** 500),
            lambda p: p.update(observed_at="2026-11-11T12:00:00"),
            lambda p: p["videos"][0].update(published_at="2026-11-12T12:00:00Z"),
            lambda p: p["videos"][0].update(url="https://www.tiktok.com.evil.test/@x/video/7699497385185119502"),
            lambda p: p["videos"].append(copy.deepcopy(p["videos"][0])),
            lambda p: p["videos"][0].update(related_videos=[example("original", "7699497385185119502")]),
        ]
        previous = {"videos": [], "runs": []}
        for mutate in mutations:
            raw = payload(); mutate(raw)
            with self.subTest(mutation=mutate), self.assertRaises(work.WorkResultError):
                work.merge_work_results(previous, raw)
            self.assertEqual(previous, {"videos": [], "runs": []})

    def test_status_semantics_and_bounds(self):
        raw = payload(); raw["status"] = "empty"
        with self.assertRaises(work.WorkResultError): work.merge_work_results({}, raw)
        raw = payload(); raw["videos"] = []
        with self.assertRaises(work.WorkResultError): work.merge_work_results({}, raw)
        raw = payload(); raw["videos"] *= 9
        with self.assertRaises(work.WorkResultError): work.merge_work_results({}, raw)

    def test_idempotency_and_same_run_conflict(self):
        raw = payload(); first = work.merge_work_results({}, raw)
        self.assertEqual(work.merge_work_results(first, raw), first)
        changed = copy.deepcopy(raw); changed["message"] = "changed"
        with self.assertRaises(work.WorkResultError): work.merge_work_results(first, changed)

    def test_existing_favorite_first_seen_analysis_and_history_preserved(self):
        raw = payload(); result = work.merge_work_results({"analysis": {"status": "not_configured"}}, raw)
        row = result["videos"][0]; row["favorite"] = True; row["first_seen_at"] = "2026-11-01T12:00:00+00:00"
        for i in range(35):
            raw["run_id"] = f"work-next-{i}"
            raw["observed_at"] = f"2026-11-11T13:{i:02d}:00+00:00"
            raw["videos"][0]["metrics"]["views"] += 1
            result = work.merge_work_results(result, raw)
        self.assertEqual(len(result["videos"]), 1)
        self.assertEqual(len(result["videos"][0]["observations"]), 30)
        self.assertTrue(result["videos"][0]["favorite"])
        self.assertEqual(result["videos"][0]["first_seen_at"], "2026-11-01T12:00:00+00:00")
        self.assertEqual(result["analysis"], {"status": "not_configured"})
        self.assertEqual(len(result["runs"]), 30)
        original = payload()
        self.assertEqual(work.merge_work_results(result, original), result)

    def test_blocked_result_keeps_candidates_and_last_success(self):
        previous = work.merge_work_results({}, payload())
        raw = payload(); raw.update(run_id="blocked-1", observed_at="2026-11-12T12:00:00Z", status="blocked", videos=[], message="需要人工验证。")
        merged = work.merge_work_results(previous, raw)
        self.assertEqual(merged["videos"], previous["videos"])
        self.assertEqual(merged["last_success_at"], previous["last_success_at"])
        self.assertEqual(merged["runs"][0]["status"], "blocked")

    def test_older_observation_does_not_roll_back_newer_candidate(self):
        previous = work.merge_work_results({}, payload())
        raw = payload(); raw.update(run_id="older", observed_at="2026-11-10T12:00:00Z")
        raw["videos"][0]["title"] = "older title"
        merged = work.merge_work_results(previous, raw)
        self.assertEqual(merged["videos"][0]["title"], previous["videos"][0]["title"])
        self.assertEqual(merged["last_attempt_at"], previous["last_attempt_at"])

    def test_cli_missing_or_invalid_input_preserves_output_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output.json"; source = Path(tmp) / "input.json"
            original = '{"videos": [], "runs": []}\n'
            output.write_text(original)
            self.assertEqual(work.main(["--input", str(source), "--output", str(output)]), 0)
            self.assertEqual(output.read_text(), original)
            invalid = payload(); invalid["api_key"] = "not-persisted"
            source.write_text(json.dumps(invalid))
            self.assertEqual(work.main(["--input", str(source), "--output", str(output)]), 1)
            self.assertEqual(output.read_text(), original)
            source.write_text(json.dumps(payload()))
            self.assertEqual(work.main(["--input", str(source), "--output", str(output)]), 0)
            result = json.loads(output.read_text())
            self.assertEqual(result["videos"][0]["url"], URL)


if __name__ == "__main__":
    unittest.main()
