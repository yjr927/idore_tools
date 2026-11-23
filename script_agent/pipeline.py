from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .openai_client import StructuredModelClient, default_model
from .prompts import (
    CHARACTER_CARD_INSTRUCTIONS,
    CHARACTER_WINDOW_INSTRUCTIONS,
    RELATIONSHIP_FILE_INSTRUCTIONS,
    RELATIONSHIP_WINDOW_INSTRUCTIONS,
    QUICK_SCRIPT_PREVIEW_INSTRUCTIONS,
    SCENE_ANALYST_INSTRUCTIONS,
)
from .readers import read_script
from .schemas import (
    CHARACTER_CARD_SCHEMA,
    CHARACTER_WINDOW_SCHEMA,
    RELATIONSHIP_FILE_SCHEMA,
    RELATIONSHIP_WINDOW_SCHEMA,
    QUICK_SCRIPT_PREVIEW_SCHEMA,
    SCENE_ANALYSIS_SCHEMA,
)
from .segmenter import Scene, numbered_scene_text, split_scenes


PROMPT_VERSION = "2026-11-openrouter.1"


@dataclass
class AnalysisConfig:
    extract_model: str = field(default_factory=default_model)
    synthesis_model: str = field(default_factory=default_model)
    window_scenes: int = 12
    max_output_tokens: int = 12000
    max_workers: int = 2
    fast_mode: bool = False
    force: bool = False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def prepare_script(script_path: Path, run_dir: Path, max_scene_chars: int = 14000) -> Dict[str, Any]:
    script_path = script_path.expanduser().resolve()
    run_dir = run_dir.expanduser().resolve()
    text = read_script(script_path)
    source_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    manifest_path = run_dir / "manifest.json"
    if manifest_path.exists():
        old_manifest = read_json(manifest_path)
        if old_manifest.get("source_digest") != source_digest:
            raise RuntimeError(
                "这个输出目录已经属于另一份剧本。为避免混合证据，请换一个新的 --output 目录。"
            )

    scenes = split_scenes(text, max_chars=max_scene_chars)
    if not scenes:
        raise RuntimeError("剧本没有产生任何可分析片段。")

    manifest = {
        "format_version": 1,
        "prompt_version": PROMPT_VERSION,
        "source_name": script_path.name,
        "source_path": str(script_path),
        "source_digest": source_digest,
        "scene_count": len(scenes),
        "max_scene_chars": max_scene_chars,
        "prepared_at": utc_now(),
        "privacy_note": "本目录含剧本文本和分析证据，请按公司保密要求保存。",
    }
    write_json(run_dir / "source" / "scenes.json", [scene.to_dict() for scene in scenes])
    write_json(manifest_path, manifest)
    return manifest


def load_scenes(run_dir: Path) -> List[Scene]:
    raw_scenes = read_json(run_dir / "source" / "scenes.json")
    return [Scene(**item) for item in raw_scenes]


def analyze_run(
    run_dir: Path,
    config: AnalysisConfig,
    client: Optional[StructuredModelClient] = None,
    progress: Optional[Callable[[str], None]] = None,
    scene_limit: Optional[int] = None,
) -> Dict[str, Any]:
    run_dir = run_dir.expanduser().resolve()
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise RuntimeError("输出目录还没有准备好，请先运行 prepare 或 analyze 并提供剧本文件。")
    manifest = read_json(manifest_path)
    all_scenes = load_scenes(run_dir)
    scenes = all_scenes[: max(1, scene_limit)] if scene_limit else all_scenes
    model_client = client or StructuredModelClient()
    notify = progress or (lambda message: None)

    notify("开始并行分析 {} 个场景（并发上限 {}）".format(len(scenes), config.max_workers))
    scene_analyses = _parallel_ordered(
        scenes,
        lambda scene: _analyze_scene(model_client, run_dir, scene, config),
        max_workers=config.max_workers,
        on_complete=lambda done, total, scene: notify(
            "逐场分析 {}/{}：{}（完成）".format(done, total, scene.heading)
        ),
    )

    alias_index = _build_alias_index(scene_analyses)
    evidence_catalog = _build_evidence_catalog(scene_analyses, scenes)
    character_records, relationship_records = _build_records(scene_analyses, alias_index)

    sorted_characters = sorted(character_records.items())
    if config.fast_mode:
        notify("正在本机生成 {} 个角色的证据预览".format(len(sorted_characters)))
    else:
        notify("开始并行汇总 {} 个角色".format(len(sorted_characters)))

    def analyze_character(item: Tuple[str, Sequence[Dict[str, Any]]]):
        character_name, records = item
        aliases = sorted(alias_index.get("canonical_aliases", {}).get(character_name, []))
        if config.fast_mode:
            windows = []
            card = _preview_character_card(character_name, aliases, records)
        elif not _collect_evidence_ids(records):
            windows: List[Dict[str, Any]] = []
            card = _limited_character_card(character_name, aliases, records)
        else:
            windows = _analyze_character_windows(
                model_client, run_dir, character_name, records, config
            )
            card = _synthesize_character(
                model_client,
                run_dir,
                character_name,
                aliases,
                windows,
                evidence_catalog,
                config,
            )
            card.setdefault("_meta", {})["profile_mode"] = "full"
        return character_name, windows, card

    character_results = _parallel_ordered(
        sorted_characters,
        analyze_character,
        max_workers=config.max_workers,
        on_complete=lambda done, total, item: notify(
            "汇总角色 {}/{}：{}（完成）".format(done, total, item[0])
        ),
    )
    character_windows = {name: windows for name, windows, _ in character_results}
    character_cards = {name: card for name, _, card in character_results}

    sorted_relationships = sorted(relationship_records.items())
    if config.fast_mode:
        notify("正在本机生成 {} 组关系的证据预览".format(len(sorted_relationships)))
    else:
        notify("开始并行汇总 {} 组人物关系".format(len(sorted_relationships)))

    def analyze_relationship(item: Tuple[Tuple[str, str], Sequence[Dict[str, Any]]]):
        pair, records = item
        pair_key = "{} × {}".format(pair[0], pair[1])
        if config.fast_mode:
            windows = []
            relationship = _preview_relationship_file(pair, records)
        elif not _collect_evidence_ids(records):
            windows: List[Dict[str, Any]] = []
            relationship = _limited_relationship_file(pair)
        else:
            windows = _analyze_relationship_windows(
                model_client, run_dir, pair, records, config
            )
            relationship = _synthesize_relationship(
                model_client,
                run_dir,
                pair,
                windows,
                character_cards,
                evidence_catalog,
                config,
            )
            relationship.setdefault("_meta", {})["profile_mode"] = "full"
        return pair_key, windows, relationship

    relationship_results = _parallel_ordered(
        sorted_relationships,
        analyze_relationship,
        max_workers=config.max_workers,
        on_complete=lambda done, total, item: notify(
            "汇总关系 {}/{}：{} × {}（完成）".format(
                done, total, item[0][0], item[0][1]
            )
        ),
    )
    relationship_windows = {key: windows for key, windows, _ in relationship_results}
    relationship_files = {key: value for key, _, value in relationship_results}

    knowledge_ledger = _build_knowledge_ledger(scene_analyses, alias_index)
    review_queue = _build_review_queue(character_cards, relationship_files)
    bundle = {
        "meta": {
            "source_name": manifest.get("source_name", ""),
            "source_digest": manifest.get("source_digest", ""),
            "prompt_version": PROMPT_VERSION,
            "extract_model": config.extract_model,
            "synthesis_model": config.synthesis_model,
            "generated_at": utc_now(),
            "scene_count_analyzed": len(scenes),
            "scene_count_total": len(all_scenes),
            "partial_analysis": len(scenes) < len(all_scenes),
            "fast_mode": config.fast_mode,
            "analysis_depth": "preview" if config.fast_mode else "full",
            "parallel_workers": config.max_workers,
            "review_warning": "模型生成内容不是正式设定；须由编剧或内容负责人确认。",
        },
        "characters": character_cards,
        "relationships": relationship_files,
        "knowledge_ledger": knowledge_ledger,
        "review_queue": review_queue,
        "evidence_catalog": evidence_catalog,
        "alias_index": alias_index,
    }
    if isinstance(model_client, StructuredModelClient):
        bundle["meta"]["openrouter_requests"] = model_client.call_counts()
    output_dir = run_dir / "output"
    write_json(output_dir / "report.json", bundle)
    write_json(output_dir / "knowledge-ledger.json", knowledge_ledger)
    write_json(output_dir / "review-queue.json", review_queue)

    from .report import render_all

    render_all(bundle, output_dir)
    notify("报告已生成：{}".format(output_dir / "report.html"))
    manifest["last_analysis"] = bundle["meta"]
    write_json(manifest_path, manifest)
    return bundle


