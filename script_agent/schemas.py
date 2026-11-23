"""Strict JSON schemas used by the script analysis pipeline."""

from __future__ import annotations

from copy import deepcopy


REVIEW_STATUSES = ["人物理解", "待确认", "正式设定"]
CHANGE_STATUSES = ["异常行为", "潜在变化", "确认变化", "无变化", "待确认"]


def _object(properties, required=None):
    return {
        "type": "object",
        "properties": properties,
        "required": required or list(properties.keys()),
        "additionalProperties": False,
    }


def _array(items):
    return {"type": "array", "items": items}


def _string_enum(values):
    return {"type": "string", "enum": values}


EVIDENCE = _object(
    {
        "evidence_id": {"type": "string"},
        "scene_id": {"type": "string"},
        "source_locator": {"type": "string"},
        "kind": _string_enum(["台词", "行为", "他人评价", "场面事实"]),
        "speaker": {"type": "string"},
        "quote_or_action": {"type": "string"},
        "interpretation": {"type": "string"},
        "directness": _string_enum(["明确", "推断"]),
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    }
)


ASSERTION = _object(
    {
        "claim": {"type": "string"},
        "review_status": _string_enum(REVIEW_STATUSES),
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence_ids": _array({"type": "string"}),
        "counterevidence_ids": _array({"type": "string"}),
        "notes": {"type": "string"},
    }
)


SCENE_CHARACTER = _object(
    {
        "name": {"type": "string"},
        "aliases": _array({"type": "string"}),
        "is_present": {"type": "boolean"},
        "explicit_words": _array(EVIDENCE),
        "actions": _array(EVIDENCE),
        "other_people_views": _array(EVIDENCE),
        "surface_impression": {"type": "string"},
        "deeper_need": {"type": "string"},
        "current_want": {"type": "string"},
        "current_fear": {"type": "string"},
        "current_emotion": {"type": "string"},
        "conflict_response": {"type": "string"},
        "intimacy_pattern": {"type": "string"},
        "speech_style": {"type": "string"},
        "possible_contradictions": _array({"type": "string"}),
        "will_not_say_or_do": _array({"type": "string"}),
        "knowledge_changes": _array(
            _object(
                {
                    "fact_id": {"type": "string"},
                    "fact": {"type": "string"},
                    "change": _string_enum(["得知", "误以为", "怀疑", "忘记或否认", "无变化"]),
                    "evidence_ids": _array({"type": "string"}),
                }
            )
        ),
        "possible_changes": _array(
            _object(
                {
                    "dimension": {"type": "string"},
                    "before": {"type": "string"},
                    "after": {"type": "string"},
                    "trigger": {"type": "string"},
                    "status": _string_enum(CHANGE_STATUSES),
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "evidence_ids": _array({"type": "string"}),
                }
            )
        ),
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "review_status": _string_enum(REVIEW_STATUSES),
    }
)


SCENE_RELATIONSHIP = _object(
    {
        "person_a": {"type": "string"},
        "person_b": {"type": "string"},
        "surface_relation": {"type": "string"},
        "underlying_state": {"type": "string"},
        "a_view_of_b": {"type": "string"},
        "b_view_of_a": {"type": "string"},
        "a_wants": {"type": "string"},
        "b_wants": {"type": "string"},
        "initiative": {"type": "string"},
        "power_balance": {"type": "string"},
        "tension": {"type": "string"},
        "change": {"type": "string"},
        "stage": {"type": "string"},
        "hidden_information": _array({"type": "string"}),
        "evidence_ids": _array({"type": "string"}),
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "review_status": _string_enum(REVIEW_STATUSES),
    }
)


STORY_FACT = _object(
    {
        "fact_id": {"type": "string"},
        "fact": {"type": "string"},
        "truth_status": _string_enum(["剧本事实", "角色认知", "传闻", "待确认"]),
        "audience_status": _string_enum(["已向用户公开", "尚未向用户公开", "仅特定路线公开", "无法判断"]),
        "known_by": _array({"type": "string"}),
        "not_known_by": _array({"type": "string"}),
        "evidence_ids": _array({"type": "string"}),
    }
)


SCENE_ANALYSIS_SCHEMA = _object(
    {
        "scene_id": {"type": "string"},
        "heading": {"type": "string"},
        "synopsis": {"type": "string"},
        "chronology_marker": {"type": "string"},
        "characters": _array(SCENE_CHARACTER),
        "relationships": _array(SCENE_RELATIONSHIP),
        "story_facts": _array(STORY_FACT),
        "unresolved_questions": _array({"type": "string"}),
    }
)


QUICK_PREVIEW_CHARACTER = _object(
    {
        "name": {"type": "string"},
        "aliases": _array({"type": "string"}),
        "one_line_portrait": {"type": "string"},
        "evidence_scene_ids": _array({"type": "string"}),
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    }
)


QUICK_PREVIEW_RELATIONSHIP = _object(
    {
        "person_a": {"type": "string"},
        "person_b": {"type": "string"},
        "relation_label": {"type": "string"},
        "one_line_summary": {"type": "string"},
        "evidence_scene_ids": _array({"type": "string"}),
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    }
)


