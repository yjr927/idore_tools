#!/usr/bin/env python3
"""One explicitly authorized OpenAI image pair or English publication draft.

No automatic retries or media downloads are performed. The workflow publishes
only generated PNGs, an intended publication draft and a safe task summary as
downloadable artifacts; it never adds those files to the public trend viewer.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from creative_service import CreativeService, CreativeError
from video_service import VideoService, VideoError


INPUT_KEYS = {"task", "model", "character", "description", "before_prompt", "after_prompt",
              "reference_image_url", "search_trends", "allow_paid", "video_model"}
PNG_NAME = re.compile(r"style-[a-f0-9]{32}-(?:before|after)\.png\Z")
JOB_ID = re.compile(r"creative-[a-f0-9]{32}\Z")
STATUSES = {"running", "completed", "partial", "failed", "submission_uncertain"}
VIDEO_STATUSES = {"pending", "in_progress", "submitting", "completed", "failed", "cancelled", "expired", "submission_uncertain"}
MAX_FRAME_BYTES = 5 * 1024 * 1024


class CloudCreativeError(ValueError):
    """A fixed, public preflight message; supplied values are never echoed."""


def text(value, label, maximum, required=False):
    if not isinstance(value, str) or len(value) > maximum or any(
            ord(char) < 32 and char not in "\n\r\t" for char in value):
        raise CloudCreativeError(label + "格式无效。")
    value = value.strip()
    if required and not value:
        raise CloudCreativeError(label + "不能为空。")
    return value


def public_image_url(value):
    """Accept an unsigned public HTTPS URL; send it only to the fixed API."""
    value = text(value, "参考图片地址", 4096, True)
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (parsed.scheme != "https" or not host or parsed.username or parsed.password
                or parsed.port not in (None, 443) or parsed.query or parsed.fragment
                or any(char.isspace() for char in value) or "." not in host
                or host == "localhost" or host.endswith((".local", ".localhost", ".internal"))):
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError()
    except ValueError:
        raise CloudCreativeError("参考图片必须使用不含签名、查询参数或登录信息的公开 HTTPS 图片直链。") from None
    return value


def safe_summary(job, task):
    result = {"task": task, "status": "not_completed"}
    if not isinstance(job, dict):
        return result
    if isinstance(job.get("id"), str) and JOB_ID.fullmatch(job["id"]):
        result["id"] = job["id"]
    if job.get("kind") in ("images", "draft"):
        result["kind"] = job["kind"]
    if isinstance(job.get("status"), str) and job["status"] in STATUSES:
        result["status"] = job["status"]
    model = job.get("model")
    if (isinstance(model, str) and re.fullmatch(r"gpt-[A-Za-z0-9._-]{1,160}", model)
            and not any(key and key in model for key in (os.environ.get("OPENAI_API_KEY"), os.environ.get("OPENROUTER_API_KEY")))):
        result["model"] = model
    if isinstance(job.get("steps"), dict):
        steps = {name: value for name, value in job["steps"].items()
                 if name in ("before", "after", "search", "draft") and isinstance(value, str)
                 and value in ("submitting", "completed", "failed", "submission_uncertain")}
        if steps:
            result["steps"] = steps
    return result


def current_local_job(service, token):
    """Read only this submission's in-memory/on-disk journal, never the API."""
    if service is None or not token:
        return None
    try:
        journal = service.jobs()
        rows = journal.get("jobs", []) if isinstance(journal, dict) else []
        return next((row for row in rows if isinstance(row, dict) and row.get("idempotency_key") == token), None)
    except Exception:
        return None


