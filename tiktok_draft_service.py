"""Official TikTok inbox uploads only; never Direct Post or automatic publication.

The caller must verify human approval of this exact video hash, caption revision
and target account before calling send_draft. TikTok's VIDEO inbox API has no
caption field: caption_revision binds local approval only. Access tokens come
from TIKTOK_ACCESS_TOKEN, never from a browser or a public queue. OAuth client
registration, consent and refresh-token rotation require a separate backend.

Private state records publish_id and upload progress, never credentials or the
signed upload_url. A reserved/ambiguous init is never retried, including after
process restart. Retain this private receipt outside the public site. If state
is lost, the caller's durable upload reservation must prevent resubmission.
"""
from __future__ import annotations

import hashlib
import json
import math
import mmap
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from video_service import _mp4

BASE = "https://open.tiktokapis.com"
INIT_PATH = "/v2/post/publish/inbox/video/init/"
STATUS_PATH = "/v2/post/publish/status/fetch/"
MAX_VIDEO_BYTES = 200_000_000
MAX_JSON_BYTES = 256 * 1024
CHUNK_BYTES = 10_000_000
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
HASH_PATTERN = re.compile(r"[a-f0-9]{64}\Z")
PUBLISH_PATTERN = re.compile(r"[A-Za-z0-9_~.:-]{1,64}\Z")
PRIVATE_STATUSES = {"uploading", "draft_sent", "needs_attention"}
SAFE_REASONS = {"unsupported_tiktok_upload_address", "unsupported_tiktok_operation",
    "upload_must_not_forward_access_token", "tiktok_redirect_rejected", "tiktok_response_too_large",
    "tiktok_authorization_expired", "tiktok_permission_or_pending_limit", "tiktok_rate_limited",
    "tiktok_request_not_confirmed", "tiktok_response_invalid", "tiktok_upload_scope_missing",
    "tiktok_pending_drafts_limit", "tiktok_account_upload_restricted", "tiktok_publish_id_invalid",
    "tiktok_upload_not_confirmed", "tiktok_upload_outcome_uncertain_do_not_resubmit",
    "tiktok_status_temporarily_unavailable", "creator_already_posted_in_tiktok",
    "tiktok_processing_failed", "tiktok_status_unknown", "init_reserved_do_not_resubmit"}


class TikTokDraftError(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _upload_url(value):
    try:
        if not isinstance(value, str) or not 1 <= len(value) <= 4096 or any(ord(c) < 33 for c in value):
            raise ValueError()
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment
                or parsed.port not in (None, 443) or parsed.path != "/video/"
                or not re.fullmatch(r"(?:open-upload|upload\.(?:us|eu|sg))\.tiktokapis\.com", parsed.hostname or "")
                or not parsed.query):
            raise ValueError()
    except ValueError:
        raise TikTokDraftError("unsupported_tiktok_upload_address") from None
    return value


def _transport(request, *, timeout, max_bytes):
    if request.get_method() == "POST":
        if request.full_url not in (BASE + INIT_PATH, BASE + STATUS_PATH):
            raise TikTokDraftError("unsupported_tiktok_operation")
    elif request.get_method() == "PUT":
        _upload_url(request.full_url)
        if request.has_header("Authorization"):
            raise TikTokDraftError("upload_must_not_forward_access_token")
    else:
        raise TikTokDraftError("unsupported_tiktok_operation")
    try:
        response = build_opener(_NoRedirect).open(request, timeout=timeout)
    except HTTPError as exc:
        # Provider bodies/redirects can contain credentials: never read/export.
        code = exc.code
        exc.close()
        return code, {}, b""
    with response:
        if response.geturl() != request.full_url:
            raise TikTokDraftError("tiktok_redirect_rejected")
        raw = response.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise TikTokDraftError("tiktok_response_too_large")
        return response.status, {}, raw


