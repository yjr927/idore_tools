#!/usr/bin/env python3
"""Submit one explicitly approved KAI + reviewed clay-motion generation in CI.

Only an immutable MP4 in this repository's reference-assets directory is
accepted. The full clip, its expected SHA-256 and decoded metadata are checked
before submission. Media and credentials are never included in the summary.
This runner never retries a paid request, including an ambiguous submission.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from video_service import VideoError, VideoService, _mp4, _validate_endpoint


REPOSITORY = "yjr927/idore_tools"
IMAGE_URL = "https://raw.githubusercontent.com/yjr927/idore_tools/bd19680c2f14b89be1a1025b7825d9e49bd4b433/reference-assets/kai-full.png"
MODEL = "bytedance/seedance-2.5"
MAX_MEDIA_BYTES = 50 * 1024 * 1024
MAX_ERROR_BYTES = 64 * 1024
POLL_SECONDS = 30
POLL_DEADLINE_SECONDS = 25 * 60
PROMPT = (
    "Create a 6-second, 9:16 video of KAI, the fictional male character in reference image 1. "
    "Reference image 1 is the sole source of the character's identity and appearance: preserve KAI's "
    "face, facial proportions, hair, skin, clothing, sheer black fabric, light-colored trousers, "
    "and butterfly accessories exactly as shown. Keep the same character and wardrobe throughout. "
    "Reference video 2 is an approved faceless clay motion study. Use it only for the visible body "
    "and finger movements, their order, timing, rhythm, orientation, framing, and camera movement. "
    "Reproduce only movements actually visible in that video; do not invent choreography or a transformation. "
    "Render every frame as KAI from image 1. Do not transfer the clay figure's faceless head, white plastic "
    "material, simplified body, missing details, proportions, or background into KAI's appearance. "
    "Maintain coherent anatomy and natural, stable hands with five distinct fingers, believable joints "
    "and continuous contact/occlusion. Keep the face, hair, clothing and butterflies consistent during motion. "
    "No morphing into the clay figure, no face replacement, no costume change, no added text, and no audio."
)
KNOWN_REJECTIONS = {
    "InputVideoSensitiveContentDetected.PrivacyInformation": (
        "input_video_privacy_rejected",
        "模型服务商因人像隐私限制拒绝了动作参考视频。不会自动重试、改换模型或退回纯文字生成。"),
    "InputImageSensitiveContentDetected.PrivacyInformation": (
        "input_image_privacy_rejected",
        "模型服务商因人像隐私限制拒绝了 KAI 角色图片。不会自动重试、改换模型或替换角色。"),
}
SAFE_MESSAGES = {
    "provider_rejected_input": "模型服务商拒绝了当前参数或素材；未取得成片，不会自动重试。",
    "provider_authentication_failed": "OpenRouter 密钥无效或未获授权，没有自动重试。",
    "provider_balance_insufficient": "OpenRouter 余额不足，没有自动重试。",
    "provider_rate_limited": "OpenRouter 请求受限，没有自动重试。",
    "generation_failed": "生成未完成。请用保留的任务编号核对结果与计费，不要重复提交。",
    "submission_uncertain": "提交结果不确定，可能已计费。请先到 OpenRouter 核对，不要重复提交。",
}
USAGE_FIELDS = {"cost", "input_tokens", "output_tokens", "total_tokens", "input_video_seconds",
                "output_video_seconds", "video_seconds", "input_images", "output_images"}


class Stopped(Exception):
    def __init__(self, reason):
        self.reason = reason


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def approved_clay_url(value):
    """Restrict every public source to our immutable, reviewed clay namespace."""
    if not isinstance(value, str) or len(value) > 512:
        raise Stopped("clay_reference_url_invalid")
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "https" and parsed.netloc == "raw.githubusercontent.com"
            and not parsed.query and not parsed.fragment and re.fullmatch(
                r"/yjr927/idore_tools/[a-f0-9]{40}/reference-assets/clay-[a-z0-9][a-z0-9-]{0,100}\.mp4",
                parsed.path) is not None)
    except ValueError:
        valid = False
    if not valid:
        raise Stopped("clay_reference_url_invalid")
    return value


def numeric_usage(value):
    if not isinstance(value, dict):
        return {}
    return {key: number for key, number in value.items() if key in USAGE_FIELDS
        and type(number) in (int, float) and math.isfinite(number) and 0 <= number <= 10**12}


def rejection_diagnosis(raw, status, secret=""):
    """Inspect bounded JSON error fields; expose fixed labels/safe identifiers."""
    if not isinstance(raw, bytes) or len(raw) > MAX_ERROR_BYTES:
        return None
    try:
        document = json.loads(raw)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None
    if not isinstance(document, dict):
        return None
    error = document.get("error")
    if error is None and status >= 400:
        error = document
    stack, details = [(error, 0)], {}
    while stack:
        item, depth = stack.pop()
        if depth > 7:
            continue
        if isinstance(item, dict):
            for field in ("code", "param"):
                value = item.get(field)
                if (isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,120}", value)
                        and not value.lower().startswith(("sk-", "bearer")) and "key" not in value.lower()
                        and not (secret and secret in value)):
                    details["provider_" + field] = value
            stack.extend((item[key], depth + 1) for key in ("code", "message", "error", "metadata", "raw", "response") if key in item)
        elif isinstance(item, str) and len(item) <= MAX_ERROR_BYTES:
            for code, (reason, message) in KNOWN_REJECTIONS.items():
                if re.search(r"(?<![A-Za-z0-9_.])" + re.escape(code) + r"(?![A-Za-z0-9_.])", item):
                    return {**details, "reason": reason, "provider_code": code, "message": message}
            if item.lstrip().startswith("{"):
                try:
                    stack.append((json.loads(item), depth + 1))
                except (ValueError, TypeError, RecursionError):
                    pass
    return details or None


class ObservedTransport:
    """Capture safe diagnosis at first I/O; never return raw upstream errors."""
    def __init__(self):
        self.diagnosis = None
        self.details = {}
        self.usage = {}
        self.last_error_status = None
        self.paid_posts = 0
        self._secret = os.environ.get("OPENROUTER_API_KEY", "").strip()

    def observe(self, raw, status):
        diagnosis = rejection_diagnosis(raw, status, self._secret)
        if diagnosis:
            self.details.update({key: value for key, value in diagnosis.items() if key in ("provider_code", "provider_param")})
            if "reason" in diagnosis:
                self.diagnosis = diagnosis
        if 400 <= status <= 599:
            self.last_error_status = status
        if 200 <= status < 300:
            try:
                document = json.loads(raw)
            except (ValueError, TypeError, UnicodeError, RecursionError):
                return
            if isinstance(document, dict):
                usage = numeric_usage(document.get("usage"))
                if usage:
                    self.usage = usage

    def __call__(self, request, *, timeout, max_bytes):
        _validate_endpoint(request.full_url, request.get_method())
        if request.get_method() == "POST":
            if self.paid_posts:
                raise VideoError("本次付费提交已尝试，不会自动重复发送。", 409)
            self.paid_posts += 1
        try:
            response = build_opener(NoRedirect).open(request, timeout=timeout)
        except HTTPError as exc:
            try:
                raw = exc.read(MAX_ERROR_BYTES + 1)
                self.observe(raw, exc.code)
            except Exception:
                self.last_error_status = exc.code
            finally:
                exc.close()
            # Raw body/headers can contain credentials or source URLs. They do
            # not leave this boundary, even for unknown provider errors.
            return exc.code, {}, b""
        with response:
            if response.geturl() != request.full_url:
                raise VideoError("视频服务返回了不受支持的跳转。", 502)
            raw = response.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise VideoError("视频服务返回的文件过大。", 502)
            if not request.full_url.endswith("/content"):
                self.observe(raw, response.status)
            if response.status not in (200, 201, 202):
                return response.status, {}, b""
            return response.status, dict(response.headers), raw


def download_media(url, target, kind, expected_sha256=None):
    """Unauthenticated, nonredirecting full download to a private temporary file."""
    if kind == "video":
        approved_clay_url(url)
    elif kind != "image" or url != IMAGE_URL:
        raise Stopped("character_image_not_approved")
    request = Request(url, method="GET", headers={"Accept": "video/mp4" if kind == "video" else "image/png",
        "Accept-Encoding": "identity", "User-Agent": "idore-clay-preflight/1.0"})
    maximum = MAX_MEDIA_BYTES if kind == "video" else 5 * 1024 * 1024
    digest, count = hashlib.sha256(), 0
    deadline = time.monotonic() + 120
    try:
        with build_opener(NoRedirect).open(request, timeout=30) as response:
            if response.status != 200 or response.geturl() != url or response.headers.get("Content-Range"):
                raise Stopped("approved_media_download_incomplete")
            mime = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if mime not in (("video/mp4", "application/octet-stream") if kind == "video" else ("image/png", "application/octet-stream")):
                raise Stopped("approved_media_format_invalid")
            length = response.headers.get("Content-Length")
            if length is not None and (not re.fullmatch(r"[0-9]{1,12}", length) or not 0 < int(length) <= maximum):
                raise Stopped("approved_media_size_invalid")
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as destination:
                while True:
                    if time.monotonic() >= deadline:
                        raise Stopped("approved_media_download_timed_out")
                    chunk = response.read(min(64 * 1024, maximum - count + 1))
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > maximum:
                        raise Stopped("approved_media_size_invalid")
                    digest.update(chunk)
                    destination.write(chunk)
        if not count or length is not None and count != int(length):
            raise Stopped("approved_media_download_incomplete")
        raw = target.read_bytes()
        if (kind == "video" and not _mp4(raw)) or (kind == "image" and not raw.startswith(b"\x89PNG\r\n\x1a\n")):
            raise Stopped("approved_media_format_invalid")
        if expected_sha256 and digest.hexdigest() != expected_sha256:
            raise Stopped("clay_reference_hash_mismatch")
        return {"verified": True, "sha256": digest.hexdigest(), "bytes": count}
    except Exception as exc:
        target.unlink(missing_ok=True)
        if isinstance(exc, Stopped):
            raise
        raise Stopped("approved_media_download_failed") from None


def probe_clay(path):
    """Require a decodable six-second, silent vertical MP4, before any POST."""
    child_env = {key: value for key, value in os.environ.items()
                 if key not in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "GENERATION_INPUTS")}
    try:
        result = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height,duration", "-of", "json", str(path)],
            capture_output=True, check=True, timeout=30, env=child_env)
        if len(result.stdout) > 64 * 1024:
            raise ValueError()
        document = json.loads(result.stdout)
        streams = document["streams"]
        video = [item for item in streams if item.get("codec_type") == "video"]
        duration = float(document["format"]["duration"])
        if (len(video) != 1 or any(item.get("codec_type") == "audio" for item in streams)
                or not math.isfinite(duration) or abs(duration - 6) > .1
                or type(video[0].get("width")) is not int or type(video[0].get("height")) is not int
                or video[0]["width"] < 256 or video[0]["height"] < 256
                or abs(video[0]["width"] / video[0]["height"] - 9 / 16) > .01):
            raise ValueError()
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
            "-map", "0:v:0", "-f", "null", "-"], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=True, timeout=45, env=child_env)
        return {"duration": duration, "width": video[0]["width"], "height": video[0]["height"], "audio": False}
    except Exception:
        raise Stopped("clay_decoded_metadata_invalid") from None


def safe_task(job, key=""):
    if not isinstance(job, dict):
        return {}
    result = {}
    for name, pattern in (("id", r"video-[a-f0-9]{32}"), ("remote_id", r"[A-Za-z0-9_-]{1,160}")):
        value = job.get(name)
        if isinstance(value, str) and re.fullmatch(pattern, value) and not value.startswith("sk-") and not (key and key in value):
            result[name] = value
    if job.get("status") in {"submitting", "pending", "in_progress", "completed", "failed", "cancelled", "expired", "submission_uncertain"}:
        result["status"] = job["status"]
    if job.get("model") == MODEL:
        result["model"] = MODEL
    cost = numeric_usage({"cost": job.get("cost")})
    result.update(cost)
    return result


def write_summary(output, summary):
    output.mkdir(parents=True, exist_ok=True)
    path = output / "clay-generation-summary.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main():
    output = Path(os.environ.get("VIDEO_OUTPUT_DIR", "generation-output"))
    summary = {"schema_version": 1, "status": "stopped", "model": MODEL,
        "duration": 6, "resolution": "720p", "aspect_ratio": "9:16", "generate_audio": False,
        "paid_post_attempted": False}
    service, job, transport = None, None, ObservedTransport()
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    try:
        if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
            raise Stopped("authorized_github_repository_required")
        if os.environ.get("GITHUB_RUN_ATTEMPT") != "1":
            raise Stopped("rerun_refused_to_avoid_duplicate_charge")
        run_id = os.environ.get("GITHUB_RUN_ID", "")
        if not re.fullmatch(r"[0-9]{1,24}", run_id):
            raise Stopped("github_run_id_invalid")
        if os.environ.get("CLAY_ALLOW_PAID") != "true":
            raise Stopped("explicit_paid_authorization_required")
        if os.environ.get("CLAY_REVIEWED") != "true":
            raise Stopped("reviewed_clay_reference_required")
        if not VideoService._valid_key(key):
            raise Stopped("openrouter_secret_missing_or_invalid")
        clay_url = approved_clay_url(os.environ.get("CLAY_REFERENCE_URL"))
        expected_hash = os.environ.get("CLAY_REFERENCE_SHA256", "")
        if not re.fullmatch(r"[a-f0-9]{64}", expected_hash):
            raise Stopped("clay_reference_expected_hash_required")
        output.mkdir(parents=True, exist_ok=True)
        claim = output / ".clay-submit-claim.json"
        if claim.exists():
            try:
                previous = output / "clay-generation-summary.json"
                if previous.stat().st_size <= MAX_ERROR_BYTES:
                    prior = json.loads(previous.read_bytes())
                    summary.update(safe_task(prior, key))
                    usage = numeric_usage(prior.get("usage")) if isinstance(prior, dict) else {}
                    if usage:
                        summary["usage"] = usage
                    summary["paid_post_attempted"] = prior.get("paid_post_attempted") is True
            except (OSError, ValueError, TypeError, RecursionError):
                pass
            raise Stopped("prior_submission_claim_exists_do_not_repeat")
        with tempfile.TemporaryDirectory(prefix="kai-clay-") as temporary:
            private = Path(temporary)
            summary["character_image"] = download_media(IMAGE_URL, private / "kai.png", "image")
            summary["clay_reference"] = download_media(clay_url, private / "clay.mp4", "video", expected_hash)
            summary["clay_reference"].update(probe_clay(private / "clay.mp4"))
            service = VideoService(private / "state", request_fn=transport)
            body = {"idempotency_key": "github-clay-" + run_id, "model": MODEL, "mode": "references",
                "prompt": PROMPT, "duration": 6, "resolution": "720p", "aspect_ratio": "9:16",
                "generate_audio": False, "consent": True,
                "references": [{"type": "image_url", "url": IMAGE_URL}, {"type": "video_url", "url": clay_url}]}
            fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            # Exclusive durable claim precedes any potentially paid operation.
            # Even a crash before the POST requires human review, never a retry.
            descriptor = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                json.dump({"schema_version": 1, "run_id": run_id, "request_sha256": fingerprint,
                    "outcome": "submission_reserved_do_not_retry"}, target)
                target.flush()
                os.fsync(target.fileno())
            job = service.submit(body)
            summary.update(safe_task(job, key))
            summary["paid_post_attempted"] = bool(transport.paid_posts)
            write_summary(output, summary)  # Preserve accepted remote ID before polling.
            deadline = time.monotonic() + POLL_DEADLINE_SECONDS
            while job["status"] in ("submitting", "pending", "in_progress") and time.monotonic() < deadline:
                time.sleep(POLL_SECONDS)
                job = service.refresh(job["id"])
                summary.update(safe_task(job, key))
                if transport.usage:
                    summary["usage"] = transport.usage
                write_summary(output, summary)
            if job["status"] == "completed":
                job = service.download(job["id"])
                generated = private / "state" / "generated" / (job["id"] + ".mp4")
                destination = output / (job["id"] + ".mp4")
                with generated.open("rb") as source:
                    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, "wb") as target:
                        shutil.copyfileobj(source, target)
                summary["result_file"] = destination.name
            summary.update(safe_task(job, key))
            if transport.diagnosis:
                summary.update(transport.diagnosis)
            elif summary["status"] != "completed":
                summary.update(reason="generation_failed", message=SAFE_MESSAGES["generation_failed"])
        return_code = 0 if summary.get("status") == "completed" and summary.get("result_file") else 1
    except Exception as exc:
        # submit() may have saved a local/remote task before raising. Preserve
        # those identifiers while the private state directory still exists.
        if service is not None:
            try:
                rows = service.jobs()["jobs"]
                if rows:
                    summary.update(safe_task(rows[0], key))
            except Exception:
                pass
        if isinstance(exc, Stopped):
            summary["reason"] = exc.reason
        elif transport.diagnosis:
            summary.update(transport.diagnosis)
        else:
            status = transport.last_error_status
            reason = {400: "provider_rejected_input", 422: "provider_rejected_input",
                401: "provider_authentication_failed", 403: "provider_authentication_failed",
                402: "provider_balance_insufficient", 429: "provider_rate_limited"}.get(status,
                    "submission_uncertain" if transport.paid_posts and not summary.get("remote_id") else "generation_failed")
            summary.update(reason=reason, message=SAFE_MESSAGES[reason])
        if summary.get("status") == "completed":
            summary["last_known_status"] = "completed"
            summary["status"] = "not_completed"
        return_code = 1
    summary["paid_post_attempted"] = bool(transport.paid_posts or summary.get("paid_post_attempted"))
    if transport.usage:
        summary["usage"] = transport.usage
    if transport.last_error_status:
        summary["provider_http_status"] = transport.last_error_status
    summary.update(transport.details)
    try:
        write_summary(output, summary)
    except OSError:
        print("KAI clay generation: safe summary could not be saved.", flush=True)
        return 1
    print("KAI clay generation: " + str(summary["status"]), flush=True)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