def safe_diagnostics(exc, local_job, task):
    result = {}
    if local_job is not None:
        state = safe_summary(local_job, task)
        result["local_job_state"] = state
        result["paid_step_recorded"] = bool(state.get("steps"))
    status = getattr(exc, "status", None)
    if isinstance(exc, (CreativeError, VideoError)) and type(status) is int and status in (
            400, 401, 402, 403, 404, 409, 413, 415, 422, 429, 500, 502, 503, 504):
        result["provider_or_validation_status"] = status
    else:
        status = None
    state = result.get("local_job_state", {})
    steps = state.get("steps", {})
    if steps and all(value == "completed" for value in steps.values()) and status in (500, 502):
        reason = "completed_step_result_validation_failed"
    elif any(value == "submission_uncertain" for value in steps.values()):
        reason = "submission_uncertain"
    elif isinstance(exc, CloudCreativeError):
        reason = "local_preflight_or_output_check"
    else:
        reason = {400: "invalid_request_or_configuration", 401: "authentication_failed",
            402: "quota_or_billing_rejected", 403: "permission_denied", 404: "model_or_endpoint_unavailable",
            409: "request_conflict", 413: "input_too_large", 415: "unsupported_media_type",
            422: "input_rejected", 429: "quota_or_rate_limited", 500: "local_or_provider_failure",
            502: "transport_or_result_validation_failed", 503: "provider_unavailable",
            504: "provider_timeout"}.get(status, "unclassified_failure")
    result["failure_reason"] = reason
    return result


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def publication_draft(job):
    """Export the intended copy and actual search evidence, not service inputs."""
    draft = job.get("draft")
    if not isinstance(draft, dict):
        raise CloudCreativeError("文案结果不完整，请检查任务记录，不会自动重新提交。")
    captions, hashtags, note = draft.get("captions"), draft.get("hashtags"), draft.get("trend_note")
    if (not isinstance(captions, list) or len(captions) != 3
            or any(not isinstance(value, str) or not value.strip() or len(value) > 2200 for value in captions)
            or not isinstance(hashtags, list) or not 1 <= len(hashtags) <= 15
            or not isinstance(note, str) or len(note) > 3000):
        raise CloudCreativeError("文案结果格式无效，请检查任务记录，不会自动重新提交。")
    safe_hashtags = []
    for item in hashtags:
        if not isinstance(item, dict) or not all(key in item for key in ("tag", "reason", "category", "source_url", "observed_at")):
            raise CloudCreativeError("标签结果格式无效，请检查任务记录。")
        if (not isinstance(item["tag"], str) or not re.fullmatch(r"#[A-Za-z0-9_]{1,80}", item["tag"])
                or not isinstance(item["reason"], str) or len(item["reason"]) > 1000
                or item["category"] not in ("content", "brand", "trend")):
            raise CloudCreativeError("标签结果格式无效，请检查任务记录。")
        safe_hashtags.append({key: item[key] for key in ("tag", "reason", "category", "source_url", "observed_at")})
    sources = job.get("sources", [])
    if not isinstance(sources, list) or len(sources) > 30:
        raise CloudCreativeError("文案来源结果格式无效，请检查任务记录。")
    safe_sources = []
    for item in sources:
        if not isinstance(item, dict) or not all(key in item for key in ("url", "title", "observed_at")):
            raise CloudCreativeError("文案来源结果格式无效，请检查任务记录。")
        safe_sources.append({key: item[key] for key in ("url", "title", "observed_at")})
    # The service validates that cited sources were returned by the actual search.
    return {"language": "en", "target_market": "US", "captions": captions,
            "hashtags": safe_hashtags, "trend_note": note, "sources": safe_sources}


def model_id(value):
    value = text(value, "模型配置", 200, True)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", value):
        raise CloudCreativeError("请使用有效的明确模型 ID；不会自动替换模型。")
    return value


def generated_images(output):
    return sorted(path.name for path in (output / "generated").glob("style-*.png")
                  if PNG_NAME.fullmatch(path.name) and path.is_file() and not path.is_symlink())


def image_pair(output, job):
    if not isinstance(job, dict) or not isinstance(job.get("id"), str) or not JOB_ID.fullmatch(job["id"]):
        raise CloudCreativeError("造型图片任务编号无效，请检查已有结果。")
    files = []
    directory = output / "generated"
    if directory.is_symlink():
        raise CloudCreativeError("生成图片目录无效，未继续提交视频。")
    for stage in ("before", "after"):
        name = "style-" + job["id"][len("creative-"):] + "-" + stage + ".png"
        if job.get(stage + "_url") != "/generated/" + name:
            raise CloudCreativeError("造型图片结果不完整，请检查已有结果，不会自动重试。")
        path = directory / name
        if not path.is_file() or path.is_symlink():
            raise CloudCreativeError("造型图片文件未能保存，请检查已有结果。")
        files.append(path)
    return files