def chunk_plan(size):
    """TikTok uses floor division: merge trailing bytes into the final chunk."""
    chunk_size = size if size <= 64_000_000 else CHUNK_BYTES
    count = max(1, size // chunk_size)
    return chunk_size, count


def _probe(path):
    environment = {key: value for key, value in os.environ.items()
                   if not any(word in key.upper() for word in ("TOKEN", "SECRET", "API_KEY"))}
    try:
        result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,width,height,avg_frame_rate:format=duration",
            "-of", "json", str(path)], capture_output=True, check=True, timeout=30, env=environment)
        if len(result.stdout) > 64 * 1024:
            raise ValueError()
        document = json.loads(result.stdout)
        stream = document["streams"][0]
        numerator, denominator = stream["avg_frame_rate"].split("/")
        return {"width": stream["width"], "height": stream["height"],
                "fps": float(numerator) / float(denominator),
                "duration": float(document["format"]["duration"]), "codec": stream["codec_name"]}
    except Exception:
        raise TikTokDraftError("approved_video_cannot_be_verified") from None


def _validate_probe(metadata):
    if (not isinstance(metadata, dict) or metadata.get("codec") not in ("h264", "hevc", "vp8", "vp9")
            or any(type(metadata.get(name)) is not int or not 360 <= metadata[name] <= 4096 for name in ("width", "height"))
            or any(type(metadata.get(name)) not in (int, float) or not math.isfinite(metadata[name]) for name in ("fps", "duration"))
            or not 23 <= metadata["fps"] <= 60 or not 0 < metadata["duration"] <= 600):
        raise TikTokDraftError("approved_video_outside_upload_limits")


