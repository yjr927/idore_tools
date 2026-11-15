#!/usr/bin/env python3
"""Apply public, watched-review classifications to existing video candidates.

Input v1 contains exactly schema_version, observed_at and items (at most 100).
Each item contains url, material_group, subject_group, original_watched and note.
This is an annotation import: it never adds videos or changes capture times,
daily reservations, original motion evidence or immutable capture snapshots.
No network requests are performed.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

try:
    from collect_tiktok_public import canonical_video, PublicPageError
except ImportError:
    import importlib.util
    _spec = importlib.util.spec_from_file_location(
        "idore_classification_url_validation", Path(__file__).with_name("collect_tiktok_public.py"))
    _collector = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_collector)
    canonical_video, PublicPageError = _collector.canonical_video, _collector.PublicPageError


TOP_KEYS = {"schema_version", "observed_at", "items"}
ITEM_KEYS = {"url", "material_group", "subject_group", "original_watched", "note"}
MATERIAL_GROUPS = ("male", "female", "mixed", "unknown")
SUBJECT_GROUPS = ("solo", "group", "unknown")
MAX_ITEMS = 100
MAX_INPUT_BYTES = 256 * 1024
MAX_HISTORY = 30
SOURCE = "ChatGPT Work 素材分类复核"


class ClassificationError(ValueError):
    """A public validation error that never includes supplied field values."""


def exact_object(value, keys, label):
    if not isinstance(value, dict) or set(value) != keys:
        raise ClassificationError(label + " 字段不完整或包含不支持的字段。")


def date_value(value):
    if not isinstance(value, str) or len(value) > 50 or "T" not in value:
        raise ClassificationError("分类时间必须是带时区的 ISO 8601 时间。")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError()
        return stamp.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        raise ClassificationError("分类时间必须是带时区的 ISO 8601 时间。") from None


def video_url(value):
    try:
        return canonical_video(value)
    except (PublicPageError, TypeError, ValueError):
        raise ClassificationError("分类只接受完整的 HTTPS TikTok 单条视频链接。") from None


def validate_payload(payload):
    exact_object(payload, TOP_KEYS, "素材分类结果")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ClassificationError("不支持的素材分类版本。")
    stamp = date_value(payload["observed_at"])
    if not isinstance(payload["items"], list) or len(payload["items"]) > MAX_ITEMS:
        raise ClassificationError("每批素材分类最多包含 100 条已有视频。")
    items, seen = [], set()
    for item in payload["items"]:
        exact_object(item, ITEM_KEYS, "视频分类")
        url, _, ident = video_url(item["url"])
        if ident in seen:
            raise ClassificationError("本批分类包含重复的视频。")
        seen.add(ident)
        material, subject = item["material_group"], item["subject_group"]
        if not isinstance(material, str) or material not in MATERIAL_GROUPS:
            raise ClassificationError("material_group 分类无效。")
        if not isinstance(subject, str) or subject not in SUBJECT_GROUPS:
            raise ClassificationError("subject_group 分类无效。")
        watched = item["original_watched"]
        if type(watched) is not bool:
            raise ClassificationError("original_watched 必须为 true 或 false。")
        if not watched and (material != "unknown" or subject != "unknown"):
            raise ClassificationError("未观看原片时，素材分区和单人／多人分类必须为 unknown。")
        note = item["note"]
        if not isinstance(note, str) or not note.strip() or len(note) > 1000 or any(
                ord(char) < 32 and char not in "\n\r\t" for char in note):
            raise ClassificationError("note 必须为 1–1000 字的公开分类依据。")
        items.append({"url": url, "material_group": material, "subject_group": subject,
                      "original_watched": watched, "note": note.strip()})
    # Input order is not a distinct classification observation.
    items.sort(key=lambda item: video_url(item["url"])[2])
    return {"schema_version": 1, "observed_at": stamp, "items": items}


def merge_classifications(previous, payload):
    """Return an atomic metadata-only update; preserve newer classifications."""
    clean = validate_payload(payload)
    if not isinstance(previous, dict) or not isinstance(previous.get("videos"), list):
        raise ClassificationError("已有候选库格式无效。")
    imports = previous.get("material_classification_imports", {})
    if not isinstance(imports, dict):
        raise ClassificationError("已有素材分类导入记录格式无效。")
    fingerprint = hashlib.sha256(json.dumps(clean, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    stamp = clean["observed_at"]
    if stamp in imports:
        if imports[stamp] == fingerprint:
            return copy.deepcopy(previous)
        raise ClassificationError("该观察时间已用于不同分类结果，请使用新的实际观察时间。")
    result = copy.deepcopy(previous)
    candidates = {}
    for row in result["videos"]:
        if not isinstance(row, dict):
            continue
        try:
            _, _, ident = video_url(row.get("url"))
        except ClassificationError:
            continue
        if ident in candidates:
            raise ClassificationError("已有候选库包含重复的视频编号。")
        candidates[ident] = row
    for item in clean["items"]:
        ident = video_url(item["url"])[2]
        if ident not in candidates:
            raise ClassificationError("分类包含尚未入库的视频；请先通过采集流程收录。")
        row = candidates[ident]
        annotation = {"observed_at": stamp, "material_group": item["material_group"],
            "subject_group": item["subject_group"], "original_watched": item["original_watched"],
            "method": "watched_original" if item["original_watched"] else "unwatched",
            "source": SOURCE, "note": item["note"]}
        history = row.get("material_classification_history", [])
        current = row.get("material_classification")
        if not isinstance(history, list) or any(not isinstance(entry, dict) for entry in history):
            raise ClassificationError("已有素材分类历史格式无效。")
        if current is not None and not isinstance(current, dict):
            raise ClassificationError("已有素材分类格式无效。")
        prior = history + ([current] if current else [])
        for entry in prior:
            if date_value(entry.get("observed_at")) == stamp and entry != annotation:
                raise ClassificationError("该视频的同一观察时间已有不同分类。")
        if annotation not in history:
            history = history + [copy.deepcopy(annotation)]
        history.sort(key=lambda entry: date_value(entry.get("observed_at")))
        row["material_classification_history"] = history[-MAX_HISTORY:]
        if not current or stamp >= date_value(current.get("observed_at")):
            row["material_classification"] = annotation
    result.setdefault("material_classification_imports", {})[stamp] = fingerprint
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("work-results/classifications.json"))
    parser.add_argument("--output", type=Path, default=Path("site/data/discovery.json"))
    args = parser.parse_args(argv)
    if not args.input.is_file():
        print("No material classification file; candidate data unchanged.")
        return 0
    try:
        if args.input.stat().st_size > MAX_INPUT_BYTES:
            raise ClassificationError("素材分类文件过大。")
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        previous = json.loads(args.output.read_text(encoding="utf-8"))
        merged = merge_classifications(previous, payload)
        serialized = json.dumps(merged, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    except (OSError, ValueError, TypeError, OverflowError):
        print("Material classification validation failed; candidate data unchanged.", file=sys.stderr)
        return 1
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    try:
        temporary.write_text(serialized, encoding="utf-8")
        temporary.replace(args.output)
    except OSError:
        temporary.unlink(missing_ok=True)
        print("Material classifications could not be saved; previous data retained.", file=sys.stderr)
        return 1
    print("Validated material classifications imported; capture history unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