def frame_data(path):
    if path.stat().st_size > MAX_FRAME_BYTES:
        raise CloudCreativeError("已生成的图片超过视频接口每张 5MB 的限制；图片已保留，请压缩后单独提交视频，不会重新生成图片。")
    raw = path.read_bytes()
    if len(raw) > MAX_FRAME_BYTES or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise CloudCreativeError("已生成的图片格式或大小不符合视频接口要求，未提交视频。")
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


def video_summary(job):
    result = {}
    if not isinstance(job, dict):
        return result
    if isinstance(job.get("id"), str) and re.fullmatch(r"video-[a-f0-9]{32}", job["id"]):
        result["id"] = job["id"]
    if isinstance(job.get("remote_id"), str) and re.fullmatch(r"[A-Za-z0-9_-]{1,160}", job["remote_id"]):
        result["remote_id"] = job["remote_id"]
    if isinstance(job.get("status"), str) and job["status"] in VIDEO_STATUSES:
        result["status"] = job["status"]
    if isinstance(job.get("model"), str) and re.fullmatch(r"bytedance/seedance-[a-z0-9.\-]{1,80}", job["model"]):
        result["model"] = job["model"]
    return result


def downloaded_video(output, job):
    ident = job.get("id") if isinstance(job, dict) else None
    if not isinstance(ident, str) or not re.fullmatch(r"video-[a-f0-9]{32}", ident):
        raise CloudCreativeError("视频下载任务编号无效，请查询已有任务，不会重新提交。")
    name = ident + ".mp4"
    path = output / "generated" / name
    if (job.get("result_url") != "/generated/" + name or not path.is_file()
            or path.is_symlink() or path.stat().st_size <= 0):
        raise CloudCreativeError("视频任务已完成但文件未保存；任务编号已保留，请单独重新下载。")
    return name


def full_summary(image_job, video_job, draft_job, stage, output):
    result = {"task": "full_video", "status": "not_completed", "stage": stage,
              "image_files": generated_images(output), "caption_basis": "generated_style_images_only"}
    if image_job is not None:
        result["images"] = safe_summary(image_job, "styles")
    if video_job is not None:
        result["video"] = video_summary(video_job)
    if draft_job is not None:
        result["draft"] = safe_summary(draft_job, "draft")
    return result


def full_preflight(creative, video, image_model, text_model, video_model):
    # These are free capability/key lookups. Do them before the first image edit.
    catalog = creative.models()
    for key, selected in (("image_models", image_model), ("text_models", text_model)):
        if not isinstance(catalog, dict) or not any(isinstance(item, dict) and item.get("id") == selected
                for item in catalog.get(key, [])):
            raise CloudCreativeError("账号没有提供所选图片或文案模型；未开始付费生成，请检查模型变量。")
    video.connect({"api_key": os.environ["OPENROUTER_API_KEY"]})
    models = video.models()
    selected = next((item for item in models.get("models", []) if isinstance(item, dict)
                     and item.get("id") == video_model), None) if isinstance(models, dict) else None
    if (not selected or 5 not in selected.get("supported_durations", [])
            or "720p" not in selected.get("supported_resolutions", [])
            or "9:16" not in selected.get("supported_aspect_ratios", [])
            or not all(stage in selected.get("supported_frame_images", []) for stage in ("first_frame", "last_frame"))):
        raise CloudCreativeError("视频模型当前未声明支持本流程的 5秒／720p／9:16 首尾帧；未开始付费生成。")