class TikTokDraftService:
    def __init__(self, state_dir, request_fn=None, probe_fn=None, clock=None):
        self.directory = Path(state_dir)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "tiktok-draft-receipts.json"
        self._request = request_fn or _transport
        self._probe = probe_fn or _probe
        self._clock = clock or time.time
        self._token = os.environ.get("TIKTOK_ACCESS_TOKEN", "").strip()
        if not 8 <= len(self._token) <= 4096 or any(ord(c) < 33 for c in self._token):
            self._token = ""
        self._lock = threading.RLock()
        self._rows = {}
        if self.path.exists():
            try:
                if self.path.is_symlink() or self.path.stat().st_size > 4 * 1024 * 1024:
                    raise ValueError()
                document = json.loads(self.path.read_text())
                rows = document["uploads"]
                if document.get("schema_version") != 1 or not isinstance(rows, dict):
                    raise ValueError()
                for key, row in rows.items():
                    if (not ID_PATTERN.fullmatch(key) or not isinstance(row, dict)
                            or set(row) - {"fingerprint", "status", "publish_id", "phase", "uploaded_bytes", "reason"}
                            or row.get("status") not in PRIVATE_STATUSES
                            or row.get("phase") not in ("init_reserved", "init_accepted", "upload_complete", "inbox_delivered")
                            or type(row.get("uploaded_bytes")) is not int or not 0 <= row["uploaded_bytes"] <= MAX_VIDEO_BYTES
                            or row.get("reason") is not None and row["reason"] not in SAFE_REASONS
                            or not HASH_PATTERN.fullmatch(str(row.get("fingerprint", "")))
                            or row.get("publish_id") is not None and not PUBLISH_PATTERN.fullmatch(str(row["publish_id"]))):
                        raise ValueError()
                self._rows = rows
            except Exception:
                raise TikTokDraftError("private_draft_state_invalid_do_not_resubmit") from None

    def connection(self):
        return {"configured": bool(self._token), "status": "configured" if self._token else "not_configured",
                "scope": "video.upload", "mode": "inbox_only", "caption_transfer_supported": False}

    def _save(self):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory, delete=False) as target:
                temporary = Path(target.name)
                json.dump({"schema_version": 1, "uploads": self._rows}, target, separators=(",", ":"))
                target.flush()
                os.fsync(target.fileno())
            os.chmod(temporary, 0o600)
            temporary.replace(self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _public(self, row):
        result = {"status": row["status"], "caption_transfer_supported": False}
        if row.get("phase") == "init_reserved" and not row.get("publish_id"):
            result.update(status="needs_attention", reason="init_reserved_do_not_resubmit")
        if row.get("publish_id"):
            result["publish_id"] = row["publish_id"]
        if row.get("reason"):
            result["reason"] = row["reason"]
        return result

    def _json(self, path, body):
        request = Request(BASE + path, data=json.dumps(body, separators=(",", ":")).encode(),
            headers={"Authorization": "Bearer " + self._token, "Content-Type": "application/json; charset=UTF-8"}, method="POST")
        status, _, raw = self._request(request, timeout=30, max_bytes=MAX_JSON_BYTES)
        if status != 200:
            raise TikTokDraftError({401: "tiktok_authorization_expired", 403: "tiktok_permission_or_pending_limit",
                429: "tiktok_rate_limited"}.get(status, "tiktok_request_not_confirmed"))
        if not isinstance(raw, bytes) or len(raw) > MAX_JSON_BYTES:
            raise TikTokDraftError("tiktok_response_invalid")
        try:
            document = json.loads(raw)
        except (ValueError, UnicodeError):
            raise TikTokDraftError("tiktok_response_invalid") from None
        if not isinstance(document, dict) or not isinstance(document.get("error"), dict) or document["error"].get("code") != "ok":
            code = document.get("error", {}).get("code") if isinstance(document, dict) and isinstance(document.get("error"), dict) else None
            raise TikTokDraftError({"access_token_invalid": "tiktok_authorization_expired",
                "scope_not_authorized": "tiktok_upload_scope_missing",
                "spam_risk_too_many_pending_share": "tiktok_pending_drafts_limit",
                "spam_risk_user_banned_from_posting": "tiktok_account_upload_restricted"}.get(code, "tiktok_request_not_confirmed"))
        if not isinstance(document.get("data"), dict):
            raise TikTokDraftError("tiktok_response_invalid")
        return document["data"]

    def send_draft(self, media_path, *, idempotency_key, expected_sha256, caption_revision):
        """Caller has approved this exact revision. Never sends caption text."""
        with self._lock:
            if (not isinstance(idempotency_key, str) or not ID_PATTERN.fullmatch(idempotency_key)
                    or self._token and self._token in idempotency_key
                    or not isinstance(expected_sha256, str) or not HASH_PATTERN.fullmatch(expected_sha256)
                    or not isinstance(caption_revision, str) or not HASH_PATTERN.fullmatch(caption_revision)):
                raise TikTokDraftError("invalid_approved_revision")
            fingerprint = hashlib.sha256((expected_sha256 + ":" + caption_revision).encode()).hexdigest()
            prior = self._rows.get(idempotency_key)
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise TikTokDraftError("approval_revision_changed_do_not_resubmit")
                return self._public(prior)
            if not self._token:
                return {"status": "not_configured", "reason": "tiktok_access_token_missing", "caption_transfer_supported": False}
            source = Path(media_path)
            if source.is_symlink() or not source.is_file() or not 28 <= source.stat().st_size <= MAX_VIDEO_BYTES:
                raise TikTokDraftError("approved_video_invalid")
            # Freeze the approved bytes before reserving or contacting TikTok.
            with tempfile.TemporaryDirectory(prefix="tiktok-approved-") as directory:
                snapshot = Path(directory) / "approved.mp4"
                digest, size = hashlib.sha256(), 0
                with source.open("rb") as incoming, snapshot.open("wb") as target:
                    while True:
                        chunk = incoming.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > MAX_VIDEO_BYTES:
                            raise TikTokDraftError("approved_video_too_large")
                        digest.update(chunk)
                        target.write(chunk)
                if digest.hexdigest() != expected_sha256:
                    raise TikTokDraftError("approved_video_hash_changed")
                with snapshot.open("rb") as target, mmap.mmap(target.fileno(), 0, access=mmap.ACCESS_READ) as raw:
                    if not _mp4(raw):
                        raise TikTokDraftError("approved_video_invalid_mp4")
                _validate_probe(self._probe(snapshot))
                chunk_size, count = chunk_plan(size)
                row = {"fingerprint": fingerprint, "status": "uploading", "publish_id": None,
                       "phase": "init_reserved", "uploaded_bytes": 0}
                self._rows[idempotency_key] = row
                self._save()  # Durable before any mutating network operation.
                try:
                    data = self._json(INIT_PATH, {"source_info": {"source": "FILE_UPLOAD", "video_size": size,
                        "chunk_size": chunk_size, "total_chunk_count": count}})
                    publish_id = data.get("publish_id")
                    if (not isinstance(publish_id, str) or not PUBLISH_PATTERN.fullmatch(publish_id)
                            or self._token and self._token in publish_id):
                        raise TikTokDraftError("tiktok_publish_id_invalid")
                    row.update(publish_id=publish_id, phase="init_accepted")
                    self._save()
                    upload_url = _upload_url(data.get("upload_url"))  # Memory only.
                    with snapshot.open("rb") as target:
                        for index in range(count):
                            start = index * chunk_size
                            length = size - start if index == count - 1 else chunk_size
                            content = target.read(length)
                            request = Request(upload_url, data=content, method="PUT", headers={
                                "Content-Type": "video/mp4", "Content-Length": str(length),
                                "Content-Range": f"bytes {start}-{start + length - 1}/{size}"})
                            status, _, _ = self._request(request, timeout=60, max_bytes=MAX_JSON_BYTES)
                            if status != (201 if index == count - 1 else 206):
                                raise TikTokDraftError("tiktok_upload_not_confirmed")
                            row["uploaded_bytes"] = start + length
                            self._save()
                    row["phase"] = "upload_complete"
                    self._save()
                    return self.refresh(idempotency_key)
                except Exception as exc:
                    row.update(status="needs_attention", reason=exc.reason if isinstance(exc, TikTokDraftError) and exc.reason in SAFE_REASONS
                               else "tiktok_upload_outcome_uncertain_do_not_resubmit")
                    self._save()
                    return self._public(row)

    def refresh(self, idempotency_key):
        with self._lock:
            row = self._rows.get(idempotency_key)
            if row is None:
                return {"status": "needs_attention", "reason": "private_receipt_missing_do_not_resubmit"}
            if row["status"] == "draft_sent" or not row.get("publish_id"):
                return self._public(row)
            if not self._token:
                return {"status": "not_configured", "reason": "tiktok_access_token_missing"}
            try:
                data = self._json(STATUS_PATH, {"publish_id": row["publish_id"]})
                status = data.get("status")
                if status == "SEND_TO_USER_INBOX":
                    row.update(status="draft_sent", phase="inbox_delivered")
                    row.pop("reason", None)
                elif status in ("PROCESSING_UPLOAD", "PROCESSING_DOWNLOAD"):
                    row.update(status="uploading")
                    row.pop("reason", None)
                elif status == "PUBLISH_COMPLETE":
                    # The creator subsequently posted in TikTok. This service
                    # did not call Direct Post; do not label this as a new draft.
                    row.update(status="needs_attention", reason="creator_already_posted_in_tiktok")
                else:
                    row.update(status="needs_attention", reason="tiktok_processing_failed" if status == "FAILED"
                               else "tiktok_status_unknown")
            except Exception as exc:
                row.update(status="needs_attention", reason=exc.reason if isinstance(exc, TikTokDraftError) and exc.reason in SAFE_REASONS
                           else "tiktok_status_temporarily_unavailable")
            self._save()
            return self._public(row)
