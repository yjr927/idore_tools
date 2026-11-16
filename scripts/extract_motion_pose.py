#!/usr/bin/env python3
"""Extract six seconds of motion locally; never export source pixels or audio.

Runtime: Python 3.11, mediapipe==0.10.21, numpy==1.26.4,
opencv-contrib-python==4.11.0.86. No packages are installed by this script.
Only missing version-1 task models are fetched from fixed Google model URLs.

x/y are unmirrored, full-frame screen coordinates (right/down, normalized).
Body z is MediaPipe normalized-image depth, negative toward the camera, with
the hip midpoint as origin. Hand z is wrist-relative and offset to body-wrist
z; it is qualitative estimated depth, NOT calibrated shared metric 3D.
Hands have no calibrated per-landmark confidence: the emitted conservative
confidence reflects availability and the associated body wrist, not accuracy.
"""
from __future__ import annotations

import argparse
import copy
import itertools
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

FPS, DURATION, WIDTH, HEIGHT = 24, 6, 576, 1024
FRAME_COUNT = FPS * DURATION
MIN_CONFIDENCE = 0.35
MAX_INTERPOLATION_GAP = 3
MIN_CORE_COVERAGE = 0.75
MAX_CORE_GAP = 12
MAX_MODEL_BYTES = 40 * 1024 * 1024
MODEL_URLS = {
    "pose_landmarker_full.task": "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_full/float16/1/pose_landmarker_full.task",
    "hand_landmarker.task": "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task",
}
BODY_INDICES = {"left_shoulder": 11, "right_shoulder": 12,
    "left_elbow": 13, "right_elbow": 14, "left_wrist": 15, "right_wrist": 16,
    "left_hip": 23, "right_hip": 24, "left_knee": 25, "right_knee": 26,
    "left_ankle": 27, "right_ankle": 28}
BODY_JOINTS = tuple(BODY_INDICES) + ("head_center",)
CORE_JOINTS = tuple(name for name in BODY_INDICES if any(
    name.endswith(part) for part in ("shoulder", "elbow", "wrist")))


