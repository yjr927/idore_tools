import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from scripts import extract_motion_pose as motion


def point(x, y, z=0.0, confidence=0.95):
    return SimpleNamespace(x=x, y=y, z=z, visibility=confidence, presence=confidence)


def body():
    return {name: [0.3 if name.startswith("left") else 0.7,
                   0.3 if name.endswith("shoulder") else 0.5 if name.endswith("elbow") else 0.7,
                   0.1, 0.95] for name in motion.CORE_JOINTS}


def frames():
    return [{"body": body(), "hands": {"left": None, "right": None}}
            for _ in range(motion.FRAME_COUNT)]


def hand(x, y, z=0.0):
    return [point(x + index * 0.001, y - index * 0.001, z - index * 0.001)
            for index in range(21)]


class MotionExtractionTests(unittest.TestCase):
    def test_hand_assignment_uses_body_wrists_not_detector_order_and_rejects_ambiguity(self):
        tracked = body()
        result, ambiguous = motion.assign_hands(tracked, [hand(0.7, 0.7, 2), hand(0.3, 0.7, -2)])
        self.assertFalse(ambiguous)
        self.assertEqual(result["left"][0][:3], [0.3, 0.7, 0.1])
        self.assertEqual(result["right"][0][:3], [0.7, 0.7, 0.1])
        self.assertLessEqual(result["left"][0][3], 0.6)
        tracked["left_wrist"] = tracked["right_wrist"] = [0.5, 0.7, 0.1, 0.95]
        result, ambiguous = motion.assign_hands(tracked, [hand(0.5, 0.7)])
        self.assertTrue(ambiguous)
        self.assertEqual(result, {"left": None, "right": None})

    def test_short_missing_motion_is_marked_interpolated_but_long_hand_gaps_stay_unknown(self):
        raw = frames()
        for index in range(len(raw)):
            raw[index]["hands"]["left"] = [[0.3, 0.7, 0, 0.6] for _ in range(21)]
        for index in (20, 21):
            raw[index]["body"].pop("left_wrist")
            raw[index]["hands"]["left"] = None
        for index in range(60, 66):
            raw[index]["hands"]["left"] = None
        result = motion.finalize_motion(raw, source_fps=30)
        self.assertEqual(result["quality"]["body"]["left_wrist"]["interpolated_frames"], 2)
        self.assertLessEqual(result["frames"][20]["body"]["left_wrist"][3], 0.3)
        self.assertEqual(result["quality"]["hands"]["left"]["longest_gap_frames"], 6)
        self.assertIsNone(result["frames"][62]["hands"]["left"])
        self.assertTrue(result["quality"]["hand_missing_warning"])
        self.assertFalse(result["quality"]["lower_body_observed"])
        self.assertNotIn("left_ankle", result["frames"][0]["body"])
        self.assertNotIn("left_ankle", raw[0]["body"])

    def test_body_gate_uses_observed_data_and_does_not_treat_interpolation_as_evidence(self):
        raw = frames()
        for index in range(0, len(raw), 2):
            raw[index]["body"].pop("right_wrist")
        with self.assertRaises(motion.ExtractionError) as caught:
            motion.finalize_motion(raw, source_fps=30)
        self.assertEqual(caught.exception.reason, "body_tracking_insufficient")
        self.assertEqual(caught.exception.quality["body"]["right_wrist"]["coverage"], 0.5)
        self.assertGreater(caught.exception.quality["body"]["right_wrist"]["interpolated_frames"], 0)

    def test_pose_output_has_no_face_landmarks_or_source_appearance(self):
        landmarks = [point(0.5, 0.5) for _ in range(33)]
        landmarks[7], landmarks[8] = point(0.4, 0.2), point(0.6, 0.2)
        result = motion.body_from_pose(landmarks)
        self.assertEqual(set(result), set(motion.BODY_JOINTS))
        self.assertEqual(result["head_center"][:2], [0.5, 0.2])
        self.assertFalse(any(name in result for name in ("nose", "eyes", "face", "mouth")))

    def test_cpu_video_pipeline_processes_all_144_timestamps_without_live_dependencies(self):
        timestamps = {"body": [], "hands": []}
        landmarks = [point(0.5, 0.5) for _ in range(33)]
        for name, index in motion.BODY_INDICES.items():
            if name in body():
                p = body()[name]
                landmarks[index] = point(*p[:3])
            else:
                landmarks[index] = point(0.5, 0.9, confidence=0.1)
        class Task:
            def __init__(self, name): self.name = name
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def detect_for_video(self, image, timestamp):
                timestamps[self.name].append(timestamp)
                return SimpleNamespace(pose_landmarks=[landmarks]) if self.name == "body" else SimpleNamespace(hand_landmarks=[])
        delegates = []
        class BaseOptions:
            Delegate = SimpleNamespace(CPU="CPU")
            def __init__(self, **kwargs): delegates.append(kwargs["delegate"])
        class Capture:
            def __init__(self, path): self.reads, self.released = 0, False
            def isOpened(self): return True
            def get(self, key): return 30.0
            def read(self):
                self.reads += 1
                return True, SimpleNamespace(shape=(1024, 576, 3))
            def release(self): self.released = True
        capture = Capture(None)
        cv2 = SimpleNamespace(VideoCapture=lambda path: capture, CAP_PROP_FPS=1,
            COLOR_BGR2RGB=2, cvtColor=lambda frame, code: "PRIVATE_PIXELS_NOT_FOR_OUTPUT")
        vision = SimpleNamespace(RunningMode=SimpleNamespace(VIDEO="VIDEO"),
            PoseLandmarkerOptions=lambda **kwargs: kwargs, HandLandmarkerOptions=lambda **kwargs: kwargs,
            PoseLandmarker=SimpleNamespace(create_from_options=lambda options: Task("body")),
            HandLandmarker=SimpleNamespace(create_from_options=lambda options: Task("hands")))
        mp = SimpleNamespace(tasks=SimpleNamespace(vision=vision, BaseOptions=BaseOptions),
            Image=lambda **kwargs: kwargs, ImageFormat=SimpleNamespace(SRGB=1))
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "private.mp4"
            source.write_bytes(b"\x00\x00\x00\x20ftyp" + b"x" * 24)
            model_paths = {name: Path(directory) / name for name in motion.MODEL_URLS}
            with patch.dict("sys.modules", {"cv2": cv2, "mediapipe": mp}), patch.object(motion, "ensure_models", return_value=model_paths):
                result = motion.extract_motion(source, directory)
            output = Path(directory) / "motion.json"
            motion.write_motion(output, result)
            document = output.read_text()
            self.assertNotIn("PRIVATE_PIXELS", document)
            self.assertNotIn(str(source), document)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertTrue(capture.released)
        self.assertEqual(delegates, ["CPU", "CPU"])
        self.assertEqual(len(result["frames"]), 144)
        self.assertEqual(timestamps["body"], timestamps["hands"])
        self.assertEqual(len(set(timestamps["body"])), 144)
        self.assertEqual(timestamps["body"][-1], 5958)
        self.assertTrue(result["quality"]["hand_missing_warning"])

    def test_cli_failures_do_not_echo_private_paths_or_upstream_exception_content(self):
        output = io.StringIO()
        with redirect_stdout(output), patch.object(motion, "extract_motion", side_effect=RuntimeError("https://secret.example/signed?token=PRIVATE")):
            status = motion.main(["--input", "private.mp4", "--output", "motion.json", "--model-dir", "models"])
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(output.getvalue()), {"status": "stopped", "reason": "motion_extraction_failed"})
        self.assertNotIn("PRIVATE", output.getvalue())


if __name__ == "__main__":
    unittest.main()
