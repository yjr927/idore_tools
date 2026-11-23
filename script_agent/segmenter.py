from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from typing import Dict, Iterable, List, Sequence, Tuple


SCENE_HEADING_PATTERNS = (
    re.compile(r"^\s*(?:第\s*[零〇一二两三四五六七八九十百千\d]+\s*[场幕集章]|场景\s*[零〇一二两三四五六七八九十百千\d]+)\b"),
    re.compile(r"^\s*(?:INT\.?|EXT\.?|INT\.?/EXT\.?|I/E\.?)\s+", re.IGNORECASE),
    re.compile(r"^\s*\d{1,4}\s*[\.、]\s*(?:内景|外景|日|夜|晨|晚|INT|EXT)\b", re.IGNORECASE),
    re.compile(r"^\s*(?:内景|外景)\s*[·.\-— ]"),
)


@dataclass(frozen=True)
class Scene:
    id: str
    order: int
    heading: str
    start_line: int
    end_line: int
    text: str
    digest: str

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


def is_scene_heading(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > 120:
        return False
    return any(pattern.search(stripped) for pattern in SCENE_HEADING_PATTERNS)


def split_scenes(text: str, max_chars: int = 14000) -> List[Scene]:
    """Split on screenplay headings, with paragraph-aware fallback chunks."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    heading_indexes = [index for index, line in enumerate(lines) if is_scene_heading(line)]

    raw_ranges: List[Tuple[int, int, str]] = []
    if heading_indexes:
        if heading_indexes[0] > 0 and any(line.strip() for line in lines[: heading_indexes[0]]):
            raw_ranges.append((0, heading_indexes[0], "开场资料"))
        for position, start in enumerate(heading_indexes):
            end = heading_indexes[position + 1] if position + 1 < len(heading_indexes) else len(lines)
            raw_ranges.append((start, end, lines[start].strip()))
    else:
        raw_ranges = _fallback_ranges(lines, max_chars)

    pieces: List[Tuple[int, int, str]] = []
    for start, end, heading in raw_ranges:
        pieces.extend(_split_oversized_range(lines, start, end, heading, max_chars))

    scenes = []
    for order, (start, end, heading) in enumerate(pieces, start=1):
        while start < end and not lines[start].strip():
            start += 1
        while start < end and not lines[end - 1].strip():
            end -= 1
        body = "\n".join(lines[start:end])
        if not body:
            continue
        scene_id = "S{:04d}".format(len(scenes) + 1)
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        scenes.append(
            Scene(
                id=scene_id,
                order=len(scenes) + 1,
                heading=heading or "片段 {:03d}".format(order),
                start_line=start + 1,
                end_line=end,
                text=body,
                digest=digest,
            )
        )
    return scenes


def numbered_scene_text(scene: Scene) -> str:
    return "\n".join(
        "L{:06d} {}".format(scene.start_line + offset, line)
        for offset, line in enumerate(scene.text.split("\n"))
    )


def _fallback_ranges(lines: Sequence[str], max_chars: int) -> List[Tuple[int, int, str]]:
    ranges = []
    start = 0
    size = 0
    last_blank = None
    for index, line in enumerate(lines):
        size += len(line) + 1
        if not line.strip():
            last_blank = index + 1
        if size >= max_chars:
            end = last_blank if last_blank is not None and last_blank > start else index + 1
            ranges.append((start, end, "片段 {:03d}".format(len(ranges) + 1)))
            start = end
            size = sum(len(item) + 1 for item in lines[start : index + 1])
            last_blank = None
    if start < len(lines):
        ranges.append((start, len(lines), "片段 {:03d}".format(len(ranges) + 1)))
    return ranges


def _split_oversized_range(
    lines: Sequence[str], start: int, end: int, heading: str, max_chars: int
) -> Iterable[Tuple[int, int, str]]:
    if sum(len(line) + 1 for line in lines[start:end]) <= max_chars:
        return [(start, end, heading)]

    result = []
    part_start = start
    part = 1
    current_size = 0
    last_blank = None
    for index in range(start, end):
        current_size += len(lines[index]) + 1
        if not lines[index].strip():
            last_blank = index + 1
        if current_size >= max_chars:
            part_end = last_blank if last_blank is not None and last_blank > part_start else index + 1
            result.append((part_start, part_end, "{}（续 {}）".format(heading, part)))
            part += 1
            part_start = part_end
            current_size = sum(len(line) + 1 for line in lines[part_start : index + 1])
            last_blank = None
    if part_start < end:
        result.append((part_start, end, "{}（续 {}）".format(heading, part)))
    return result