QUICK_SCRIPT_PREVIEW_SCHEMA = _object(
    {
        "characters": _array(QUICK_PREVIEW_CHARACTER),
        "relationships": _array(QUICK_PREVIEW_RELATIONSHIP),
        "unresolved_questions": _array({"type": "string"}),
    }
)


CHARACTER_WINDOW_SCHEMA = _object(
    {
        "character_name": {"type": "string"},
        "phase_label": {"type": "string"},
        "start_scene": {"type": "string"},
        "end_scene": {"type": "string"},
        "state_at_start": {"type": "string"},
        "state_at_end": {"type": "string"},
        "stable_patterns": _array(ASSERTION),
        "possible_changes": _array(
            _object(
                {
                    "dimension": {"type": "string"},
                    "before": {"type": "string"},
                    "after": {"type": "string"},
                    "trigger": {"type": "string"},
                    "status": _string_enum(CHANGE_STATUSES),
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "evidence_ids": _array({"type": "string"}),
                }
            )
        ),
        "contradictions": _array(ASSERTION),
        "knowledge_state": _array({"type": "string"}),
        "unresolved_questions": _array({"type": "string"}),
    }
)


ARC = _object(
    {
        "starting_state": {"type": "string"},
        "stages": _array(
            _object(
                {
                    "stage": {"type": "string"},
                    "state": {"type": "string"},
                    "key_behavior": {"type": "string"},
                    "change_status": _string_enum(CHANGE_STATUSES),
                    "evidence_ids": _array({"type": "string"}),
                }
            )
        ),
        "turning_points": _array(
            _object(
                {
                    "scene_id": {"type": "string"},
                    "trigger": {"type": "string"},
                    "before": {"type": "string"},
                    "after": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "evidence_ids": _array({"type": "string"}),
                }
            )
        ),
        "ending_state": {"type": "string"},
        "overall_change": {"type": "string"},
        "change_confirmed": {"type": "boolean"},
    }
)


POSTER_BRIEF = _object(
    {
        "first_glance_message": {"type": "string"},
        "expression": _array(ASSERTION),
        "posture_and_action": _array(ASSERTION),
        "clothing_and_props": _array(ASSERTION),
        "color_and_light": _array(ASSERTION),
        "copy_tone": _array(ASSERTION),
        "avoid_mismatches": _array(ASSERTION),
        "note": {"type": "string"},
    }
)


CHARACTER_CARD_SCHEMA = _object(
    {
        "character_name": {"type": "string"},
        "aliases": _array({"type": "string"}),
        "one_line_positioning": _array(ASSERTION),
        "first_impression": _array(ASSERTION),
        "deeper_personality_and_needs": _array(ASSERTION),
        "most_wanted": _array(ASSERTION),
        "greatest_fears": _array(ASSERTION),
        "conflict_pattern": _array(ASSERTION),
        "intimacy_pattern": _array(ASSERTION),
        "central_contradictions": _array(ASSERTION),
        "speech_rhythm_wording_emotion": _array(ASSERTION),
        "will_not_say_or_do": _array(ASSERTION),
        "arc": ARC,
        "poster_brief": POSTER_BRIEF,
        "unconfirmed_questions": _array({"type": "string"}),
    }
)


RELATIONSHIP_WINDOW_SCHEMA = _object(
    {
        "person_a": {"type": "string"},
        "person_b": {"type": "string"},
        "phase_label": {"type": "string"},
        "start_scene": {"type": "string"},
        "end_scene": {"type": "string"},
        "surface_relation": {"type": "string"},
        "underlying_state": {"type": "string"},
        "asymmetries": _array(ASSERTION),
        "power_and_initiative": _array(ASSERTION),
        "changes": _array(ASSERTION),
        "hidden_information": _array(ASSERTION),
        "unresolved_questions": _array({"type": "string"}),
    }
)


RELATIONSHIP_FILE_SCHEMA = _object(
    {
        "person_a": {"type": "string"},
        "person_b": {"type": "string"},
        "how_they_met": _array(ASSERTION),
        "surface_relation": _array(ASSERTION),
        "underlying_state": _array(ASSERTION),
        "a_view_of_b": _array(ASSERTION),
        "b_view_of_a": _array(ASSERTION),
        "a_wants": _array(ASSERTION),
        "b_wants": _array(ASSERTION),
        "initiative_and_control": _array(ASSERTION),
        "important_shared_events": _array(ASSERTION),
        "current_conflict": _array(ASSERTION),
        "unspoken_information": _array(ASSERTION),
        "timeline": _array(
            _object(
                {
                    "stage": {"type": "string"},
                    "state": {"type": "string"},
                    "change_event": {"type": "string"},
                    "scene_ids": _array({"type": "string"}),
                    "evidence_ids": _array({"type": "string"}),
                }
            )
        ),
        "current_stage": {"type": "string"},
        "story_truth_vs_audience_view": _array(ASSERTION),
        "unresolved_questions": _array({"type": "string"}),
    }
)


def schema_copy(schema):
    return deepcopy(schema)
