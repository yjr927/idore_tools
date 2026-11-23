"""Verify the public, two-character screenplay demo against its actual text."""

import argparse
import hashlib
import json
import re
from pathlib import Path


NAMES = {"阿禾", "林舟"}
SOURCE = Path(__file__).resolve().parents[1] / "site/script-agent/demo/source.txt"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def cited_ids(value):
    if isinstance(value, dict):
        yield from value.get("evidence_ids", [])
        for item in value.values():
            yield from cited_ids(item)
    elif isinstance(value, list):
        for item in value:
            yield from cited_ids(item)


def normalized_quote(value, speaker):
    value = re.sub(r"\s+", "", value)
    value = re.sub(r"^" + re.escape(speaker) + "[：:]", "", value)
    return value.strip("「」『』“”\"‘’'。，！？；")


def validate(report, source, model):
    meta = report["meta"]
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    require(meta["source_digest"] == digest, "Report belongs to a different screenplay")
    require(meta["extract_model"] == meta["synthesis_model"] == model, "Unexpected model")
    require(meta["analysis_depth"] == "full" and meta["partial_analysis"] is False,
            "A complete analysis is required")
    require(meta["scene_count_analyzed"] == meta["scene_count_total"] == 1, "Expected one scene")
    require(set(report["characters"]) >= NAMES, "Expected two named characters")
    expected_stages = {
        "scene_analysis": 1, "character_window": 2, "character_card": 2,
        "relationship_window": 1, "relationship_file": 1,
    }
    actual_stages = meta["openrouter_requests"]
    require(all(actual_stages.get(name, 0) >= count for name, count in expected_stages.items()),
            "Missing live OpenRouter requests for one or more stages")
    catalog = report["evidence_catalog"]
    verified = {}
    for name in sorted(NAMES):
        card = report["characters"][name]
        require(card.get("_meta", {}).get("profile_mode") == "full", "Missing full card: " + name)
        lines = [line.split("：", 1)[1] for line in source.splitlines() if line.startswith(name + "：")]
        spoken = set()
        for key, evidence in catalog.items():
            if evidence.get("speaker") != name or evidence.get("kind") != "台词":
                continue
            quote = normalized_quote(evidence.get("quote_or_action", ""), name)
            if (len(quote) >= 4 and key == evidence.get("evidence_id")
                    and evidence.get("scene_id")
                    and any(quote in normalized_quote(line, name) for line in lines)):
                spoken.add(key)
        require(spoken, "No verbatim dialogue found for " + name)
        require(set(cited_ids(card)) & spoken, "Card does not cite matching dialogue: " + name)
        verified[name] = len(spoken)
    pair = next((relationship for relationship in report["relationships"].values()
                 if {relationship["person_a"], relationship["person_b"]} == NAMES), None)
    require(pair and pair.get("_meta", {}).get("profile_mode") == "full", "Missing full relationship")
    require(set(cited_ids(pair)) & catalog.keys(), "Relationship has no traceable citation")
    return verified


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    matches = validate(report, SOURCE.read_text(encoding="utf-8-sig"), args.model)
    print("Verified full analysis of 三号站台 using " + args.model)
    print("One scene, two character cards and their relationship.")
    print("Verbatim dialogue evidence: " + ", ".join(f"{name} ({count})" for name, count in matches.items()))
    print("OpenRouter requests: " + str(sum(report["meta"]["openrouter_requests"].values())))