def analyze_quick_preview(
    run_dir: Path,
    config: Optional[AnalysisConfig] = None,
    client: Optional[StructuredModelClient] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Create a deliberately small whole-script overview in one model call."""
    run_dir = run_dir.expanduser().resolve()
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise RuntimeError("输出目录还没有准备好，请先完成剧本切分。")

    manifest = read_json(manifest_path)
    scenes = load_scenes(run_dir)
    if not scenes:
        raise RuntimeError("没有可供快速概括的剧本场景。")
    resolved_config = config or AnalysisConfig(
        extract_model=default_model(),
        synthesis_model=default_model(),
        max_output_tokens=3000,
        max_workers=1,
        fast_mode=True,
    )
    model_client = client or StructuredModelClient()
    notify = progress or (lambda message: None)

    notify("正在读取全剧并压缩为快速概览")
    preview_input = _build_quick_preview_input(scenes)
    digest = json_digest(preview_input)
    cache_path = run_dir / "analysis" / "preview" / "overview.json"
    result = _read_cache(
        cache_path, digest, resolved_config.extract_model, resolved_config.force
    )
    if result is None:
        notify("正在生成一句话人物形象与简易关系图")
        result = model_client.generate(
            model=resolved_config.extract_model,
            instructions=QUICK_SCRIPT_PREVIEW_INSTRUCTIONS,
            input_text=json.dumps(preview_input, ensure_ascii=False),
            schema_name="quick_script_preview",
            schema=QUICK_SCRIPT_PREVIEW_SCHEMA,
            max_output_tokens=min(3500, resolved_config.max_output_tokens),
        )
        result = _write_cache(
            cache_path, result, digest, resolved_config.extract_model
        )
    else:
        notify("已找到快速概览缓存，正在整理页面")

    notify("正在本机整理角色与关系图")
    bundle = _quick_preview_bundle(result, manifest, scenes, preview_input)
    output_dir = run_dir / "output"
    write_json(output_dir / "report.json", bundle)
    write_json(output_dir / "knowledge-ledger.json", [])
    write_json(output_dir / "review-queue.json", bundle["review_queue"])

    from .report import render_all

    render_all(bundle, output_dir)
    notify("一分钟概览已生成：{}".format(output_dir / "report.html"))
    manifest["last_analysis"] = bundle["meta"]
    write_json(manifest_path, manifest)
    return bundle


def render_existing(run_dir: Path) -> Dict[str, Any]:
    run_dir = run_dir.expanduser().resolve()
    report_path = run_dir / "output" / "report.json"
    if not report_path.exists():
        raise RuntimeError("还没有 report.json，请先完成 analyze。")
    bundle = read_json(report_path)
    from .report import render_all

    render_all(bundle, run_dir / "output")
    return bundle


def analyze_character_profile(
    run_dir: Path,
    character_name: str,
    config: Optional[AnalysisConfig] = None,
    client: Optional[StructuredModelClient] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Upgrade one preview character to a full evidence-backed profile."""
    run_dir = run_dir.expanduser().resolve()
    report_path = run_dir / "output" / "report.json"
    if not report_path.exists():
        raise RuntimeError("请先完成快速分析，再生成单个角色的完整角色卡。")
    bundle = read_json(report_path)
    notify = progress or (lambda message: None)
    resolved_config = config or AnalysisConfig(
        extract_model=bundle.get("meta", {}).get("extract_model", default_model()),
        synthesis_model=default_model(),
        max_output_tokens=12000,
        max_workers=2,
    )
    model_client = client or StructuredModelClient()
    analyzed_count = int(bundle.get("meta", {}).get("scene_count_analyzed", 0) or 0)
    all_scenes = load_scenes(run_dir)
    if bundle.get("meta", {}).get("analysis_depth") == "quick_overview":
        scenes = all_scenes
    else:
        scenes = all_scenes[:analyzed_count]
    scene_analyses = []
    missing_scene_evidence = False
    for scene in scenes:
        path = run_dir / "analysis" / "scenes" / "{}.json".format(scene.id)
        if not path.exists():
            missing_scene_evidence = True
            break
    if missing_scene_evidence:
        notify("正在补充完整逐场证据；深度分析会比快速概览更久")
        scene_analyses = _parallel_ordered(
            scenes,
            lambda scene: _analyze_scene(model_client, run_dir, scene, resolved_config),
            max_workers=resolved_config.max_workers,
            on_complete=lambda done, total, scene: notify(
                "深度逐场取证 {}/{}：{}（完成）".format(done, total, scene.heading)
            ),
        )
    else:
        scene_analyses = [
            read_json(run_dir / "analysis" / "scenes" / "{}.json".format(scene.id))
            for scene in scenes
        ]

    notify("正在整理 {} 的逐场证据".format(character_name))
    alias_index = _build_alias_index(scene_analyses)
    evidence_catalog = _build_evidence_catalog(scene_analyses, scenes)
    character_records, _ = _build_records(scene_analyses, alias_index)
    canonical_name = _resolve_name(alias_index, character_name)
    records = character_records.get(canonical_name)
    if not records:
        raise RuntimeError("当前已分析场景中找不到这个角色的直接记录。")
    aliases = sorted(alias_index.get("canonical_aliases", {}).get(canonical_name, []))
    if not _collect_evidence_ids(records):
        card = _limited_character_card(canonical_name, aliases, records)
    else:
        notify("正在追踪 {} 的性格与变化阶段".format(canonical_name))
        windows = _analyze_character_windows(
            model_client, run_dir, canonical_name, records, resolved_config
        )
        notify("正在生成 {} 的完整角色卡".format(canonical_name))
        card = _synthesize_character(
            model_client,
            run_dir,
            canonical_name,
            aliases,
            windows,
            evidence_catalog,
            resolved_config,
        )
        card.setdefault("_meta", {})["profile_mode"] = "full"

    bundle.setdefault("characters", {})[canonical_name] = card
    bundle["review_queue"] = _build_review_queue(
        bundle.get("characters", {}), bundle.get("relationships", {})
    )
    bundle.setdefault("meta", {})["updated_at"] = utc_now()
    write_json(report_path, bundle)
    from .report import render_all

    notify("正在更新角色档案与审核队列")
    render_all(bundle, run_dir / "output")
    notify("{} 的完整角色卡已生成".format(canonical_name))
    return card


def _analyze_scene(
    client: StructuredModelClient,
    run_dir: Path,
    scene: Scene,
    config: AnalysisConfig,
) -> Dict[str, Any]:
    input_value = {
        "scene_id": scene.id,
        "heading": scene.heading,
        "order": scene.order,
        "source_lines": [scene.start_line, scene.end_line],
        "text_with_line_numbers": numbered_scene_text(scene),
    }
    input_digest = json_digest(input_value)
    cache_path = run_dir / "analysis" / "scenes" / "{}.json".format(scene.id)
    cached = _read_cache(cache_path, input_digest, config.extract_model, config.force)
    if cached is not None:
        return cached

    result = client.generate(
        model=config.extract_model,
        instructions=SCENE_ANALYST_INSTRUCTIONS,
        input_text=json.dumps(input_value, ensure_ascii=False),
        schema_name="scene_analysis",
        schema=SCENE_ANALYSIS_SCHEMA,
        max_output_tokens=config.max_output_tokens,
    )
    result["scene_id"] = scene.id
    result["heading"] = scene.heading
    _normalize_scene_evidence(result, scene)
    return _write_cache(cache_path, result, input_digest, config.extract_model)


def _analyze_character_windows(
    client: StructuredModelClient,
    run_dir: Path,
    character_name: str,
    records: Sequence[Dict[str, Any]],
    config: AnalysisConfig,
) -> List[Dict[str, Any]]:
    output = []
    for window_number, group in enumerate(_chunks(records, config.window_scenes), start=1):
        input_value = {"character_name": character_name, "chronological_records": group}
        input_digest = json_digest(input_value)
        path = (
            run_dir
            / "analysis"
            / "windows"
            / "characters"
            / safe_slug(character_name)
            / "W{:03d}.json".format(window_number)
        )
        cached = _read_cache(path, input_digest, config.synthesis_model, config.force)
        if cached is None:
            cached = client.generate(
                model=config.synthesis_model,
                instructions=CHARACTER_WINDOW_INSTRUCTIONS,
                input_text=json.dumps(input_value, ensure_ascii=False),
                schema_name="character_window",
                schema=CHARACTER_WINDOW_SCHEMA,
                max_output_tokens=config.max_output_tokens,
            )
            _resolve_citations(cached, {key: key for key in _collect_evidence_ids(group)})
            cached["character_name"] = character_name
            cached = _write_cache(path, cached, input_digest, config.synthesis_model)
        output.append(cached)
    return output


def _synthesize_character(
    client: StructuredModelClient,
    run_dir: Path,
    character_name: str,
    aliases: Sequence[str],
    windows: Sequence[Dict[str, Any]],
    evidence_catalog: Dict[str, Dict[str, Any]],
    config: AnalysisConfig,
) -> Dict[str, Any]:
    evidence_ids = _collect_evidence_ids(windows)
    input_value = {
        "character_name": character_name,
        "known_aliases": list(aliases),
        "chronological_phase_files": [_without_meta(item) for item in windows],
        "evidence_catalog": _select_evidence(evidence_catalog, evidence_ids),
    }
    digest = json_digest(input_value)
    path = run_dir / "analysis" / "characters" / "{}.json".format(safe_slug(character_name))
    cached = _read_cache(path, digest, config.synthesis_model, config.force)
    if cached is not None:
        return cached
    result = client.generate(
        model=config.synthesis_model,
        instructions=CHARACTER_CARD_INSTRUCTIONS,
        input_text=json.dumps(input_value, ensure_ascii=False),
        schema_name="character_card",
        schema=CHARACTER_CARD_SCHEMA,
        max_output_tokens=config.max_output_tokens,
    )
    result["character_name"] = character_name
    result["aliases"] = list(aliases)
    _resolve_citations(result, {key: key for key in input_value["evidence_catalog"]})
    _prevent_unapproved_canon(result)
    return _write_cache(path, result, digest, config.synthesis_model)


def _analyze_relationship_windows(
    client: StructuredModelClient,
    run_dir: Path,
    pair: Tuple[str, str],
    records: Sequence[Dict[str, Any]],
    config: AnalysisConfig,
) -> List[Dict[str, Any]]:
    output = []
    pair_slug = safe_slug("{}--{}".format(pair[0], pair[1]))
    for window_number, group in enumerate(_chunks(records, config.window_scenes), start=1):
        input_value = {
            "person_a": pair[0],
            "person_b": pair[1],
            "chronological_records": group,
        }
        digest = json_digest(input_value)
        path = (
            run_dir
            / "analysis"
            / "windows"
            / "relationships"
            / pair_slug
            / "W{:03d}.json".format(window_number)
        )
        cached = _read_cache(path, digest, config.synthesis_model, config.force)
        if cached is None:
            cached = client.generate(
                model=config.synthesis_model,
                instructions=RELATIONSHIP_WINDOW_INSTRUCTIONS,
                input_text=json.dumps(input_value, ensure_ascii=False),
                schema_name="relationship_window",
                schema=RELATIONSHIP_WINDOW_SCHEMA,
                max_output_tokens=config.max_output_tokens,
            )
            _resolve_citations(cached, {key: key for key in _collect_evidence_ids(group)})
            cached["person_a"], cached["person_b"] = pair
            cached = _write_cache(path, cached, digest, config.synthesis_model)
        output.append(cached)
    return output


def _synthesize_relationship(
    client: StructuredModelClient,
    run_dir: Path,
    pair: Tuple[str, str],
    windows: Sequence[Dict[str, Any]],
    character_cards: Dict[str, Dict[str, Any]],
    evidence_catalog: Dict[str, Dict[str, Any]],
    config: AnalysisConfig,
) -> Dict[str, Any]:
    evidence_ids = _collect_evidence_ids(windows)
    input_value = {
        "person_a": pair[0],
        "person_b": pair[1],
        "person_a_card": _card_context(character_cards.get(pair[0], {})),
        "person_b_card": _card_context(character_cards.get(pair[1], {})),
        "chronological_phase_files": [_without_meta(item) for item in windows],
        "evidence_catalog": _select_evidence(evidence_catalog, evidence_ids),
    }
    digest = json_digest(input_value)
    path = (
        run_dir
        / "analysis"
        / "relationships"
        / "{}.json".format(safe_slug("{}--{}".format(pair[0], pair[1])))
    )
    cached = _read_cache(path, digest, config.synthesis_model, config.force)
    if cached is not None:
        return cached
    result = client.generate(
        model=config.synthesis_model,
        instructions=RELATIONSHIP_FILE_INSTRUCTIONS,
        input_text=json.dumps(input_value, ensure_ascii=False),
        schema_name="relationship_file",
        schema=RELATIONSHIP_FILE_SCHEMA,
        max_output_tokens=config.max_output_tokens,
    )
    result["person_a"], result["person_b"] = pair
    _resolve_citations(result, {key: key for key in input_value["evidence_catalog"]})
    _prevent_unapproved_canon(result)
    return _write_cache(path, result, digest, config.synthesis_model)


def _write_cache(
    path: Path, value: Dict[str, Any], input_digest: str, model: str
) -> Dict[str, Any]:
    value["_meta"] = {
        "input_digest": input_digest,
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "generated_at": utc_now(),
    }
    write_json(path, value)
    return value


def _read_cache(
    path: Path, input_digest: str, model: str, force: bool
) -> Optional[Dict[str, Any]]:
    if force or not path.exists():
        return None
    value = read_json(path)
    meta = value.get("_meta", {})
    if (
        meta.get("input_digest") == input_digest
        and meta.get("model") == model
        and meta.get("prompt_version") == PROMPT_VERSION
    ):
        return value
    return None


def _evidence_is_located(evidence: Dict[str, Any], lines: Sequence[str], start_line: int, dialogue: bool) -> bool:
    match = re.fullmatch(r"L(\d+)(?:\s*[-–—]\s*L?(\d+))?", evidence["source_locator"].strip())
    if not match:
        return False
    first = int(match[1]) - start_line
    last = int(match[2] or match[1]) - start_line
    if not 0 <= first <= last < len(lines):
        return False
    excerpt = evidence["quote_or_action"].strip()
    if not excerpt:
        return False
    if not dialogue and evidence["kind"] != "台词":
        return True
    excerpt = excerpt.strip('“”「」『』"').strip()
    return bool(excerpt) and excerpt in "\n".join(lines[first : last + 1])


def _normalize_scene_evidence(analysis: Dict[str, Any], scene: Scene) -> None:
    lines = scene.text.split("\n")
    mapping = {}
    duplicate_ids = set()
    counter = 0
    rejected = 0
    for character in analysis.get("characters", []):
        for field in ("explicit_words", "actions", "other_people_views"):
            grounded = []
            for evidence in character.get(field, []):
                if not _evidence_is_located(evidence, lines, scene.start_line, field == "explicit_words"):
                    rejected += 1
                    continue
                counter += 1
                new_id = "{}-E{:03d}".format(scene.id, counter)
                old_id = evidence.get("evidence_id", "")
                if old_id in mapping:
                    duplicate_ids.add(old_id)
                if old_id:
                    mapping[old_id] = new_id
                evidence["evidence_id"] = new_id
                evidence["scene_id"] = scene.id
                grounded.append(evidence)
            character[field] = grounded
    if rejected:
        analysis.setdefault("unresolved_questions", []).append(
            "有 {} 条证据的引文或行号无法与原文匹配，已排除。".format(rejected)
        )
    for old_id in duplicate_ids:
        del mapping[old_id]
    _resolve_citations(analysis, mapping)


def _resolve_citations(value: Any, allowed: Dict[str, str]) -> None:
    if isinstance(value, list):
        for child in value:
            _resolve_citations(child, allowed)
    elif isinstance(value, dict):
        for key in ("evidence_ids", "counterevidence_ids"):
            if key in value:
                value[key] = [allowed[item] for item in value[key] if item in allowed]
        if "claim" in value and not value.get("evidence_ids"):
            value["review_status"] = "待确认"
        for child in value.values():
            _resolve_citations(child, allowed)


class _UnionFind:
    def __init__(self):
        self.parent = {}

    def add(self, item: str) -> None:
        self.parent.setdefault(item, item)

    def find(self, item: str) -> str:
        self.add(item)
        if self.parent[item] != item:
            self.parent[item] = self.find(self.parent[item])
        return self.parent[item]

    def union(self, left: str, right: str) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def _name_key(name: str) -> str:
    return re.sub(r"\s+", "", name).casefold()


def _build_alias_index(scene_analyses: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    union = _UnionFind()
    labels = defaultdict(Counter)
    primary_counts = Counter()
    for analysis in scene_analyses:
        for character in analysis.get("characters", []):
            name = character.get("name", "").strip()
            if not name:
                continue
            key = _name_key(name)
            union.add(key)
            labels[key][name] += 1
            primary_counts[name] += 1
            for alias in character.get("aliases", []):
                alias = alias.strip()
                if not alias:
                    continue
                alias_key = _name_key(alias)
                union.union(key, alias_key)
                labels[alias_key][alias] += 1

    groups = defaultdict(set)
    for key in union.parent:
        groups[union.find(key)].add(key)

    name_to_canonical = {}
    canonical_aliases = {}
    for keys in groups.values():
        candidates = []
        for key in keys:
            candidates.extend(labels[key].keys())
        candidates = list(dict.fromkeys(candidates))
        canonical = max(candidates, key=lambda name: (primary_counts[name], -len(name), name))
        aliases = sorted(name for name in candidates if name != canonical)
        canonical_aliases[canonical] = aliases
        for key in keys:
            for label in labels[key]:
                name_to_canonical[label] = canonical
            name_to_canonical[key] = canonical
    return {
        "name_to_canonical": name_to_canonical,
        "canonical_aliases": canonical_aliases,
    }


def _resolve_name(alias_index: Dict[str, Any], name: str) -> str:
    mapping = alias_index.get("name_to_canonical", {})
    return mapping.get(name, mapping.get(_name_key(name), name))


def _build_records(
    scene_analyses: Sequence[Dict[str, Any]], alias_index: Dict[str, Any]
) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[Tuple[str, str], List[Dict[str, Any]]]]:
    characters = defaultdict(list)
    relationships = defaultdict(list)
    for analysis in scene_analyses:
        base = {
            "scene_id": analysis.get("scene_id", ""),
            "heading": analysis.get("heading", ""),
            "synopsis": analysis.get("synopsis", ""),
            "chronology_marker": analysis.get("chronology_marker", ""),
        }
        scene_characters = {}
        for character in analysis.get("characters", []):
            canonical = _resolve_name(alias_index, character.get("name", ""))
            scene_characters[canonical] = character
            characters[canonical].append({**base, "character_observation": character})
        for relationship in analysis.get("relationships", []):
            a = _resolve_name(alias_index, relationship.get("person_a", ""))
            b = _resolve_name(alias_index, relationship.get("person_b", ""))
            if not a or not b or a == b:
                continue
            pair = tuple(sorted((a, b)))
            normalized_relationship = dict(relationship)
            normalized_relationship["person_a"] = pair[0]
            normalized_relationship["person_b"] = pair[1]
            if a != pair[0]:
                normalized_relationship["a_view_of_b"], normalized_relationship["b_view_of_a"] = (
                    relationship.get("b_view_of_a", ""),
                    relationship.get("a_view_of_b", ""),
                )
                normalized_relationship["a_wants"], normalized_relationship["b_wants"] = (
                    relationship.get("b_wants", ""),
                    relationship.get("a_wants", ""),
                )
            relationships[pair].append(
                {
                    **base,
                    "relationship_observation": normalized_relationship,
                    "person_a_observation": scene_characters.get(pair[0], {}),
                    "person_b_observation": scene_characters.get(pair[1], {}),
                    "story_facts": analysis.get("story_facts", []),
                }
            )
    return dict(characters), dict(relationships)


def _build_evidence_catalog(
    scene_analyses: Sequence[Dict[str, Any]], scenes: Sequence[Scene]
) -> Dict[str, Dict[str, Any]]:
    scene_lookup = {scene.id: scene for scene in scenes}
    catalog = {}
    for analysis in scene_analyses:
        scene_id = analysis.get("scene_id", "")
        scene = scene_lookup.get(scene_id)
        for character in analysis.get("characters", []):
            for field in ("explicit_words", "actions", "other_people_views"):
                for evidence in character.get(field, []):
                    evidence_id = evidence.get("evidence_id", "")
                    if evidence_id:
                        catalog[evidence_id] = {
                            **evidence,
                            "character": character.get("name", ""),
                            "heading": analysis.get("heading", ""),
                            "scene_order": scene.order if scene else 0,
                        }
    return catalog


def _build_knowledge_ledger(
    scene_analyses: Sequence[Dict[str, Any]], alias_index: Dict[str, Any]
) -> List[Dict[str, Any]]:
    ledger = []
    for analysis in scene_analyses:
        for fact in analysis.get("story_facts", []):
            item = dict(fact)
            item["scene_id"] = analysis.get("scene_id", "")
            item["heading"] = analysis.get("heading", "")
            item["known_by"] = [
                _resolve_name(alias_index, name) for name in fact.get("known_by", [])
            ]
            item["not_known_by"] = [
                _resolve_name(alias_index, name) for name in fact.get("not_known_by", [])
            ]
            ledger.append(item)
    return ledger


def _build_review_queue(
    character_cards: Dict[str, Dict[str, Any]],
    relationship_files: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    queue = []

    def visit(value: Any, owner_type: str, owner: str, path: str = "") -> None:
        if isinstance(value, dict):
            if "claim" in value and "review_status" in value:
                queue.append(
                    {
                        "owner_type": owner_type,
                        "owner": owner,
                        "path": path,
                        "claim": value.get("claim", ""),
                        "suggested_status": value.get("review_status", "待确认"),
                        "confidence": value.get("confidence", 0),
                        "evidence_ids": value.get("evidence_ids", []),
                        "editor_decision": "",
                        "editor_notes": "",
                    }
                )
            for key, child in value.items():
                if key != "_meta":
                    visit(child, owner_type, owner, "{}.{}".format(path, key).strip("."))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, owner_type, owner, "{}[{}]".format(path, index))

    for owner, card in character_cards.items():
        visit(card, "角色", owner)
        for question in card.get("unconfirmed_questions", []):
            queue.append(
                {
                    "owner_type": "角色",
                    "owner": owner,
                    "path": "unconfirmed_questions",
                    "claim": question,
                    "suggested_status": "待确认",
                    "confidence": 0,
                    "evidence_ids": [],
                    "editor_decision": "",
                    "editor_notes": "",
                }
            )
    for owner, relationship in relationship_files.items():
        visit(relationship, "关系", owner)
        for question in relationship.get("unresolved_questions", []):
            queue.append(
                {
                    "owner_type": "关系",
                    "owner": owner,
                    "path": "unresolved_questions",
                    "claim": question,
                    "suggested_status": "待确认",
                    "confidence": 0,
                    "evidence_ids": [],
                    "editor_decision": "",
                    "editor_notes": "",
                }
            )
    return queue


def _prevent_unapproved_canon(value: Any) -> None:
    if isinstance(value, dict):
        if value.get("review_status") == "正式设定":
            value["review_status"] = "人物理解"
        for child in value.values():
            _prevent_unapproved_canon(child)
    elif isinstance(value, list):
        for child in value:
            _prevent_unapproved_canon(child)


def _collect_evidence_ids(value: Any) -> Set[str]:
    found = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "evidence_id" and isinstance(child, str) and child:
                found.add(child)
            elif key in ("evidence_ids", "counterevidence_ids") and isinstance(child, list):
                found.update(item for item in child if isinstance(item, str))
            else:
                found.update(_collect_evidence_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_collect_evidence_ids(child))
    return found


def _select_evidence(
    catalog: Dict[str, Dict[str, Any]], evidence_ids: Iterable[str]
) -> Dict[str, Dict[str, Any]]:
    return {evidence_id: catalog[evidence_id] for evidence_id in sorted(evidence_ids) if evidence_id in catalog}


def _without_meta(value: Dict[str, Any]) -> Dict[str, Any]:
    return {key: child for key, child in value.items() if key != "_meta"}


def _card_context(card: Dict[str, Any]) -> Dict[str, Any]:
    allowed = {
        "character_name",
        "one_line_positioning",
        "deeper_personality_and_needs",
        "conflict_pattern",
        "intimacy_pattern",
        "central_contradictions",
        "will_not_say_or_do",
        "arc",
    }
    return {key: value for key, value in card.items() if key in allowed}


def _parallel_ordered(
    items: Sequence[Any],
    worker: Callable[[Any], Any],
    *,
    max_workers: int,
    on_complete: Optional[Callable[[int, int, Any], None]] = None,
) -> List[Any]:
    """Run independent tasks concurrently while preserving their input order."""
    values = list(items)
    if not values:
        return []
    worker_count = max(1, min(int(max_workers or 1), len(values)))
    if worker_count == 1:
        results = []
        for done, item in enumerate(values, start=1):
            results.append(worker(item))
            if on_complete:
                on_complete(done, len(values), item)
        return results

    results: List[Any] = [None] * len(values)
    with ThreadPoolExecutor(
        max_workers=worker_count, thread_name_prefix="script-pipeline"
    ) as executor:
        futures = {
            executor.submit(worker, item): (index, item)
            for index, item in enumerate(values)
        }
        completed = 0
        try:
            for future in as_completed(futures):
                index, item = futures[future]
                results[index] = future.result()
                completed += 1
                if on_complete:
                    on_complete(completed, len(values), item)
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    return results


def _fast_character_phase(
    character_name: str, records: Sequence[Dict[str, Any]]
) -> Dict[str, Any]:
    """Pass scene records straight to final synthesis in preview mode."""
    return {
        "character_name": character_name,
        "phase_label": "快速测试范围",
        "start_scene": records[0].get("scene_id", "") if records else "",
        "end_scene": records[-1].get("scene_id", "") if records else "",
        "chronological_records": list(records),
    }


def _fast_relationship_phase(
    pair: Tuple[str, str], records: Sequence[Dict[str, Any]]
) -> Dict[str, Any]:
    """Pass relationship records straight to final synthesis in preview mode."""
    return {
        "person_a": pair[0],
        "person_b": pair[1],
        "phase_label": "快速测试范围",
        "start_scene": records[0].get("scene_id", "") if records else "",
        "end_scene": records[-1].get("scene_id", "") if records else "",
        "chronological_records": list(records),
    }


def _pending_assertion(claim: str) -> Dict[str, Any]:
    return {
        "claim": claim,
        "review_status": "待确认",
        "confidence": 0,
        "evidence_ids": [],
        "counterevidence_ids": [],
        "notes": "当前范围没有可定位的直接证据。",
    }


def _character_evidence_ids(observation: Dict[str, Any]) -> List[str]:
    evidence_ids = []
    for field in ("explicit_words", "actions", "other_people_views"):
        for evidence in observation.get(field, []):
            evidence_id = evidence.get("evidence_id", "")
            if evidence_id and evidence_id not in evidence_ids:
                evidence_ids.append(evidence_id)
    return evidence_ids


def _preview_character_assertions(
    records: Sequence[Dict[str, Any]],
    field: str,
    *,
    maximum: int = 3,
) -> List[Dict[str, Any]]:
    output = []
    seen = set()
    for record in records:
        observation = record.get("character_observation", {})
        raw_value = observation.get(field, [])
        values = raw_value if isinstance(raw_value, list) else [raw_value]
        evidence_ids = _character_evidence_ids(observation)
        for value in values:
            claim = str(value or "").strip()
            if not claim or claim in {"无", "未明确", "待确认", "无法判断"} or claim in seen:
                continue
            seen.add(claim)
            status = observation.get("review_status", "人物理解")
            if status == "正式设定":
                status = "人物理解"
            if not evidence_ids:
                status = "待确认"
            output.append(
                {
                    "claim": claim,
                    "review_status": status,
                    "confidence": min(float(observation.get("confidence", 0.5) or 0.5), 0.85),
                    "evidence_ids": evidence_ids,
                    "counterevidence_ids": [],
                    "notes": "快速预览结论；生成完整角色卡后再确认是否为稳定特征。",
                }
            )
            if len(output) >= maximum:
                return output
    return output


def _build_quick_preview_input(
    scenes: Sequence[Scene], max_total_chars: int = 80000
) -> Dict[str, Any]:
    """Keep every scene represented while bounding the single preview request."""
    per_scene = max(360, min(6000, max_total_chars // max(1, len(scenes))))
    items = []
    truncated = False
    for scene in scenes:
        text = scene.text.strip()
        if len(text) > per_scene:
            truncated = True
            head = max(220, int(per_scene * 0.68))
            tail = max(100, per_scene - head - 20)
            text = "{}\n……[中间省略]……\n{}".format(text[:head], text[-tail:])
        items.append(
            {
                "scene_id": scene.id,
                "heading": scene.heading,
                "text_excerpt": text,
            }
        )
    return {
        "task": "生成全剧的一分钟角色概览",
        "scene_count": len(scenes),
        "sampling_note": (
            "长场景已保留开头与结尾片段，结论仅作快速导航。"
            if truncated
            else "已提供全部场景文本。"
        ),
        "scenes": items,
    }


def _quick_assertion(
    claim: str,
    scene_ids: Sequence[str],
    confidence: float,
    note: str = "快速概览结论；点击深度分析后再补充逐场证据与变化线。",
) -> Dict[str, Any]:
    return {
        "claim": claim.strip() or "待确认",
        "review_status": "人物理解" if scene_ids else "待确认",
        "confidence": min(max(float(confidence or 0), 0), 0.85),
        "evidence_ids": list(dict.fromkeys(scene_ids))[:3],
        "counterevidence_ids": [],
        "notes": note,
    }


def _quick_character_card(
    item: Dict[str, Any], valid_scene_ids: Set[str]
) -> Dict[str, Any]:
    name = str(item.get("name", "")).strip()
    scenes = [
        scene_id
        for scene_id in item.get("evidence_scene_ids", [])
        if scene_id in valid_scene_ids
    ]
    claim = str(item.get("one_line_portrait", "")).strip() or "待确认"
    assertion = _quick_assertion(claim, scenes, item.get("confidence", 0.5))
    return {
        "character_name": name,
        "aliases": [
            str(alias).strip()
            for alias in item.get("aliases", [])
            if str(alias).strip() and str(alias).strip() != name
        ],
        "one_line_positioning": [assertion],
        "first_impression": [dict(assertion)],
        "deeper_personality_and_needs": [],
        "most_wanted": [],
        "greatest_fears": [],
        "conflict_pattern": [],
        "intimacy_pattern": [],
        "central_contradictions": [],
        "speech_rhythm_wording_emotion": [],
        "will_not_say_or_do": [],
        "arc": {
            "starting_state": "深度分析后补充",
            "stages": [],
            "turning_points": [],
            "ending_state": "深度分析后补充",
            "overall_change": "快速版暂不判断角色变化。",
            "change_confirmed": False,
        },
        "poster_brief": {
            "first_glance_message": "快速版不生成视觉设定。",
            "expression": [],
            "posture_and_action": [],
            "clothing_and_props": [],
            "color_and_light": [],
            "copy_tone": [],
            "avoid_mismatches": [],
            "note": "完成深度角色卡后再生成视觉建议。",
        },
        "unconfirmed_questions": ["这个初步印象是否会在后续剧情中发生变化？"],
        "_meta": {
            "model": "quick-script-preview",
            "profile_mode": "preview",
            "prompt_version": PROMPT_VERSION,
            "generated_at": utc_now(),
        },
    }


def _quick_relationship_file(
    item: Dict[str, Any], pair: Tuple[str, str], valid_scene_ids: Set[str]
) -> Dict[str, Any]:
    scenes = [
        scene_id
        for scene_id in item.get("evidence_scene_ids", [])
        if scene_id in valid_scene_ids
    ]
    label = str(item.get("relation_label", "")).strip() or "待确认"
    summary = str(item.get("one_line_summary", "")).strip() or label
    assertion = _quick_assertion(summary, scenes, item.get("confidence", 0.5))
    surface = _quick_assertion(label, scenes, item.get("confidence", 0.5))
    return {
        "person_a": pair[0],
        "person_b": pair[1],
        "how_they_met": [],
        "surface_relation": [surface],
        "underlying_state": [assertion],
        "a_view_of_b": [],
        "b_view_of_a": [],
        "a_wants": [],
        "b_wants": [],
        "initiative_and_control": [],
        "important_shared_events": [],
        "current_conflict": [],
        "unspoken_information": [],
        "timeline": [
            {
                "stage": label,
                "state": summary,
                "change_event": "深度分析后补充",
                "scene_ids": scenes,
                "evidence_ids": scenes,
            }
        ],
        "current_stage": label,
        "story_truth_vs_audience_view": [],
        "unresolved_questions": ["双方视角、权力变化与未说出口的信息待深度分析。"],
        "_meta": {
            "model": "quick-script-preview",
            "profile_mode": "preview",
            "prompt_version": PROMPT_VERSION,
            "generated_at": utc_now(),
        },
    }


def _quick_preview_bundle(
    result: Dict[str, Any],
    manifest: Dict[str, Any],
    scenes: Sequence[Scene],
    preview_input: Dict[str, Any],
) -> Dict[str, Any]:
    valid_scene_ids = {scene.id for scene in scenes}
    characters: Dict[str, Dict[str, Any]] = {}
    alias_index = {"name_to_canonical": {}, "canonical_aliases": {}}
    for item in result.get("characters", [])[:15]:
        name = str(item.get("name", "")).strip()
        if not name or name in characters:
            continue
        card = _quick_character_card(item, valid_scene_ids)
        characters[name] = card
        alias_index["name_to_canonical"][name] = name
        alias_index["name_to_canonical"][_name_key(name)] = name
        alias_index["canonical_aliases"][name] = card["aliases"]
        for alias in card["aliases"]:
            alias_index["name_to_canonical"][alias] = name
            alias_index["name_to_canonical"][_name_key(alias)] = name

    relationships: Dict[str, Dict[str, Any]] = {}
    for item in result.get("relationships", [])[:20]:
        a = _resolve_name(alias_index, str(item.get("person_a", "")).strip())
        b = _resolve_name(alias_index, str(item.get("person_b", "")).strip())
        if not a or not b or a == b:
            continue
        for name in (a, b):
            if name not in characters:
                fallback = {
                    "name": name,
                    "aliases": [],
                    "one_line_portrait": "人物信息待确认",
                    "evidence_scene_ids": item.get("evidence_scene_ids", []),
                    "confidence": 0.25,
                }
                characters[name] = _quick_character_card(fallback, valid_scene_ids)
        pair = tuple(sorted((a, b)))
        key = "{} × {}".format(pair[0], pair[1])
        candidate = _quick_relationship_file(item, pair, valid_scene_ids)
        previous = relationships.get(key)
        if previous is None or float(item.get("confidence", 0)) > float(
            previous.get("underlying_state", [{}])[0].get("confidence", 0)
        ):
            relationships[key] = candidate

    review_queue = _build_review_queue(characters, relationships)
    supplied_all_text = preview_input.get("sampling_note") == "已提供全部场景文本。"
    bundle = {
        "meta": {
            "source_name": manifest.get("source_name", ""),
            "source_digest": manifest.get("source_digest", ""),
            "prompt_version": PROMPT_VERSION,
            "extract_model": result.get("_meta", {}).get("model", default_model()),
            "synthesis_model": "not-used",
            "generated_at": utc_now(),
            "scene_count_analyzed": len(scenes),
            "scene_count_total": len(scenes),
            "partial_analysis": False,
            "fast_mode": True,
            "analysis_depth": "quick_overview",
            "preview_sampling": not supplied_all_text,
            "review_warning": "这是快速概览，不是正式设定；深层性格、变化与信息差需继续分析并由编剧确认。",
        },
        "characters": characters,
        "relationships": relationships,
        "knowledge_ledger": [],
        "review_queue": review_queue,
        "evidence_catalog": {},
        "alias_index": alias_index,
    }
    return bundle


def _preview_character_card(
    character_name: str,
    aliases: Sequence[str],
    records: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    if not _collect_evidence_ids(records):
        return _limited_character_card(character_name, aliases, records)

    stages = []
    turning_points = []
    for record in records:
        observation = record.get("character_observation", {})
        evidence_ids = _character_evidence_ids(observation)
        state = (
            observation.get("current_emotion")
            or observation.get("surface_impression")
            or "本场状态待确认"
        )
        actions = observation.get("actions", [])
        words = observation.get("explicit_words", [])
        key_evidence = (actions or words or [{}])[0]
        possible_changes = observation.get("possible_changes", [])
        change_status = (
            possible_changes[0].get("status", "待确认")
            if possible_changes
            else "待确认"
        )
        stages.append(
            {
                "stage": record.get("scene_id", "当前片段"),
                "state": state,
                "key_behavior": key_evidence.get("quote_or_action", ""),
                "change_status": change_status,
                "evidence_ids": evidence_ids,
            }
        )
        for change in possible_changes:
            if change.get("status") not in {"潜在变化", "确认变化"}:
                continue
            turning_points.append(
                {
                    "scene_id": record.get("scene_id", ""),
                    "trigger": change.get("trigger", ""),
                    "before": change.get("before", ""),
                    "after": change.get("after", ""),
                    "confidence": min(float(change.get("confidence", 0.5) or 0.5), 0.85),
                    "evidence_ids": change.get("evidence_ids", evidence_ids),
                }
            )

    starting_state = stages[0]["state"] if stages else "待确认"
    ending_state = stages[-1]["state"] if stages else "待确认"
    overall_change = "当前范围尚未确认稳定变化。"
    if len(stages) > 1 and starting_state != ending_state:
        overall_change = "{} → {}（是否为稳定变化待深入分析）".format(
            starting_state, ending_state
        )

    surface = _preview_character_assertions(records, "surface_impression", maximum=2)
    positioning = surface[:1] or _preview_character_assertions(records, "current_emotion", maximum=1)
    return {
        "character_name": character_name,
        "aliases": list(aliases),
        "one_line_positioning": positioning,
        "first_impression": surface,
        "deeper_personality_and_needs": _preview_character_assertions(records, "deeper_need"),
        "most_wanted": _preview_character_assertions(records, "current_want"),
        "greatest_fears": _preview_character_assertions(records, "current_fear"),
        "conflict_pattern": _preview_character_assertions(records, "conflict_response"),
        "intimacy_pattern": _preview_character_assertions(records, "intimacy_pattern"),
        "central_contradictions": _preview_character_assertions(records, "possible_contradictions"),
        "speech_rhythm_wording_emotion": _preview_character_assertions(records, "speech_style"),
        "will_not_say_or_do": _preview_character_assertions(records, "will_not_say_or_do"),
        "arc": {
            "starting_state": starting_state,
            "stages": stages,
            "turning_points": turning_points,
            "ending_state": ending_state,
            "overall_change": overall_change,
            "change_confirmed": any(
                change.get("status") == "确认变化"
                for record in records
                for change in record.get("character_observation", {}).get("possible_changes", [])
            ),
        },
        "poster_brief": {
            "first_glance_message": "快速预览不生成视觉设定。",
            "expression": [],
            "posture_and_action": [],
            "clothing_and_props": [],
            "color_and_light": [],
            "copy_tone": [],
            "avoid_mismatches": [],
            "note": "请先针对该角色生成完整角色卡。",
        },
        "unconfirmed_questions": ["这些逐场表现是稳定性格，还是当下情境反应？"],
        "_meta": {
            "model": "local-evidence-preview",
            "profile_mode": "preview",
            "prompt_version": PROMPT_VERSION,
            "generated_at": utc_now(),
        },
    }


def _preview_relationship_assertions(
    records: Sequence[Dict[str, Any]], field: str, *, maximum: int = 3
) -> List[Dict[str, Any]]:
    output = []
    seen = set()
    for record in records:
        observation = record.get("relationship_observation", {})
        raw_value = observation.get(field, [])
        values = raw_value if isinstance(raw_value, list) else [raw_value]
        evidence_ids = [
            item for item in observation.get("evidence_ids", []) if isinstance(item, str)
        ]
        for value in values:
            claim = str(value or "").strip()
            if not claim or claim in {"无", "未明确", "待确认", "无法判断"} or claim in seen:
                continue
            seen.add(claim)
            output.append(
                {
                    "claim": claim,
                    "review_status": "人物理解" if evidence_ids else "待确认",
                    "confidence": min(float(observation.get("confidence", 0.5) or 0.5), 0.85),
                    "evidence_ids": evidence_ids,
                    "counterevidence_ids": [],
                    "notes": "快速预览结论；仅代表当前已分析场景。",
                }
            )
            if len(output) >= maximum:
                return output
    return output


def _preview_relationship_file(
    pair: Tuple[str, str], records: Sequence[Dict[str, Any]]
) -> Dict[str, Any]:
    if not _collect_evidence_ids(records):
        return _limited_relationship_file(pair)
    timeline = []
    for record in records:
        observation = record.get("relationship_observation", {})
        evidence_ids = observation.get("evidence_ids", [])
        state = observation.get("underlying_state") or observation.get("surface_relation")
        if not state and not observation.get("change"):
            continue
        timeline.append(
            {
                "stage": observation.get("stage") or record.get("scene_id", "当前阶段"),
                "state": state or "待确认",
                "change_event": observation.get("change", ""),
                "scene_ids": [record.get("scene_id", "")],
                "evidence_ids": evidence_ids,
            }
        )
    last_stage = next(
        (
            record.get("relationship_observation", {}).get("stage")
            for record in reversed(records)
            if record.get("relationship_observation", {}).get("stage")
        ),
        "待确认",
    )
    first_record = records[0] if records else {}
    first_evidence = first_record.get("relationship_observation", {}).get("evidence_ids", [])
    how_met = []
    if first_record:
        how_met = [
            {
                "claim": "最早在 {} 出现可定位的直接关系记录。".format(
                    first_record.get("scene_id", "当前片段")
                ),
                "review_status": "人物理解" if first_evidence else "待确认",
                "confidence": 0.65 if first_evidence else 0,
                "evidence_ids": first_evidence,
                "counterevidence_ids": [],
                "notes": "这是当前分析范围内的最早记录，不代表两人故事中的初次相识。",
            }
        ]
    return {
        "person_a": pair[0],
        "person_b": pair[1],
        "how_they_met": how_met,
        "surface_relation": _preview_relationship_assertions(records, "surface_relation"),
        "underlying_state": _preview_relationship_assertions(records, "underlying_state"),
        "a_view_of_b": _preview_relationship_assertions(records, "a_view_of_b"),
        "b_view_of_a": _preview_relationship_assertions(records, "b_view_of_a"),
        "a_wants": _preview_relationship_assertions(records, "a_wants"),
        "b_wants": _preview_relationship_assertions(records, "b_wants"),
        "initiative_and_control": (
            _preview_relationship_assertions(records, "initiative", maximum=2)
            + _preview_relationship_assertions(records, "power_balance", maximum=2)
        )[:3],
        "important_shared_events": _preview_relationship_assertions(records, "change"),
        "current_conflict": _preview_relationship_assertions(records, "tension"),
        "unspoken_information": _preview_relationship_assertions(records, "hidden_information"),
        "timeline": timeline,
        "current_stage": last_stage,
        "story_truth_vs_audience_view": [],
        "unresolved_questions": ["快速预览只能确认已出现的关系线索，深层动机与后续变化仍待确认。"],
        "_meta": {
            "model": "local-evidence-preview",
            "profile_mode": "preview",
            "prompt_version": PROMPT_VERSION,
            "generated_at": utc_now(),
        },
    }


def _limited_character_card(
    character_name: str,
    aliases: Sequence[str],
    records: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    observations = [
        record.get("character_observation", {})
        for record in records
        if record.get("character_observation")
    ]
    is_present = any(item.get("is_present") for item in observations)
    reason = "只被其他人提及，还没有直接出场。"
    if is_present:
        reason = "虽然出场，但当前范围没有可定位的言行证据。"
    empty_poster = {
        "first_glance_message": "待补充直接证据后再设计。",
        "expression": [],
        "posture_and_action": [],
        "clothing_and_props": [],
        "color_and_light": [],
        "copy_tone": [],
        "avoid_mismatches": [],
        "note": "不根据间接提及自行补全视觉设定。",
    }
    return {
        "character_name": character_name,
        "aliases": list(aliases),
        "one_line_positioning": [_pending_assertion(reason)],
        "first_impression": [_pending_assertion("暂时无法确认。")],
        "deeper_personality_and_needs": [],
        "most_wanted": [],
        "greatest_fears": [],
        "conflict_pattern": [],
        "intimacy_pattern": [],
        "central_contradictions": [],
        "speech_rhythm_wording_emotion": [],
        "will_not_say_or_do": [],
        "arc": {
            "starting_state": "待确认",
            "stages": [],
            "turning_points": [],
            "ending_state": "待确认",
            "overall_change": "证据不足，暂时无法判断变化。",
            "change_confirmed": False,
        },
        "poster_brief": empty_poster,
        "unconfirmed_questions": ["该角色何时直接出场，并做出可观察的选择？"],
        "_meta": {
            "model": "local-limited-profile",
            "profile_mode": "limited",
            "prompt_version": PROMPT_VERSION,
            "generated_at": utc_now(),
        },
    }


def _limited_relationship_file(pair: Tuple[str, str]) -> Dict[str, Any]:
    return {
        "person_a": pair[0],
        "person_b": pair[1],
        "how_they_met": [],
        "surface_relation": [_pending_assertion("暂时无法确认。")],
        "underlying_state": [],
        "a_view_of_b": [],
        "b_view_of_a": [],
        "a_wants": [],
        "b_wants": [],
        "initiative_and_control": [],
        "important_shared_events": [],
        "current_conflict": [],
        "unspoken_information": [],
        "timeline": [],
        "current_stage": "待确认",
        "story_truth_vs_audience_view": [],
        "unresolved_questions": ["两人是否有直接互动，以及他们分别如何理解这段关系？"],
        "_meta": {
            "model": "local-limited-profile",
            "profile_mode": "limited",
            "prompt_version": PROMPT_VERSION,
            "generated_at": utc_now(),
        },
    }


def _chunks(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    size = max(1, size)
    for start in range(0, len(items), size):
        yield items[start : start + size]


def safe_slug(value: str) -> str:
    ascii_part = re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-").lower()
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]
    return "{}-{}".format(ascii_part[:40], digest) if ascii_part else "item-{}".format(digest)
