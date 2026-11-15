import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


spec = importlib.util.spec_from_file_location("material_classifications",
    Path(__file__).resolve().parents[1] / "scripts" / "import_material_classifications.py")
classifications = importlib.util.module_from_spec(spec)
spec.loader.exec_module(classifications)
URL = "https://www.tiktok.com/@original/video/7699497385185119502"
STAMP = "2026-11-15T15:00:00+00:00"


def payload():
    return {"schema_version": 1, "observed_at": STAMP, "items": [{"url": URL,
        "material_group": "male", "subject_group": "solo", "original_watched": True,
        "note": "已复看原片，单人构图；将此玩法分配给男生角色素材。"}]}


def state():
    video = {"id": "tiktok-7699497385185119502", "url": URL,
        "first_seen_at": "2026-11-11T12:00:00+00:00", "last_seen_at": "2026-11-12T12:00:00+00:00",
        "original_watched": False, "checks": {"solo": None, "repetitive": None, "fixed_camera": None},
        "motion_verified": False, "trend_verified": False, "material_group": "unknown",
        "observations": [{"observed_at": "2026-11-12T12:00:00+00:00", "original_watched": False}]}
    return {"videos": [video], "runs": [{"id": "historical-capture", "video_snapshots": [copy.deepcopy(video)]}],
        "work_daily_selections": {"2026-11-15": {"kpop": video["id"]}},
        "work_imports": {"historical-capture": "existing-hash"}, "last_attempt_at": "old-capture-time"}


