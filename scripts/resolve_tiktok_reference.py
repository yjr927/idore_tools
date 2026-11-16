#!/usr/bin/env python3
"""Resolve a public TikTok video through TikWM without publishing its media URL.

Only full https://www.tiktok.com/@handle/video/ID links are accepted. Short
links and redirects are deliberately unsupported. TikWM is a third-party
service linked by tikwm.com, not a TikTok API. This helper makes one keyless
metadata request, never generates video, and never downloads or persists media.
The caller must still download and verify the complete MP4 before submission.
Running this file prints safe metadata only; the resolved URL stays in memory.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import re
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from video_service import VideoError, _media_url


API_URL = "https://www.tikwm.com/api/"
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_REFERENCE_BYTES = 200_000_000
REQUEST_TIMEOUT = 25
AUTHOR_PATTERN = re.compile(r"[A-Za-z0-9_.]{1,64}\Z")
SOURCE_PATH = re.compile(r"/@([A-Za-z0-9_.]{1,64})/video/([0-9]{10,25})/?\Z")
CDN_HOST = re.compile(r"v[0-9]+[a-z]?\.tiktokcdn-us\.com\Z")


class TikTokReferenceError(Exception):
    """A fixed, public reason code; upstream bodies and URLs are never included."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ResolvedTikTokReference:
    url: str = field(repr=False)
    duration: float
    video_id: str
    author: str
    media_type: str = "video/mp4"

    def safe_summary(self):
        return {"video_id": self.video_id, "author": self.author,
                "duration": self.duration, "media_type": self.media_type}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _transport(request, *, timeout, max_bytes):
    if request.full_url != API_URL or request.get_method() != "POST":
        raise TikTokReferenceError("unsupported_api_endpoint")
    try:
        response = build_opener(_NoRedirect).open(request, timeout=timeout)
    except HTTPError as exc:
        # Do not read or interpolate the upstream response body.
        return exc.code, {}, b""
    with response:
        if response.geturl() != API_URL:
            raise TikTokReferenceError("upstream_redirect_rejected")
        raw = response.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise TikTokReferenceError("upstream_response_too_large")
        return response.status, {}, raw


def _source(public_url):
    if (not isinstance(public_url, str) or not 1 <= len(public_url) <= 4096
            or any(ord(char) < 33 for char in public_url)):
        raise TikTokReferenceError("invalid_tiktok_url")
    try:
        parsed = urlsplit(public_url)
        if (parsed.scheme != "https" or parsed.hostname not in ("www.tiktok.com", "tiktok.com")
                or parsed.username or parsed.password or parsed.port not in (None, 443)
                or parsed.fragment):
            raise ValueError()
        match = SOURCE_PATH.fullmatch(parsed.path)
        if match is None:
            raise ValueError()
    except ValueError:
        raise TikTokReferenceError("invalid_tiktok_url") from None
    author, video_id = match.groups()
    # Share-tracking parameters are unnecessary and are never sent upstream.
    return f"https://www.tiktok.com/@{author}/video/{video_id}", author, video_id


def resolve_tiktok_reference(public_url, request_fn=None):
    """Return ResolvedTikTokReference; no paid calls, retries, files, or logging.

    An injected transport receives Request plus timeout/max_bytes and returns
    (HTTP status, headers, bytes). Returned author and video ID must both match
    the supplied public link. Only the observed US TikTok CDN family is allowed.
    """
    canonical, expected_author, expected_id = _source(public_url)
    request = Request(API_URL, data=urlencode({"url": canonical, "hd": "0"}).encode("ascii"),
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "idore-tiktok-reference/1.0"}, method="POST")
    try:
        status, _, raw = (request_fn or _transport)(request, timeout=REQUEST_TIMEOUT,
                                                  max_bytes=MAX_JSON_BYTES)
    except TikTokReferenceError:
        raise
    except Exception:
        raise TikTokReferenceError("upstream_unavailable") from None
    if status != 200:
        raise TikTokReferenceError("upstream_http_error")
    if not isinstance(raw, bytes) or len(raw) > MAX_JSON_BYTES:
        raise TikTokReferenceError("upstream_response_too_large")
    try:
        document = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise TikTokReferenceError("invalid_upstream_response") from None
    if (not isinstance(document, dict) or type(document.get("code")) is not int
            or document["code"] != 0):
        raise TikTokReferenceError("source_unavailable")
    data = document.get("data")
    if not isinstance(data, dict):
        raise TikTokReferenceError("invalid_upstream_response")
    video_id = data.get("id")
    author_data = data.get("author")
    author = author_data.get("unique_id") if isinstance(author_data, dict) else None
    if video_id != expected_id:
        raise TikTokReferenceError("source_id_mismatch")
    if (not isinstance(author, str) or not AUTHOR_PATTERN.fullmatch(author)
            or author.casefold() != expected_author.casefold()):
        raise TikTokReferenceError("source_author_mismatch")
    duration = data.get("duration")
    if (type(duration) not in (int, float) or not 0 < duration <= 3600
            or not math.isfinite(duration)):
        raise TikTokReferenceError("invalid_source_duration")
    size = data.get("size")
    if type(size) is not int or not 0 < size <= MAX_REFERENCE_BYTES:
        raise TikTokReferenceError("invalid_source_size")
    url = data.get("play")
    try:
        if not isinstance(url, str) or not CDN_HOST.fullmatch(urlsplit(url).hostname or ""):
            raise ValueError()
        _media_url(url, "video_url")
    except (ValueError, VideoError):
        raise TikTokReferenceError("unsupported_source_media") from None
    return ResolvedTikTokReference(url=url, duration=float(duration), video_id=video_id, author=author)


def main():
    if len(sys.argv) != 2:
        print(json.dumps({"status": "stopped", "reason": "one_full_tiktok_url_required"}))
        return 1
    try:
        result = resolve_tiktok_reference(sys.argv[1])
    except TikTokReferenceError as exc:
        print(json.dumps({"status": "stopped", "reason": exc.code}))
        return 1
    print(json.dumps(result.safe_summary(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
