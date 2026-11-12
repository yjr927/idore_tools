#!/usr/bin/env python3
"""Validate and merge a bounded ChatGPT Work public-video observation.

Input v1 has exactly schema_version, run_id, observed_at, status, message, videos.
Each video has exactly url, title, kind, original_watched, motion_summary, checks,
recreation_notes, published_at, published_text, metrics, related_videos, music_name.
checks contains solo/repetitive/fixed_camera (boolean or null). metrics contains
views/likes/comments/shares (finite nonnegative number or null). Related records
contain url/original_watched/same_pattern/note. All dates require a timezone.

Only watched evidence can establish motion_verified; trend_verified additionally
requires two watched same-pattern examples from two other distinct authors. These
fields are derived here and cannot be supplied in the input. No network calls.

Every retained run carries immutable video_snapshots from that observation, so
history does not depend on today's candidate titles or metrics. The newest 90
runs are retained; each video retains its newest 30 detailed observations.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sys

try:
    from collect_tiktok_public import canonical_video, PublicPageError
except ImportError:
    # Import-by-path test callers may not have scripts/ on sys.path.
    import importlib.util
    _spec = importlib.util.spec_from_file_location(
        "idore_public_url_validation", Path(__file__).with_name("collect_tiktok_public.py"))
    _collector = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_collector)
    canonical_video, PublicPageError = _collector.canonical_video, _collector.PublicPageError


TOP_KEYS = {"schema_version", "run_id", "observed_at", "status", "message", "videos"}
VIDEO_KEYS = {"url", "title", "kind", "original_watched", "motion_summary", "checks",
              "recreation_notes", "published_at", "published_text", "metrics",
              "related_videos", "music_name"}
CHECK_KEYS = {"solo", "repetitive", "fixed_camera"}
METRIC_KEYS = {"views", "likes", "comments", "shares"}
RELATED_KEYS = {"url", "original_watched", "same_pattern", "note"}
STATUSES = {"succeeded", "partial", "empty", "blocked", "failed"}
MAX_INPUT_BYTES = 256 * 1024
MAX_RUNS = 90
SOURCE = "ChatGPT Work 云端浏览器"
SNAPSHOT_KEYS = (
    "id", "video_id", "url", "title", "author", "kind", "original_watched",
    "motion_summary", "checks", "recreation_notes", "published_at", "published_text",
    "metrics", "related_videos", "music_name", "motion_verified", "trend_verified",
    "source", "source_url", "source_kind", "assessment", "assessment_note",
)


class WorkResultError(ValueError):
    """Public validation error; never includes user-provided field values."""


def exact_object(value, keys, label):
    if not isinstance(value, dict) or set(value) != keys:
        raise WorkResultError(label + " 字段不完整或包含不支持的字段。")
    return value


def public_text(value, label, maximum, required=False):
    if not isinstance(value, str) or len(value) > maximum or any(
            ord(char) < 32 and char not in "\n\r\t" for char in value):
        raise WorkResultError(label + " 必须是长度受限的公开文字。")
    if required and not value.strip():
        raise WorkResultError(label + " 不能为空。")
    return value.strip()


def date_value(value, label, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or len(value) > 50 or "T" not in value:
        raise WorkResultError(label + " 必须是带时区的 ISO 8601 时间。")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        raise WorkResultError(label + " 必须是带时区的 ISO 8601 时间。") from None


def optional_bool(value, label):
    if value is not None and type(value) is not bool:
        raise WorkResultError(label + " 必须为 true、false 或 null。")
    return value


def watched(value):
    if type(value) is not bool:
        raise WorkResultError("original_watched 必须为 true 或 false。")
    return value


def public_url(value):
    try:
        return canonical_video(value)
    except (PublicPageError, TypeError, ValueError):
        raise WorkResultError("只接受完整的 HTTPS TikTok 单条视频链接。") from None


def metric(value):
    if value is None:
        return None
    if type(value) not in (int, float) or not 0 <= value <= 1e15 or not math.isfinite(value):
        raise WorkResultError("互动数据必须为有限的非负数字或 null。")
    return value


def validate_payload(payload):
    exact_object(payload, TOP_KEYS, "Work 结果")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise WorkResultError("不支持的 Work 结果版本。")
    ident = payload["run_id"]
    if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}", ident):
        raise WorkResultError("run_id 必须是 1–100 位字母、数字或常见分隔符。")
    stamp = date_value(payload["observed_at"], "observed_at")
    status = payload["status"]
    if not isinstance(status, str) or status not in STATUSES:
        raise WorkResultError("Work 采集状态无效。")
    message = public_text(payload["message"], "message", 1000, True)
    videos = payload["videos"]
    if not isinstance(videos, list) or len(videos) > 8:
        raise WorkResultError("每次 Work 结果最多包含 8 条视频。")
    if (status in ("succeeded", "partial")) != bool(videos):
        raise WorkResultError("成功或部分成功必须有视频；empty、blocked、failed 不得包含视频。")
    cleaned, seen = [], set()
    for video in videos:
        exact_object(video, VIDEO_KEYS, "视频")
        url, author, video_id = public_url(video["url"])
        if video_id in seen:
            raise WorkResultError("本次结果包含重复的原视频。")
        seen.add(video_id)
        kind = video["kind"]
        if not isinstance(kind, str) or kind not in ("hands", "transition", "pov"):
            raise WorkResultError("视频 kind 必须为 hands、transition 或 pov。")
        seen_original = watched(video["original_watched"])
        checks = exact_object(video["checks"], CHECK_KEYS, "checks")
        checks = {key: optional_bool(checks[key], "checks") for key in sorted(CHECK_KEYS)}
        metrics = exact_object(video["metrics"], METRIC_KEYS, "metrics")
        metrics = {key: metric(metrics[key]) for key in sorted(METRIC_KEYS)}
        published = date_value(video["published_at"], "published_at", True)
        if published and datetime.fromisoformat(published) > datetime.fromisoformat(stamp):
            raise WorkResultError("原视频发布时间不能晚于观察时间。")
        related = video["related_videos"]
        if not isinstance(related, list) or len(related) > 5:
            raise WorkResultError("每条原视频最多包含 5 条跟拍示例。")
        related_clean, related_ids = [], {video_id}
        for example in related:
            exact_object(example, RELATED_KEYS, "跟拍示例")
            example_url, _, example_id = public_url(example["url"])
            if example_id in related_ids:
                raise WorkResultError("跟拍示例重复或指向原视频本身。")
            related_ids.add(example_id)
            related_clean.append({"url": example_url,
                "original_watched": watched(example["original_watched"]),
                "same_pattern": optional_bool(example["same_pattern"], "same_pattern"),
                "note": public_text(example["note"], "跟拍 note", 500)})
        cleaned.append({"url": url, "title": public_text(video["title"], "title", 300, True),
            "kind": kind, "original_watched": seen_original,
            "motion_summary": public_text(video["motion_summary"], "motion_summary", 2000),
            "checks": checks, "recreation_notes": public_text(video["recreation_notes"], "recreation_notes", 2000),
            "published_at": published, "published_text": public_text(video["published_text"], "published_text", 100),
            "metrics": metrics, "related_videos": related_clean,
            "music_name": public_text(video["music_name"], "music_name", 300)})
    return {"schema_version": 1, "run_id": ident, "observed_at": stamp, "status": status,
            "message": message, "videos": cleaned}


def normalized_video(video, stamp):
    url, author, video_id = public_url(video["url"])
    motion = (video["original_watched"] and bool(video["motion_summary"])
              and video["checks"]["solo"] is True and video["checks"]["repetitive"] is True)
    other_authors = {
        public_url(example["url"])[1].lower() for example in video["related_videos"]
        if example["original_watched"] and example["same_pattern"] is True
        and public_url(example["url"])[1].lower() != author.lower()
    }
    trend = video["original_watched"] and len(other_authors) >= 2
    note = video["motion_summary"] if video["original_watched"] else "原片尚未观看；动作与跟拍仍待核验。"
    return {**copy.deepcopy(video), "id": "tiktok-" + video_id, "video_id": video_id,
        "author": author, "kinds": [video["kind"]], "url": url,
        "first_seen_at": stamp, "last_seen_at": stamp, "duration": None, "music_id": "",
        "source": SOURCE, "source_url": url, "source_kind": "chatgpt_work", "search_query": "",
        "assessment": "已看原片的动作候选" if motion else "待核验候选", "assessment_note": note,
        "motion_verified": bool(motion), "trend_verified": bool(trend), "favorite": False,
        "availability": "Work 已观看原片" if video["original_watched"] else "仅发现链接；原片待观看"}


def _timestamp(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, TypeError, ValueError, OverflowError):
        return float("-inf")


def video_snapshots(clean):
    """Capture only the validated observation, never current candidate state."""
    snapshots = []
    for video in clean["videos"]:
        row = normalized_video(video, clean["observed_at"])
        snapshots.append({**{key: copy.deepcopy(row[key]) for key in SNAPSHOT_KEYS},
                          "observed_at": clean["observed_at"]})
    return snapshots


def enrich_existing_run(previous, clean):
    """Backfill exact replayed observations without changing recorded run facts.

    A matching fingerprint was checked by the caller. Other legacy runs cannot
    be reconstructed from today's candidates and are deliberately left alone.
    """
    result = copy.deepcopy(previous)
    for run in result.get("runs", []):
        if not isinstance(run, dict) or not (run.get("run_id") == clean["run_id"] or run.get("id") == clean["run_id"]):
            continue
        if "video_snapshots" not in run:
            run["video_snapshots"] = video_snapshots(clean)
        # This records input ingestion, not a successful website publication.
        run.setdefault("ingestion_status", "imported")
    result["runs"] = result.get("runs", [])[:MAX_RUNS]
    return result


def merge_work_results(previous, payload):
    """Return a new state; validation/idempotency errors never mutate previous."""
    clean = validate_payload(payload)
    if not isinstance(previous, dict) or not isinstance(previous.get("videos", []), list) or not isinstance(previous.get("runs", []), list):
        raise WorkResultError("已有候选库格式无效。")
    fingerprint = hashlib.sha256(json.dumps(clean, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    imports = previous.get("work_imports", {})
    if not isinstance(imports, dict):
        raise WorkResultError("已有 Work 导入记录格式无效。")
    if clean["run_id"] in imports:
        if imports[clean["run_id"]] == fingerprint:
            return enrich_existing_run(previous, clean)
        raise WorkResultError("该 run_id 已用于不同结果，请使用新的 run_id。")
    for prior in previous.get("runs", []):
        if isinstance(prior, dict) and (prior.get("run_id") == clean["run_id"] or prior.get("id") == clean["run_id"]):
            if prior.get("payload_sha256") == fingerprint:
                return enrich_existing_run(previous, clean)
            raise WorkResultError("该 run_id 已用于不同结果，请使用新的 run_id。")
    result = copy.deepcopy(previous)
    result.setdefault("videos", []); result.setdefault("runs", [])
    by_id = {row.get("id"): row for row in result["videos"] if isinstance(row, dict)}
    stamp, added = clean["observed_at"], 0
    for video in clean["videos"]:
        row = normalized_video(video, stamp)
        observation = {"run_id": clean["run_id"], "observed_at": stamp,
            **{key: copy.deepcopy(row[key]) for key in ("metrics", "original_watched", "motion_summary",
                "checks", "recreation_notes", "related_videos", "motion_verified", "trend_verified")}}
        existing = by_id.get(row["id"])
        if existing is None:
            row["observations"] = [observation]
            result["videos"].append(row); by_id[row["id"]] = row; added += 1
        else:
            observations = existing.get("observations", [])
            if not isinstance(observations, list): observations = []
            observations = observations + [observation]
            observations.sort(key=lambda item: _timestamp(item.get("observed_at")) if isinstance(item, dict) else float("-inf"))
            observations = observations[-30:]
            if _timestamp(stamp) >= _timestamp(existing.get("last_seen_at")):
                keep = {"first_seen_at": existing.get("first_seen_at", stamp), "favorite": existing.get("favorite", False)}
                kinds = list(dict.fromkeys(existing.get("kinds", [existing.get("kind")]) + [row["kind"]]))
                existing.update(row); existing.update(keep); existing["kinds"] = [kind for kind in kinds if kind]
            existing["observations"] = observations
    source_urls = list(dict.fromkeys([url for video in clean["videos"] for url in
        [video["url"]] + [example["url"] for example in video["related_videos"]]]))
    run = {"id": clean["run_id"], "run_id": clean["run_id"], "payload_sha256": fingerprint,
        "collector": "chatgpt_work", "status": clean["status"], "kind": "all", "query": "",
        "started_at": stamp, "finished_at": stamp, "observed_at": stamp,
        "found": len(clean["videos"]), "added": added, "message": clean["message"],
        "ingestion_status": "imported", "video_snapshots": video_snapshots(clean),
        "sources": [{"name": SOURCE, "url": url, "status": "ok", "found": 1} for url in source_urls]}
    result["runs"].append(run)
    result["runs"].sort(key=lambda row: _timestamp(row.get("observed_at") or row.get("started_at")), reverse=True)
    result["runs"] = result["runs"][:MAX_RUNS]
    if _timestamp(stamp) >= _timestamp(result.get("last_attempt_at")):
        result.update(collector="chatgpt_work", last_attempt_at=stamp)
    if clean["videos"] and _timestamp(stamp) >= _timestamp(result.get("last_success_at")):
        result["last_success_at"] = stamp
    result.setdefault("work_imports", {})[clean["run_id"]] = fingerprint
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("work-results/latest.json"))
    parser.add_argument("--output", type=Path, default=Path("site/data/discovery.json"))
    args = parser.parse_args(argv)
    if not args.input.is_file():
        print("No Work result file; published candidate data unchanged.")
        return 0
    try:
        if args.input.stat().st_size > MAX_INPUT_BYTES:
            raise WorkResultError("Work 结果文件超过允许大小。")
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        previous = json.loads(args.output.read_text(encoding="utf-8")) if args.output.exists() else {"videos": [], "runs": []}
        merged = merge_work_results(previous, payload)
        serialized = json.dumps(merged, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    except (OSError, ValueError, TypeError, OverflowError):
        print("Work result validation failed; candidate data unchanged.", file=sys.stderr)
        return 1
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(serialized, encoding="utf-8")
        temporary.replace(args.output)
    except OSError:
        temporary.unlink(missing_ok=True)
        print("Work result could not be saved; previous candidate data retained.", file=sys.stderr)
        return 1
    print("Validated Work observation imported.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