class ExtractionError(Exception):
    def __init__(self, reason, quality=None):
        self.reason, self.quality = reason, quality
        super().__init__(reason)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def ensure_models(directory):
    """Fetch only the two fixed public model assets, without auth or redirects."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, url in MODEL_URLS.items():
        target = directory / name
        if target.exists():
            if not target.is_file() or not 1024 <= target.stat().st_size <= MAX_MODEL_BYTES:
                raise ExtractionError("invalid_cached_model")
            paths[name] = target
            continue
        temporary = None
        try:
            request = Request(url, headers={"User-Agent": "idore-local-motion/1.0"})
            with build_opener(_NoRedirect).open(request, timeout=30) as response:
                if response.status != 200 or response.geturl() != url:
                    raise ExtractionError("model_download_rejected")
                with tempfile.NamedTemporaryFile(dir=directory, delete=False) as output:
                    temporary = Path(output.name)
                    size = 0
                    while True:
                        chunk = response.read(min(1024 * 1024, MAX_MODEL_BYTES + 1 - size))
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > MAX_MODEL_BYTES:
                            raise ExtractionError("model_download_too_large")
                        output.write(chunk)
                if size < 1024:
                    raise ExtractionError("invalid_model_download")
            temporary.replace(target)
            paths[name] = target
        except ExtractionError:
            raise
        except Exception:
            raise ExtractionError("model_download_failed") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return paths


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _point(landmark, confidence):
    xyz = [getattr(landmark, axis, None) for axis in ("x", "y", "z")]
    if (not all(_finite(value) for value in xyz) or not 0 <= xyz[0] <= 1
            or not 0 <= xyz[1] <= 1 or abs(xyz[2]) > 5
            or not _finite(confidence) or confidence < MIN_CONFIDENCE):
        return None
    return [float(value) for value in xyz] + [min(1.0, float(confidence))]


def _pose_point(landmark):
    confidence = [getattr(landmark, name, None) for name in ("visibility", "presence")]
    confidence = [value for value in confidence if _finite(value)]
    return _point(landmark, min(confidence) if confidence else 0.0)


def body_from_pose(landmarks):
    if not landmarks or len(landmarks) < 33:
        return {}
    body = {}
    for name, index in BODY_INDICES.items():
        point = _pose_point(landmarks[index])
        if point is not None:
            body[name] = point
    # Retain one generic head location, never eye/nose/mouth/facial geometry.
    ears = [_pose_point(landmarks[index]) for index in (7, 8)]
    if all(point is not None for point in ears):
        body["head_center"] = [sum(point[axis] for point in ears) / 2 for axis in range(3)] + [min(point[3] for point in ears)]
    return body


def _distance(first, second):
    # Measure screen distance in units of image height rather than stretching x.
    return math.hypot((first[0] - second[0]) * WIDTH / HEIGHT, first[1] - second[1])


def assign_hands(body, detected_hands):
    """Assign globally to body wrists; ignore handedness classification labels.

    Near-tied assignments at crossed/occluded wrists are deliberately omitted.
    No hand is assigned to both sides or accepted without a reliable body wrist.
    """
    result = {"left": None, "right": None}
    candidates = []
    for landmarks in list(detected_hands or [])[:2]:
        if len(landmarks) != 21:
            continue
        points = [_point(point, 0.6) for point in landmarks]
        if all(point is not None for point in points):
            candidates.append(points)
    wrists = {side: body.get(side + "_wrist") for side in result}
    sides = [side for side in result if wrists[side] is not None]
    options = []
    for count in range(1, min(len(candidates), len(sides)) + 1):
        for chosen_sides in itertools.combinations(sides, count):
            for chosen_candidates in itertools.permutations(range(len(candidates)), count):
                pairs = list(zip(chosen_sides, chosen_candidates))
                distances = [_distance(wrists[side], candidates[index][0]) for side, index in pairs]
                if all(distance <= 0.16 for distance in distances):
                    options.append((count, sum(distances), pairs))
    if not options:
        return result, False
    options.sort(key=lambda item: (-item[0], item[1]))
    best = options[0]
    if len(options) > 1 and options[1][0] == best[0] and options[1][1] - best[1] < 0.015:
        return result, True
    for side, index in best[2]:
        points = candidates[index]
        wrist_z = points[0][2]
        confidence = min(0.6, wrists[side][3])
        result[side] = [[point[0], point[1], point[2] - wrist_z + wrists[side][2], confidence]
                        for point in points]
    return result, False


def _longest_missing(sequence):
    longest = current = 0
    for value in sequence:
        current = current + 1 if value is None else 0
        longest = max(longest, current)
    return longest


def _interpolate(sequence, *, hand=False):
    values = copy.deepcopy(sequence)
    inserted = 0
    index = 0
    while index < len(values):
        if values[index] is not None:
            index += 1
            continue
        start = index
        while index < len(values) and values[index] is None:
            index += 1
        gap = index - start
        if start == 0 or index == len(values) or gap > MAX_INTERPOLATION_GAP:
            continue
        before, after = values[start - 1], values[index]
        # Do not interpolate a large discontinuity across a suspected tracking switch.
        if _distance(before[0] if hand else before, after[0] if hand else after) > 0.3:
            continue
        for offset in range(gap):
            weight = (offset + 1) / (gap + 1)
            def blend(a, b):
                return [a[axis] * (1 - weight) + b[axis] * weight for axis in range(3)] + [min(a[3], b[3], 0.3)]
            values[start + offset] = ([blend(a, b) for a, b in zip(before, after)]
                                      if hand else blend(before, after))
            inserted += 1
    return values, inserted


def _smooth(sequence, *, hand=False):
    output = copy.deepcopy(sequence)
    for index in range(1, len(sequence) - 1):
        points = sequence[index - 1:index + 2]
        if any(point is None for point in points):
            continue
        def average(values):
            return [sum(point[axis] * weight for point, weight in zip(values, (0.25, 0.5, 0.25)))
                    for axis in range(3)] + [min(point[3] for point in values)]
        output[index] = ([average([point[joint] for point in points]) for joint in range(21)]
                         if hand else average(points))
    return output


def finalize_motion(raw_frames, *, source_fps, ambiguous_hand_frames=0):
    if len(raw_frames) != FRAME_COUNT:
        raise ExtractionError("source_video_too_short")
    quality = {"body": {}, "hands": {}, "ambiguous_hand_frames": ambiguous_hand_frames,
        "source_fps": float(source_fps), "sampling": "nearest source frame by container fps",
        "short_gap_limit_frames": MAX_INTERPOLATION_GAP, "smoothing": "centered 3-frame 1:2:1",
        "minimum_core_coverage": MIN_CORE_COVERAGE, "maximum_core_gap_frames": MAX_CORE_GAP,
        "core_joints": list(CORE_JOINTS), "finger_fidelity_guaranteed": False,
        "hand_confidence_basis": "availability and body wrist; not calibrated per-finger confidence",
        "head_information": "ear-midpoint location only; no facial appearance or expressions",
        "coordinates": {"x": "full-frame normalized right", "y": "full-frame normalized down",
            "z": "estimated normalized image depth; negative toward camera; hands aligned at body wrists; not metric 3D"}}
    output = [{"t": round(index / FPS, 6), "body": {}, "hands": {"left": None, "right": None}}
              for index in range(FRAME_COUNT)]
    for name in BODY_JOINTS + ("left", "right"):
        hand = name in ("left", "right")
        sequence = [frame["hands"].get(name) if hand else frame["body"].get(name) for frame in raw_frames]
        observed = sum(value is not None for value in sequence)
        filled, interpolated = _interpolate(sequence, hand=hand)
        values = _smooth(filled, hand=hand)
        quality["hands" if hand else "body"][name] = {
            "observed_frames": observed, "coverage": round(observed / FRAME_COUNT, 4),
            "longest_gap_frames": _longest_missing(sequence), "interpolated_frames": interpolated,
            "remaining_missing_frames": sum(value is None for value in values)}
        for frame, value in zip(output, values):
            if hand:
                frame["hands"][name] = value
            elif value is not None:
                frame["body"][name] = value
    quality["hand_missing_warning"] = any(stats["coverage"] < 0.9 or stats["longest_gap_frames"] > MAX_INTERPOLATION_GAP
                                          for stats in quality["hands"].values())
    quality["lower_body_observed"] = all(quality["body"][name]["coverage"] >= MIN_CORE_COVERAGE
                                         for name in ("left_hip", "right_hip", "left_knee", "right_knee", "left_ankle", "right_ankle"))
    if any(quality["body"][name]["coverage"] < MIN_CORE_COVERAGE
           or quality["body"][name]["longest_gap_frames"] > MAX_CORE_GAP for name in CORE_JOINTS):
        raise ExtractionError("body_tracking_insufficient", quality)
    return {"schema_version": 1, "fps": FPS, "duration": DURATION, "width": WIDTH,
            "height": HEIGHT, "frames": output, "quality": quality}


def extract_motion(input_path, model_dir):
    input_path = Path(input_path)
    if not input_path.is_file() or not 28 <= input_path.stat().st_size <= 200_000_000:
        raise ExtractionError("invalid_local_input")
    with input_path.open("rb") as source:
        if source.read(8)[4:8] != b"ftyp":
            raise ExtractionError("input_must_be_local_mp4")
    try:
        import cv2
        import mediapipe as mp
    except ImportError:
        raise ExtractionError("required_cpu_dependencies_missing") from None
    models = ensure_models(model_dir)
    capture = cv2.VideoCapture(str(input_path))
    try:
        if not capture.isOpened():
            raise ExtractionError("local_video_decode_failed")
        source_fps = capture.get(cv2.CAP_PROP_FPS)
        if not _finite(source_fps) or not 1 <= source_fps <= 240:
            raise ExtractionError("unsupported_source_fps")
        vision = mp.tasks.vision
        base = mp.tasks.BaseOptions
        pose_options = vision.PoseLandmarkerOptions(base_options=base(
            model_asset_path=str(models["pose_landmarker_full.task"]), delegate=base.Delegate.CPU),
            running_mode=vision.RunningMode.VIDEO, num_poses=1,
            min_pose_detection_confidence=0.5, min_pose_presence_confidence=0.5,
            min_tracking_confidence=0.5, output_segmentation_masks=False)
        hand_options = vision.HandLandmarkerOptions(base_options=base(
            model_asset_path=str(models["hand_landmarker.task"]), delegate=base.Delegate.CPU),
            running_mode=vision.RunningMode.VIDEO, num_hands=2,
            min_hand_detection_confidence=0.5, min_hand_presence_confidence=0.5,
            min_tracking_confidence=0.5)
        raw_frames, ambiguous = [], 0
        source_index, frame = -1, None
        with vision.PoseLandmarker.create_from_options(pose_options) as pose, vision.HandLandmarker.create_from_options(hand_options) as hands:
            for index in range(FRAME_COUNT):
                wanted = round(index / FPS * source_fps)
                while source_index < wanted:
                    success, frame = capture.read()
                    if not success:
                        raise ExtractionError("source_video_too_short")
                    source_index += 1
                if frame is None or len(frame.shape) != 3:
                    raise ExtractionError("local_video_decode_failed")
                height, width = frame.shape[:2]
                if width < 200 or height < 200 or abs(width / height - WIDTH / HEIGHT) > 0.02:
                    raise ExtractionError("unsupported_source_dimensions")
                if (width, height) != (WIDTH, HEIGHT):
                    frame = cv2.resize(frame, (WIDTH, HEIGHT), interpolation=cv2.INTER_AREA)
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                timestamp = round(index * 1000 / FPS)
                pose_result = pose.detect_for_video(image, timestamp)
                hand_result = hands.detect_for_video(image, timestamp)
                body = body_from_pose(pose_result.pose_landmarks[0] if pose_result.pose_landmarks else None)
                assigned, unsure = assign_hands(body, hand_result.hand_landmarks)
                ambiguous += int(unsure)
                raw_frames.append({"body": body, "hands": assigned})
        return finalize_motion(raw_frames, source_fps=source_fps, ambiguous_hand_frames=ambiguous)
    finally:
        capture.release()


def write_motion(path, motion):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            json.dump(motion, output, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            output.write("\n")
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # Invalid CLI values must not be copied into a log.
        raise ExtractionError("invalid_cli_arguments")


def main(argv=None):
    try:
        parser = _Parser(description=__doc__)
        parser.add_argument("--input", required=True)
        parser.add_argument("--output", required=True)
        parser.add_argument("--model-dir", required=True)
        arguments = parser.parse_args(argv)
        if Path(arguments.input).resolve() == Path(arguments.output).resolve():
            raise ExtractionError("input_output_must_differ")
        motion = extract_motion(arguments.input, arguments.model_dir)
        write_motion(arguments.output, motion)
        print(json.dumps({"status": "completed", "frames": FRAME_COUNT, "fps": FPS,
                          "duration": DURATION, "quality": motion["quality"]}, ensure_ascii=False))
        return 0
    except ExtractionError as exc:
        summary = {"status": "stopped", "reason": exc.reason}
        if exc.quality is not None:
            summary["quality"] = exc.quality
    except Exception:
        summary = {"status": "stopped", "reason": "motion_extraction_failed"}
    print(json.dumps(summary, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