def main():
    output = Path(os.environ.get("CREATIVE_OUTPUT_DIR", "generation-output"))
    task, job = "unknown", None
    image_job = video_job = draft_job = None
    service, active_creative_token = None, ""
    stage = "preflight"
    try:
        output.mkdir(parents=True, exist_ok=True)
        raw = os.environ.get("CREATIVE_INPUTS", "{}")
        if len(raw) > 128 * 1024:
            raise CloudCreativeError("创作参数过多，未提交生成。")
        settings = json.loads(raw)
        if not isinstance(settings, dict) or set(settings) - INPUT_KEYS:
            raise CloudCreativeError("创作参数必须为受支持的对象字段。")
        task = settings.get("task")
        if task not in ("styles", "draft", "full_video"):
            raise CloudCreativeError("请选择 styles（前后造型）、draft（英文文案）或 full_video（完整视频流程）。")
        if settings.get("allow_paid") is not True:
            raise CloudCreativeError("尚未允许本次使用 OpenAI 额度，没有提交生成。")
        if os.environ.get("GITHUB_RUN_ATTEMPT", "1") != "1":
            raise CloudCreativeError("这是重新运行；为避免重复扣费，本次未提交。请检查已有结果后新建一次明确的创作任务。")
        if not os.environ.get("OPENAI_API_KEY", "").strip():
            raise CloudCreativeError("请先在 GitHub Actions Secrets 保存 OPENAI_API_KEY。")
        model = text(settings.get("model", ""), "模型", 200)
        model = model or text(os.environ.get("OPENAI_IMAGE_MODEL" if task in ("styles", "full_video") else "OPENAI_MODEL", ""), "模型配置", 200)
        if not model:
            raise CloudCreativeError("请明确填写本次任务可用的模型 ID，或配置对应的 OPENAI_IMAGE_MODEL／OPENAI_MODEL 变量。")
        model = model_id(model)
        character = text(settings.get("character", ""), "角色名称", 200)
        description = text(settings.get("description", ""), "视频／创作说明", 2000, task in ("draft", "full_video"))
        search = settings.get("search_trends", False)
        if type(search) is not bool:
            raise CloudCreativeError("search_trends 必须为 true 或 false。")
        reference = text(settings.get("reference_image_url", ""), "参考图片地址", 4096)
        body = {"idempotency_key": "github-creative-" + os.environ.get("GITHUB_RUN_ID", "manual"),
                "model": model, "consent": True}
        if task in ("styles", "full_video"):
            before = text(settings.get("before_prompt", ""), "变装前说明", 3000, True)
            after = text(settings.get("after_prompt", ""), "变装后说明", 3000, True)
            reference = public_image_url(reference)
            context = "\n".join(part for part in (
                "Character: " + character if character else "",
                "Creative brief: " + description if description else "") if part)
            prefix = context + "\n" if context else ""
            body.update(reference_image=reference, before_prompt=prefix + before, after_prompt=prefix + after)
        else:
            if reference:
                raise CloudCreativeError("云端文案任务按视频说明生成，不接收参考图片地址；如需画面分析，请在本机创作页选择图片。")
            body.update(description=description, character=character, search_trends=search)
        text_model = video_model = ""
        if task == "full_video":
            if not os.environ.get("OPENROUTER_API_KEY", "").strip():
                raise CloudCreativeError("完整视频流程还需要 GitHub Actions Secret OPENROUTER_API_KEY；未开始生成图片。")
            text_model = model_id(os.environ.get("OPENAI_MODEL", ""))
            video_model = model_id(settings.get("video_model", ""))
            if not re.fullmatch(r"bytedance/seedance-[a-z0-9.\-]{1,80}", video_model):
                raise CloudCreativeError("请明确选择 Seedance 视频模型；未开始付费生成。")
        service = CreativeService(output)
        if task == "full_video":
            video = VideoService(output)
            full_preflight(service, video, model, text_model, video_model)
            stage = "images"
            body["idempotency_key"] += "-styles"
            active_creative_token = body["idempotency_key"]
            image_job = service.generate_images(body)
            if image_job.get("status") != "completed":
                raise CloudCreativeError("前后造型图片尚未全部完成；已有图片保留，不会继续扣费提交视频或自动重试。")
            frames = [frame_data(path) for path in image_pair(output, image_job)]
            stage = "video"
            active_creative_token = ""
            video_job = video.submit({"idempotency_key": "github-creative-" + os.environ.get("GITHUB_RUN_ID", "manual") + "-video",
                "model": video_model, "mode": "frames", "prompt": description,
                "first_frame": frames[0], "last_frame": frames[1], "duration": 5, "resolution": "720p",
                "aspect_ratio": "9:16", "generate_audio": False, "consent": True})
            deadline = time.monotonic() + 25 * 60
            while video_job.get("status") in ("pending", "in_progress", "submitting") and time.monotonic() < deadline:
                time.sleep(30)
                video_job = video.refresh(video_job["id"])
                print("Video task status: " + str(video_job.get("status")), flush=True)
            if video_job.get("status") != "completed":
                raise CloudCreativeError("视频尚未完成；图片和视频任务编号已保留，请用视频查询任务继续处理，不要重新生成整套素材。")
            video_job = video.download(video_job["id"])
            downloaded_video(output, video_job)
            stage = "draft"
            active_creative_token = "github-creative-" + os.environ.get("GITHUB_RUN_ID", "manual") + "-draft"
            draft_job = service.generate_draft({"idempotency_key": active_creative_token,
                "model": text_model, "character": character, "description": description +
                    "\nVisual inputs are generated before/after style images only, not frames extracted from the finished movie. Do not claim the finished movie was visually reviewed.",
                "frames": frames, "search_trends": search, "consent": True})
            if draft_job.get("status") != "completed":
                raise CloudCreativeError("图片和视频已生成，文案尚未完成；请下载已有素材，不要重新运行整套生成。")
            draft = publication_draft(draft_job)
            draft["frame_basis"] = "generated_style_images_only"
            write_json(output / "publication-draft.json", draft)
            summary = full_summary(image_job, video_job, draft_job, "completed", output)
            summary["status"] = "completed"
            write_json(output / "creative-summary.json", summary)
            print("Creative task status: completed", flush=True)
            return 0
        stage = "images" if task == "styles" else "draft"
        active_creative_token = body["idempotency_key"]
        job = service.generate_images(body) if task == "styles" else service.generate_draft(body)
        summary = safe_summary(job, task)
        if task == "draft" and summary["status"] == "completed":
            write_json(output / "publication-draft.json", publication_draft(job))
        if task == "styles":
            files = generated_images(output)
            summary["image_files"] = files
            if summary["status"] == "completed" and len(files) != 2:
                raise CloudCreativeError("造型任务没有返回两张可下载图片，请检查已有结果，不会自动重试。")
        write_json(output / "creative-summary.json", summary)
        print("Creative task status: " + summary["status"], flush=True)
        return 0 if summary["status"] == "completed" else 1
    except Exception as exc:
        # Provider exceptions may contain raw payloads. Only our own fixed
        # preflight messages are suitable for logs and artifact summaries.
        message = str(exc) if isinstance(exc, CloudCreativeError) else "OpenAI 创作任务未完成，请检查账户与已有任务记录；不会自动重试或重复提交。"
        local_job = current_local_job(service, active_creative_token)
        if local_job is not None:
            if task == "full_video":
                if stage == "images" and image_job is None:
                    image_job = local_job
                elif stage == "draft" and draft_job is None:
                    draft_job = local_job
            elif job is None:
                job = local_job
        summary = full_summary(image_job, video_job, draft_job, stage, output) if task == "full_video" else safe_summary(job, task if task in ("styles", "draft") else "unknown")
        if summary["status"] != "not_completed":
            summary["last_known_status"] = summary["status"]
        summary.update(status="not_completed", message=message)
        summary.update(safe_diagnostics(exc, local_job, "styles" if stage == "images" else "draft"))
        try:
            output.mkdir(parents=True, exist_ok=True)
            write_json(output / "creative-summary.json", summary)
        except OSError:
            pass
        print(message, file=sys.stderr)
        print("Creative safe summary: " + json.dumps(summary, ensure_ascii=False, separators=(",", ":"), allow_nan=False), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
