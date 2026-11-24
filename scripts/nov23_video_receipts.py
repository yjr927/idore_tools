"""One approved November 23 replacement per named film. The caller owns the durable claim."""
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError
from urllib.request import build_opener

from scripts.observed_dance import gesture_prompt
from video_service import BASE, VideoService, VideoError, _NoRedirect, _transport

MODEL = "bytedance/seedance-2.5"
JOB = re.compile(r"gen-vid-[0-9]{10,13}-[A-Za-z0-9]{20}")
STATES = frozenset(("pending", "in_progress", "completed", "failed", "cancelled", "expired"))
# Source id/url, role photo SHA, immutable URL digest, approved gesture prompt digest.
BINDINGS = {
    "2026-11-23-momoko": (
        "tiktok-7703265627972128021", "https://www.tiktok.com/@itsmomoko.jp/video/7703265627972128021",
        "3abb96e025889f477fd976ab634e8fd79dee3c7bf70a92157d617bcb614cbf85",
        "6e2f70ef81ef104b488d1166886cf441374da03641002632be4e8f1520768282",
        "83123fa281fffb58cc36b0152bfaa5c5dd01a0cea5dad47755f8473592895c9b"),
    "2026-11-23-bay": (
        "tiktok-7709762212679683342", "https://www.tiktok.com/@mybaybie/video/7709762212679683342",
        "323c636618027cbd6d200dcb82eb823ba8d2c83a66a89a2d2f4b7b3eecd0318c",
        "fd37d213345f1fb90d674df4ebf6d18fc00ae3e8de293289438643ada781c893",
        "6fe0705e9bc8f6c29e46a77f13c28da40e7f30b8bf457b73a43a3d6364600f03"),
}


class NamedVideoError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def digest(value):
    return sha256(value.encode()).hexdigest() if isinstance(value, str) else None


def diagnostic_for(status, document):
    """Return fixed local labels only. Provider prose and arbitrary fields stay in memory."""
    fallback = {400: "invalid_request", 401: "authentication_rejected", 402: "payment_required",
                403: "access_denied", 404: "endpoint_unavailable", 422: "input_rejected",
                429: "rate_limited"}.get(status, "submission_uncertain")
    diagnostic = {"video_post_reason": fallback}
    if status not in (400, 401, 402, 403, 404, 422, 429):
        return diagnostic
    codes = {"InputImageSensitiveContentDetected.PrivacyInformation": "input_image_privacy_rejected",
             "InputVideoSensitiveContentDetected.PrivacyInformation": "input_video_privacy_rejected",
             "content_policy_violation": "content_policy_rejected", "invalid_api_key": "authentication_rejected",
             "insufficient_quota": "insufficient_quota", "insufficient_credits": "payment_required",
             "insufficient_balance": "payment_required", "InvalidParameter": "invalid_parameter",
             "unsupported_parameter": "unsupported_parameter", "model_not_found": "model_unavailable",
             "image_download_failed": "image_download_failed", "image_fetch_failed": "image_download_failed",
             "invalid_image_url": "invalid_image_url", "image_parse_error": "invalid_image",
             "rate_limit_exceeded": "rate_limited"}
    params = {"model", "resolution", "aspect_ratio", "frame_images", "generate_audio", "duration",
              "prompt", "image_url", "frame_type", "frame_images.0.image_url.url",
              "frame_images[0].image_url.url", "frame_images.0.frame_type"}
    outer = document.get("error")
    if not isinstance(outer, dict):
        return diagnostic
    candidates = [outer]
    metadata = outer.get("metadata")
    raw = metadata.get("raw") if isinstance(metadata, dict) else None
    if isinstance(raw, str) and len(raw) < 65536:
        try:
            raw = json.loads(raw)
        except (ValueError, RecursionError):
            raw = None
    if isinstance(raw, dict):
        nested = raw.get("error", raw)
        if isinstance(nested, dict):
            candidates.append(nested)
    for record in candidates:
        code, param = record.get("code"), record.get("param")
        if isinstance(code, str) and code in codes:
            diagnostic["video_post_reason"] = codes[code]
        if isinstance(param, str) and param in params:
            diagnostic["video_post_param"] = param
    return diagnostic

