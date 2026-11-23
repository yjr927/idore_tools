import copy
import importlib.util
import hashlib
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


def payload_v2(topic="kpop", ident="7699497385185119502", run_id="work-v2-1", stamp=STAMP):
    raw = payload()
    raw.update(schema_version=2, run_id=run_id, observed_at=stamp)
    raw["videos"][0].update(url=f"https://www.tiktok.com/@original/video/{ident}",
        trend_category=topic, material_group="male", transformation="none")
    return raw


def payload_v3():
    raw = payload_v2()
    record = raw["videos"][0]
    raw["schema_version"] = 3
    record["metrics"].update(views=1_000_000, likes=100_000)
    record["metric_display"] = {"views": "1,000,000", "likes": "100,000"}
    record["related_videos"] = [example("second", "7699497385185119522"), example("third", "7699497385185119533")]
    return raw


def merge_archived_fixture(previous, payload):
    # Reconstruct a pre-v3 archive to exercise historical observations and reservation rules.
    return work._merge_observation(previous, work.validate_payload(payload))


def two_kpop(stamp=STAMP):
    raw = payload_v2(stamp=stamp)
    raw["videos"].append(payload_v2(ident="7699497385185119503", stamp=stamp)["videos"][0])
    return raw


class WorkResultsTests(unittest.TestCase):
    def test_new_v3_requires_both_counts_and_two_watched_other_creators(self):
        good = payload_v3()
        result = work.merge_work_results({}, good)
        row = result["videos"][0]
        self.assertTrue(row["qualified_trend"])
        self.assertEqual(result["runs"][0]["video_snapshots"][0]["metric_display"], row["metric_display"])
        self.assertEqual(result, work.merge_work_results(result, good))
        mutations = [
            lambda v: v["metric_display"].update(views="1M"),
            lambda v: v["metric_display"].update(likes="100K"),
            lambda v: v["metrics"].update(views=None),
            lambda v: v["metrics"].update(likes=99999),
            lambda v: v["metric_display"].update(views="1.3M"),
            lambda v: v.update(original_watched=False),
            lambda v: v.update(motion_summary=" "),
            lambda v: v["related_videos"][1].update(url="https://www.tiktok.com/@SECOND/video/7699497385185119533"),
            lambda v: v["related_videos"][1].update(url="https://www.tiktok.com/@original/video/7699497385185119533"),
            lambda v: v["related_videos"][1].update(note=" "),
            lambda v: v["related_videos"][1].update(same_pattern=False),
        ]
        for mutate in mutations:
            p = copy.deepcopy(good)
            mutate(p["videos"][0])
            with self.subTest(mutate=mutate), self.assertRaises(work.WorkResultError):
                work.merge_work_results({}, p)
        v = good["videos"][0]
        v["metrics"].update(views=1200000, likes=130000)
        v["metric_display"].update(views="1.2M", likes="130K")
        self.assertTrue(work.merge_work_results({}, good)["videos"][0]["qualified_trend"])

    def test_legacy_runs_can_replay_but_new_nonempty_input_cannot_downgrade(self):
        legacy = payload_v2()
        prior = merge_archived_fixture({}, legacy)
        self.assertEqual(work.merge_work_results(prior, legacy), prior)
        with self.assertRaises(work.WorkResultError):
            work.merge_work_results({}, legacy)
        modified = copy.deepcopy(legacy)
        modified["message"] = "changed"
        with self.assertRaises(work.WorkResultError):
            work.merge_work_results(prior, modified)
        empty = dict(legacy, run_id="new-empty", videos=[], status="empty")
        self.assertEqual(work.merge_work_results(prior, empty)["videos"], prior["videos"])

    def test_human_intervention_signal_is_a_bounded_blocked_event(self):
        p = dict(payload_v3(), status="blocked", videos=[], action_required="human_verification")
        self.assertEqual(work.merge_work_results({}, p)["runs"][0]["action_required"], "human_verification")
        for patch in ({"status":"empty"}, {"action_required":"https://challenge.example"}, {"schema_version":2}):
            with self.assertRaises(work.WorkResultError):
                work.merge_work_results({}, dict(p, **patch))

    def test_v1_five_video_legacy_payload_and_fingerprint_stay_compatible(self):
        raw = payload()
        raw["videos"] = [copy.deepcopy(raw["videos"][0]) for _ in range(5)]
        for offset, video in enumerate(raw["videos"]):
            video["url"] = f"https://www.tiktok.com/@original/video/{7699497385185119502 + offset}"
        expected_fingerprint = hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False,
            separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
        result = merge_archived_fixture({}, raw)
        self.assertEqual(len(result["videos"]), 5)
        self.assertEqual(result["work_imports"][raw["run_id"]], expected_fingerprint)
        self.assertEqual(merge_archived_fixture(result, raw), result)
        self.assertNotIn("work_daily_selections", result)
        self.assertTrue(all("material_group" not in video for video in result["videos"]))

    def test_v2_requires_bounded_classifications_and_watched_transformation(self):
        mutations = [
            lambda p: p["videos"][0].pop("trend_category"),
            lambda p: p["videos"][0].pop("material_group"),
            lambda p: p["videos"][0].pop("transformation"),
            lambda p: p["videos"][0].update(trend_category="dance"),
            lambda p: p["videos"][0].update(material_group=True),
            lambda p: p["videos"][0].update(transformation="hand_dance"),
            lambda p: p["videos"][0].update(original_watched=False, transformation="none"),
            lambda p: p["videos"][0].update(gender="male"),
            lambda p: p.update(schema_version=True),
        ]
        for mutate in mutations:
            raw = payload_v2(); mutate(raw)
            with self.subTest(mutation=mutate), self.assertRaises(work.WorkResultError):
                merge_archived_fixture({}, raw)
        raw = payload_v2()
        raw["videos"][0].update(original_watched=False, material_group="unknown", transformation="unknown")
        result = merge_archived_fixture({}, raw)
        self.assertEqual(result["videos"][0]["material_group"], "unknown")
        self.assertEqual(result["videos"][0]["transformation"], "unknown")
        self.assertFalse(result["videos"][0]["motion_verified"])

    def test_v2_batch_accepts_four_distinct_with_two_per_topic(self):
        raw = two_kpop()
        raw["videos"] += [payload_v2("virtual_idol", str(7699497385185119504 + i))["videos"][0] for i in range(2)]
        self.assertEqual(len(merge_archived_fixture({}, raw)["videos"]), 4)
        raw["videos"][2]["trend_category"] = "kpop"
        with self.assertRaises(work.WorkResultError): merge_archived_fixture({}, raw)
        raw["videos"][2]["trend_category"] = "virtual_idol"
        raw["videos"].append(payload_v2(ident="7699497385185119506")["videos"][0])
        with self.assertRaises(work.WorkResultError): merge_archived_fixture({}, raw)

    def test_v2_classifications_are_preserved_in_observations_and_immutable_snapshots(self):
        raw = payload_v2()
        raw["videos"][0].update(material_group="female", transformation="makeup_and_outfit")
        first = merge_archived_fixture({}, raw)
        snapshot = copy.deepcopy(first["runs"][0]["video_snapshots"][0])
        for source in (first["videos"][0], first["videos"][0]["observations"][0], snapshot):
            self.assertEqual(source["trend_category"], "kpop")
            self.assertEqual(source["material_group"], "female")
            self.assertEqual(source["transformation"], "makeup_and_outfit")
        changed = copy.deepcopy(raw)
        changed.update(run_id="work-v2-update", observed_at="2026-11-11T13:00:00Z")
        changed["videos"][0].update(material_group="mixed", transformation="outfit")
        result = merge_archived_fixture(first, changed)
        self.assertEqual(result["videos"][0]["material_group"], "mixed")
        self.assertEqual(result["videos"][0]["observations"][-1]["transformation"], "outfit")
        self.assertEqual(result["runs"][1]["video_snapshots"][0], snapshot)

    def test_v2_same_day_reruns_cannot_add_another_video_in_reserved_topic(self):
        first = merge_archived_fixture({}, two_kpop())
        update = payload_v2(run_id="same-url-recheck")
        update["videos"][0]["metrics"]["views"] = 500
        result = merge_archived_fixture(first, update)
        self.assertEqual(len(result["videos"]), 2)
        self.assertEqual(next(row for row in result["videos"] if row["url"] == URL)["metrics"]["views"], 500)
        self.assertEqual(merge_archived_fixture(result, update), result)
        before = copy.deepcopy(result)
        with self.assertRaises(work.WorkResultError):
            merge_archived_fixture(result, payload_v2(ident="7699497385185119504", run_id="new-kpop"))
        self.assertEqual(result, before)
        result = merge_archived_fixture(result, payload_v2("virtual_idol", "7699497385185119504", "new-idol"))
        result = merge_archived_fixture(result, payload_v2("virtual_idol", "7699497385185119505", "second-idol"))
        self.assertEqual(len(result["videos"]), 4)
        with self.assertRaises(work.WorkResultError):
            merge_archived_fixture(result, payload_v2("virtual_idol", "7699497385185119506", "extra-idol"))

    def test_v2_daily_slots_follow_new_york_midnight_instead_of_utc_midnight(self):
        first = merge_archived_fixture({}, two_kpop(stamp="2026-11-12T00:30:00Z"))
        with self.assertRaises(work.WorkResultError):
            merge_archived_fixture(first, payload_v2(ident="7699497385185119504", run_id="same-ny-day",
                stamp="2026-11-12T04:59:59Z"))
        result = merge_archived_fixture(first, payload_v2(ident="7699497385185119503", run_id="next-ny-day",
            stamp="2026-11-12T05:00:00Z"))
        self.assertEqual(set(result["work_daily_selections"]), {"2026-11-11", "2026-11-12"})

    def test_v2_legacy_and_prior_day_rechecks_reserve_the_current_day(self):
        legacy = merge_archived_fixture({}, payload())
        first = merge_archived_fixture(legacy, payload_v2(run_id="classify-legacy"))
        self.assertEqual(next(run for run in first["runs"] if run["run_id"] == "classify-legacy")["added"], 0)
        first = merge_archived_fixture(first, payload_v2(ident="7699497385185119503", run_id="second-after-legacy"))
        with self.assertRaises(work.WorkResultError):
            merge_archived_fixture(first, payload_v2(ident="7699497385185119504", run_id="new-after-legacy"))
        next_day = merge_archived_fixture(first, payload_v2(run_id="next-day-recheck", stamp="2026-11-12T12:00:00Z"))
        next_day = merge_archived_fixture(next_day, payload_v2(ident="7699497385185119503", run_id="second-after-recheck",
            stamp="2026-11-12T13:00:00Z"))
        with self.assertRaises(work.WorkResultError):
            merge_archived_fixture(next_day, payload_v2(ident="7699497385185119504", run_id="new-after-recheck",
                stamp="2026-11-12T13:00:00Z"))

    def test_v2_reclassifying_a_video_does_not_free_its_original_topic_slot(self):
        first = merge_archived_fixture({}, two_kpop())
        changed = merge_archived_fixture(first, payload_v2("virtual_idol", run_id="change-topic"))
        self.assertEqual(set(changed["work_daily_selections"]["2026-11-11"]), {"kpop", "virtual_idol"})
        with self.assertRaises(work.WorkResultError):
            merge_archived_fixture(changed, payload_v2(ident="7699497385185119504", run_id="cannot-free-topic"))

    def test_v2_daily_reservations_recover_from_snapshots_and_survive_run_trimming(self):
        first = merge_archived_fixture({}, two_kpop())
        without_ledger = copy.deepcopy(first)
        del without_ledger["work_daily_selections"]
        with self.assertRaises(work.WorkResultError):
            merge_archived_fixture(without_ledger, payload_v2(ident="7699497385185119504", run_id="snapshot-limit"))
        without_history = copy.deepcopy(first)
        without_history["runs"] = []
        without_history["videos"] = []
        with self.assertRaises(work.WorkResultError):
            merge_archived_fixture(without_history, payload_v2(ident="7699497385185119504", run_id="retained-limit"))
        restored = merge_archived_fixture(without_history, payload_v2(run_id="restore-same-url"))
        self.assertEqual(len(restored["videos"]), 1)

    def test_v2_failed_and_overquota_cli_imports_preserve_prior_state(self):
        first = merge_archived_fixture({}, two_kpop())
        failed = payload_v2(run_id="v2-blocked")
        failed.update(status="blocked", videos=[])
        result = merge_archived_fixture(first, failed)
        self.assertEqual(result["videos"], first["videos"])
        self.assertEqual(result["work_daily_selections"], first["work_daily_selections"])
        self.assertEqual(next(run for run in result["runs"] if run["run_id"] == "v2-blocked")["video_snapshots"], [])
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output.json"; source = Path(tmp) / "input.json"
            original = json.dumps(first)
            output.write_text(original)
            source.write_text(json.dumps(payload_v2(ident="7699497385185119504", run_id="over-quota")))
            self.assertEqual(work.main(["--input", str(source), "--output", str(output)]), 1)
            self.assertEqual(output.read_text(), original)

    def test_v2_old_string_ledger_migrates_without_releasing_reservations(self):
        previous = {"videos": [], "runs": [], "work_daily_selections": {
            "2026-11-11": {"kpop": "tiktok-7699497385185119502"}}}
        original = copy.deepcopy(previous)
        result = merge_archived_fixture(previous, payload_v2(ident="7699497385185119503", run_id="migrate-old-slot"))
        self.assertEqual(result["work_daily_selections"]["2026-11-11"]["kpop"],
                         ["tiktok-7699497385185119502", "tiktok-7699497385185119503"])
        with self.assertRaises(work.WorkResultError):
            merge_archived_fixture(result, payload_v2(ident="7699497385185119504", run_id="old-slot-still-counts"))
        self.assertEqual(previous, original)

    def test_normalizes_queries_and_derives_motion_checks(self):
        raw = payload(); raw["videos"][0]["url"] += "?tracking=1"
        result = merge_archived_fixture({"videos": [], "runs": []}, raw)
        row = result["videos"][0]
        self.assertEqual(row["url"], URL)
        self.assertEqual(row["source_kind"], "chatgpt_work")
        self.assertTrue(row["motion_verified"])
        self.assertFalse(row["trend_verified"])
        self.assertEqual(result["runs"][0]["sources"][0]["url"], URL)

    def test_unwatched_original_or_missing_checks_cannot_verify_motion(self):
        for field, value in (("original_watched", False), ("motion_summary", "")):
            raw = payload(); raw["videos"][0][field] = value
            row = merge_archived_fixture({}, raw)["videos"][0]
            self.assertFalse(row["motion_verified"])
        raw = payload(); raw["videos"][0]["checks"]["solo"] = None
        self.assertFalse(merge_archived_fixture({}, raw)["videos"][0]["motion_verified"])

    def test_trend_needs_three_distinct_watched_creators_same_pattern(self):
        raw = payload()
        raw["videos"][0]["related_videos"] = [example("second", "7699497385185119503"), example("third", "7699497385185119504")]
        self.assertTrue(merge_archived_fixture({}, raw)["videos"][0]["trend_verified"])
        raw["videos"][0]["related_videos"][1]["original_watched"] = False
        self.assertFalse(merge_archived_fixture({}, raw)["videos"][0]["trend_verified"])
        raw["videos"][0]["related_videos"][1] = example("SECOND", "7699497385185119504")
        self.assertFalse(merge_archived_fixture({}, raw)["videos"][0]["trend_verified"])
        raw["videos"][0]["related_videos"][1] = example("original", "7699497385185119504")
        self.assertFalse(merge_archived_fixture({}, raw)["videos"][0]["trend_verified"])

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
                merge_archived_fixture(previous, raw)
            self.assertEqual(previous, {"videos": [], "runs": []})

    def test_status_semantics_and_bounds(self):
        raw = payload(); raw["status"] = "empty"
        with self.assertRaises(work.WorkResultError): merge_archived_fixture({}, raw)
        raw = payload(); raw["videos"] = []
        with self.assertRaises(work.WorkResultError): merge_archived_fixture({}, raw)
        raw = payload(); raw["videos"] *= 9
        with self.assertRaises(work.WorkResultError): merge_archived_fixture({}, raw)

    def test_idempotency_and_same_run_conflict(self):
        raw = payload(); first = merge_archived_fixture({}, raw)
        self.assertEqual(merge_archived_fixture(first, raw), first)
        changed = copy.deepcopy(raw); changed["message"] = "changed"
        with self.assertRaises(work.WorkResultError): merge_archived_fixture(first, changed)

    def test_existing_favorite_first_seen_analysis_and_history_preserved(self):
        raw = payload(); result = merge_archived_fixture({"analysis": {"status": "not_configured"}}, raw)
        row = result["videos"][0]; row["favorite"] = True; row["first_seen_at"] = "2026-11-01T12:00:00+00:00"
        for i in range(35):
            raw["run_id"] = f"work-next-{i}"
            raw["observed_at"] = f"2026-11-11T13:{i:02d}:00+00:00"
            raw["videos"][0]["metrics"]["views"] += 1
            result = merge_archived_fixture(result, raw)
        self.assertEqual(len(result["videos"]), 1)
        self.assertEqual(len(result["videos"][0]["observations"]), 30)
        self.assertTrue(result["videos"][0]["favorite"])
        self.assertEqual(result["videos"][0]["first_seen_at"], "2026-11-01T12:00:00+00:00")
        self.assertEqual(result["analysis"], {"status": "not_configured"})
        self.assertEqual(len(result["runs"]), 36)
        original = payload()
        self.assertEqual(merge_archived_fixture(result, original), result)

    def test_run_snapshots_keep_historical_title_checks_and_metrics(self):
        first_payload = payload()
        first = merge_archived_fixture({}, first_payload)
        original_snapshot = copy.deepcopy(first["runs"][0]["video_snapshots"][0])
        changed = payload()
        changed.update(run_id="changed-observation", observed_at="2026-11-12T12:00:00Z")
        changed["videos"][0].update(title="新的标题", kind="pov", motion_summary="新的动作观察")
        changed["videos"][0]["checks"]["solo"] = False
        changed["videos"][0]["metrics"]["views"] = 500
        changed["videos"][0]["related_videos"] = [example("other", "7699497385185119503")]
        result = merge_archived_fixture(first, changed)
        self.assertEqual(result["videos"][0]["title"], "新的标题")
        historical = next(run for run in result["runs"] if run["run_id"] == first_payload["run_id"])
        self.assertEqual(historical["video_snapshots"][0], original_snapshot)
        self.assertEqual(historical["found"], 1)
        self.assertEqual(historical["added"], 1)
        self.assertEqual(result["runs"][0]["video_snapshots"][0]["metrics"]["views"], 500)
        result["videos"][0]["checks"]["solo"] = True
        result["videos"][0]["related_videos"][0]["note"] = "changed current candidate"
        self.assertFalse(result["runs"][0]["video_snapshots"][0]["checks"]["solo"])
        self.assertNotEqual(result["runs"][0]["video_snapshots"][0]["related_videos"][0]["note"], "changed current candidate")

    def test_exact_replay_backfills_old_run_without_duplicates_or_rollback(self):
        raw = payload()
        first = merge_archived_fixture({}, raw)
        del first["runs"][0]["video_snapshots"]
        del first["runs"][0]["ingestion_status"]
        recorded_run = copy.deepcopy(first["runs"][0])
        first["videos"][0]["title"] = "current title changed independently"
        first["videos"][0]["metrics"]["views"] = 900
        first["videos"][0]["checks"]["solo"] = False
        before_candidates = copy.deepcopy(first["videos"])
        result = merge_archived_fixture(first, raw)
        self.assertEqual(len(result["runs"]), 1)
        self.assertEqual(result["videos"], before_candidates)
        self.assertEqual(len(result["videos"][0]["observations"]), 1)
        for key, value in recorded_run.items():
            self.assertEqual(result["runs"][0][key], value)
        snapshot = result["runs"][0]["video_snapshots"][0]
        self.assertEqual(snapshot["title"], raw["videos"][0]["title"])
        self.assertEqual(snapshot["metrics"]["views"], 100)
        self.assertTrue(snapshot["checks"]["solo"])
        self.assertEqual(result["runs"][0]["ingestion_status"], "imported")
        self.assertNotIn("published_at", result["runs"][0])
        self.assertEqual(merge_archived_fixture(result, raw), result)

    def test_historical_snapshot_survives_missing_current_candidate(self):
        result = merge_archived_fixture({}, payload())
        snapshot = copy.deepcopy(result["runs"][0]["video_snapshots"])
        result["videos"] = []
        blocked = payload()
        blocked.update(run_id="blocked-after-removal", observed_at="2026-11-12T12:00:00Z", status="blocked", message="受限", videos=[])
        result = merge_archived_fixture(result, blocked)
        self.assertEqual(result["videos"], [])
        self.assertEqual(result["runs"][1]["video_snapshots"], snapshot)
        self.assertEqual(result["runs"][0]["video_snapshots"], [])

    def test_legacy_unrelated_run_is_not_fabricated_from_current_candidate(self):
        previous = {"videos": [], "runs": [{"id": "legacy", "status": "blocked", "found": 0,
            "started_at": "2026-11-10T12:00:00Z", "sources": [{"url": URL}]}]}
        result = merge_archived_fixture(previous, payload())
        legacy = next(run for run in result["runs"] if run["id"] == "legacy")
        self.assertNotIn("video_snapshots", legacy)
        self.assertEqual(legacy, previous["runs"][0])

    def test_run_retention_is_90_while_video_observations_stay_30(self):
        result = {}
        for i in range(100):
            raw = payload()
            raw.update(run_id=f"retention-{i}", observed_at=f"2026-11-11T13:{i // 60:02d}:{i % 60:02d}Z")
            result = merge_archived_fixture(result, raw)
        self.assertEqual(work.MAX_RUNS, 90)
        self.assertEqual(len(result["runs"]), 90)
        self.assertEqual(result["runs"][0]["run_id"], "retention-99")
        self.assertEqual(result["runs"][-1]["run_id"], "retention-10")
        self.assertEqual(len(result["videos"][0]["observations"]), 30)
        self.assertTrue(all(len(run["video_snapshots"]) == 1 for run in result["runs"]))

    def test_no_video_statuses_have_empty_snapshots_without_publication_claim(self):
        for status in ("empty", "blocked", "failed"):
            raw = payload(); raw.update(status=status, videos=[])
            result = merge_archived_fixture({}, raw)
            run = result["runs"][0]
            self.assertEqual(run["video_snapshots"], [])
            self.assertEqual(run["found"], 0)
            self.assertEqual(run["added"], 0)
            self.assertEqual(run["status"], status)
            self.assertEqual(run["ingestion_status"], "imported")
            self.assertNotIn("published", run)
            self.assertNotIn("publication_success", run)

    def test_blocked_result_keeps_candidates_and_last_success(self):
        previous = merge_archived_fixture({}, payload())
        raw = payload(); raw.update(run_id="blocked-1", observed_at="2026-11-12T12:00:00Z", status="blocked", videos=[], message="需要人工验证。")
        merged = merge_archived_fixture(previous, raw)
        self.assertEqual(merged["videos"], previous["videos"])
        self.assertEqual(merged["last_success_at"], previous["last_success_at"])
        self.assertEqual(merged["runs"][0]["status"], "blocked")

    def test_older_observation_does_not_roll_back_newer_candidate(self):
        previous = merge_archived_fixture({}, payload())
        raw = payload(); raw.update(run_id="older", observed_at="2026-11-10T12:00:00Z")
        raw["videos"][0]["title"] = "older title"
        merged = merge_archived_fixture(previous, raw)
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
            source.write_text(json.dumps(payload_v3()))
            self.assertEqual(work.main(["--input", str(source), "--output", str(output)]), 0)
            result = json.loads(output.read_text())
            self.assertEqual(result["videos"][0]["url"], URL)


if __name__ == "__main__":
    unittest.main()