class MaterialClassificationTests(unittest.TestCase):
    def test_annotation_preserves_capture_evidence_history_and_daily_slots(self):
        previous = state()
        before = copy.deepcopy(previous)
        result = classifications.merge_classifications(previous, payload())
        annotation = result["videos"][0]["material_classification"]
        self.assertEqual(annotation["material_group"], "male")
        self.assertEqual(annotation["subject_group"], "solo")
        self.assertEqual(annotation["observed_at"], STAMP)
        self.assertEqual(annotation["method"], "watched_original")
        self.assertTrue(annotation["original_watched"])
        self.assertEqual(len(result["videos"]), 1)
        for key, value in previous["videos"][0].items():
            self.assertEqual(result["videos"][0][key], value)
        for key in ("runs", "work_daily_selections", "work_imports", "last_attempt_at"):
            self.assertEqual(result[key], previous[key])
        self.assertEqual(result["videos"][0]["material_classification_history"], [annotation])
        self.assertEqual(previous, before)

    def test_known_material_or_cast_needs_watched_original(self):
        for material, cast in (("male", "unknown"), ("female", "unknown"), ("mixed", "group"), ("unknown", "solo")):
            raw = payload()
            raw["items"][0].update(material_group=material, subject_group=cast, original_watched=False)
            with self.subTest(material=material, cast=cast), self.assertRaises(classifications.ClassificationError):
                classifications.merge_classifications(state(), raw)
        raw["items"][0].update(material_group="unknown", subject_group="unknown")
        annotation = classifications.merge_classifications(state(), raw)["videos"][0]["material_classification"]
        self.assertEqual(annotation["method"], "unwatched")
        self.assertEqual(annotation["subject_group"], "unknown")

    def test_strict_input_rejects_extra_fields_invalid_values_and_unbounded_batches(self):
        mutations = [
            lambda p: p.update(api_key="not-accepted"),
            lambda p: p.update(schema_version=True),
            lambda p: p.update(observed_at="2026-11-15T15:00:00"),
            lambda p: p["items"][0].pop("subject_group"),
            lambda p: p["items"][0].update(subject_group="duo"),
            lambda p: p["items"][0].update(material_group="boy"),
            lambda p: p["items"][0].update(original_watched=1),
            lambda p: p["items"][0].update(note=""),
            lambda p: p["items"][0].update(note="x" * 1001),
            lambda p: p["items"][0].update(note="bad\x00text"),
            lambda p: p["items"][0].update(url="https://www.tiktok.com.evil.test/@x/video/7699497385185119502"),
            lambda p: p["items"].append(copy.deepcopy(p["items"][0])),
            lambda p: p.update(items=p["items"] * 101),
        ]
        for mutate in mutations:
            raw = payload(); mutate(raw)
            with self.subTest(mutation=mutate), self.assertRaises(classifications.ClassificationError):
                classifications.merge_classifications(state(), raw)

    def test_only_existing_video_ids_are_annotated_and_failure_is_atomic(self):
        previous = state(); before = copy.deepcopy(previous)
        raw = payload()
        unknown = copy.deepcopy(raw["items"][0])
        unknown["url"] = URL.replace("7699497385185119502", "7699497385185119503")
        raw["items"].append(unknown)
        with self.assertRaises(classifications.ClassificationError):
            classifications.merge_classifications(previous, raw)
        self.assertEqual(previous, before)

    def test_replays_normalize_offset_tracking_and_item_order(self):
        raw = payload()
        second_video = copy.deepcopy(state()["videos"][0])
        second_video.update(id="tiktok-7699497385185119503", url=URL.replace("9502", "9503"))
        previous = state(); previous["videos"].append(second_video)
        second_item = copy.deepcopy(raw["items"][0]); second_item["url"] = second_video["url"]
        raw["items"].append(second_item)
        first = classifications.merge_classifications(previous, raw)
        raw["items"].reverse()
        raw["items"][0]["url"] += "?tracking=1"
        raw["observed_at"] = "2026-11-15T10:00:00-05:00"
        self.assertEqual(classifications.merge_classifications(first, raw), first)

    def test_same_timestamp_different_annotations_conflict_without_mutation(self):
        raw = payload()
        first = classifications.merge_classifications(state(), raw); before = copy.deepcopy(first)
        raw["items"][0]["material_group"] = "female"
        with self.assertRaises(classifications.ClassificationError):
            classifications.merge_classifications(first, raw)
        self.assertEqual(first, before)
        del first["material_classification_imports"]
        with self.assertRaises(classifications.ClassificationError):
            classifications.merge_classifications(first, raw)

    def test_older_annotations_are_recorded_but_do_not_roll_back_newer_classification(self):
        raw = payload()
        first = classifications.merge_classifications(state(), raw)
        newer = copy.deepcopy(raw); newer["observed_at"] = "2026-11-16T15:00:00Z"
        newer["items"][0].update(material_group="female", subject_group="group")
        result = classifications.merge_classifications(first, newer)
        older = copy.deepcopy(raw); older["observed_at"] = "2026-11-14T15:00:00Z"
        older["items"][0].update(material_group="mixed", subject_group="group")
        result = classifications.merge_classifications(result, older)
        self.assertEqual(result["videos"][0]["material_classification"]["material_group"], "female")
        self.assertEqual([entry["material_group"] for entry in result["videos"][0]["material_classification_history"]],
            ["mixed", "male", "female"])
        self.assertEqual(result["runs"], first["runs"])

    def test_classification_history_is_bounded_without_changing_capture_observations(self):
        result = state()
        for minute in range(35):
            raw = payload(); raw["observed_at"] = f"2026-11-15T15:{minute:02d}:00Z"
            result = classifications.merge_classifications(result, raw)
        video = result["videos"][0]
        self.assertEqual(len(video["material_classification_history"]), 30)
        self.assertEqual(video["material_classification"]["observed_at"], "2026-11-15T15:34:00+00:00")
        self.assertEqual(video["observations"], state()["videos"][0]["observations"])

    def test_cli_missing_invalid_and_valid_annotations_preserve_or_update_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "classifications.json"; output = Path(tmp) / "discovery.json"
            original = json.dumps(state(), ensure_ascii=False)
            output.write_text(original)
            self.assertEqual(classifications.main(["--input", str(source), "--output", str(output)]), 0)
            self.assertEqual(output.read_text(), original)
            invalid = payload(); invalid["items"][0]["url"] = URL.replace("9502", "9503")
            source.write_text(json.dumps(invalid))
            self.assertEqual(classifications.main(["--input", str(source), "--output", str(output)]), 1)
            self.assertEqual(output.read_text(), original)
            source.write_text(json.dumps(payload()))
            self.assertEqual(classifications.main(["--input", str(source), "--output", str(output)]), 0)
            result = json.loads(output.read_text())
            self.assertEqual(result["videos"][0]["material_classification"]["subject_group"], "solo")
            self.assertEqual(result["runs"], state()["runs"])
            self.assertFalse(output.with_suffix(".json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