class _SubmitTransport:
    def __init__(self, save_receipt):
        self.save = save_receipt
        self.receipt = {"paid_posts": 0}
        self.saved = False

    def __call__(self, request, *, timeout, max_bytes):
        if request.get_method() == "GET" and request.full_url == BASE + "/videos/models":
            return _transport(request, timeout=timeout, max_bytes=max_bytes)
        if request.get_method() != "POST" or request.full_url != BASE + "/videos" or self.receipt["paid_posts"]:
            raise NamedVideoError("unexpected_request")
        self.receipt["paid_posts"] = 1
        try:
            response = build_opener(_NoRedirect).open(request, timeout=timeout)
        except HTTPError as exc:
            response = exc
        with response:
            status = response.status
            self.receipt["video_post_http_status"] = status
            if response.geturl() != request.full_url:
                raise NamedVideoError("unexpected_response")
            limit = min(max_bytes, 65536)
            raw = response.read(limit + 1)
            body = {}
            if len(raw) <= limit:
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        body = parsed
                except (ValueError, RecursionError):
                    pass
            if status in (200, 201, 202):
                remote = body.get("id")
                credential = request.get_header("Authorization", "").removeprefix("Bearer ")
                if isinstance(remote, str) and JOB.fullmatch(remote) and not (credential and credential in remote):
                    self.receipt["remote_id"] = remote
                state = body.get("status")
                if isinstance(state, str) and state in STATES:
                    self.receipt["video_post_status"] = state
            else:
                self.receipt.update(diagnostic_for(status, body))
            self.save(dict(self.receipt))
            self.saved = True
            return status, {}, raw if 200 <= status < 300 and len(raw) <= limit else b""


def submit_named(slot, row, source, character, directory, save_receipt):
    spec = BINDINGS.get(slot)
    if spec is None:
        raise NamedVideoError("unapproved_slot")
    source_id, source_url, photo, url_digest, prompt_digest = spec
    binding = [source_id, source_url, "role-" + photo[:16], photo]
    if (row.get("sequence_slot") != slot or row.get("id") != "agent-" + slot
            or row.get("model") != MODEL or row.get("motion_binding") != binding
            or row.get("remote_id") or row.get("motion_reference_method") != "observed_description"
            or [source.get("id"), source.get("url"), character.get("id"), character.get("sha256")] != binding
            or digest(character.get("image_url")) != url_digest):
        raise NamedVideoError("binding_mismatch")
    prompt = gesture_prompt(source)
    if digest(prompt) != prompt_digest:
        raise NamedVideoError("binding_mismatch")
    transport = _SubmitTransport(save_receipt)
    service = VideoService(Path(directory) / "named-submit", request_fn=transport)
    try:
        job = service.submit({"idempotency_key": "pipeline-agent-" + slot + "-attempt-2",
                        "model": MODEL, "mode": "frames", "first_frame": character["image_url"],
                        "prompt": prompt, "duration": 6, "resolution": "720p", "aspect_ratio": "adaptive",
                        "generate_audio": False, "consent": True})
    except (VideoError, OSError, NamedVideoError):
        job = None
    # A matching fingerprint in this slot's local ledger can return a prior
    # successful response without issuing any HTTP request in this invocation.
    if not transport.receipt["paid_posts"] and isinstance(job, dict):
        prior = job.get("remote_id")
        credential = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if isinstance(prior, str) and JOB.fullmatch(prior) and not (credential and credential in prior):
            transport.receipt["remote_id"] = prior
    if not transport.saved:
        save_receipt(dict(transport.receipt))
    if transport.receipt.get("remote_id"):
        return transport.receipt["remote_id"]
    if not transport.receipt["paid_posts"]:
        raise NamedVideoError("video_preflight_failed")
    raise NamedVideoError(transport.receipt.get("video_post_reason", "submission_uncertain"))


def download_named(slot, remote_id, directory):
    if slot not in BINDINGS or not isinstance(remote_id, str) or not JOB.fullmatch(remote_id):
        raise NamedVideoError("invalid_job_id")
    allowed = {BASE + "/videos/" + remote_id, BASE + "/videos/" + remote_id + "/content"}

    def read_only(request, *, timeout, max_bytes):
        if request.get_method() != "GET" or request.full_url not in allowed:
            raise NamedVideoError("unexpected_request")
        return _transport(request, timeout=timeout, max_bytes=max_bytes)

    service = VideoService(Path(directory) / "named-download", request_fn=read_only)
    job = service.recover(remote_id, MODEL)
    deadline = time.monotonic() + 1500
    while time.monotonic() < deadline:
        try:
            job = service.refresh(job["id"])
            if job["status"] == "completed":
                service.download(job["id"])
                return service.generated / (job["id"] + ".mp4")
            if job["status"] in ("failed", "cancelled", "expired"):
                raise NamedVideoError("provider_" + job["status"])
        except VideoError as exc:
            if exc.status in (400, 401, 402, 403):
                raise NamedVideoError("poll_or_download_denied") from None
        time.sleep(30)
    raise NamedVideoError("poll_or_download_incomplete")
