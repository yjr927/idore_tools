#!/usr/bin/env python3
"""Read a public motion source into private scratch storage; no paid API access."""
import argparse
import hashlib
import json
import mmap
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from video_service import _media_url, _mp4
from scripts.resolve_tiktok_reference import resolve_tiktok_reference
MAX_REFERENCE_BYTES = 200_000_000
class PreflightStopped(Exception):
    def __init__(self, reason):
        self.reason = reason
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def download_reference(url, target):
    """Download and structurally verify the entire MP4 without API credentials."""
    validated = _media_url(url, "video_url")
    request = Request(validated, headers={"Accept": "video/mp4", "Accept-Encoding": "identity",
        "User-Agent": "idore-media-preflight/1.0"}, method="GET")
    count, digest = 0, hashlib.sha256()
    deadline = time.monotonic() + 120
    try:
        with build_opener(NoRedirect).open(request, timeout=30) as response:
            if response.status not in (200, 206):
                raise PreflightStopped("reference_download_failed")
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type not in ("video/mp4", "application/octet-stream"):
                raise PreflightStopped("reference_download_not_mp4")
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdigit() or len(length) > 12):
                raise PreflightStopped("reference_download_length_invalid")
            expected = int(length) if length is not None else None
            if expected is not None and not 0 < expected <= MAX_REFERENCE_BYTES:
                raise PreflightStopped("reference_download_size_unsupported")
            # A CDN may answer a full GET with 206. Accept that only when the
            # declared range explicitly covers the entire object.
            content_range = response.headers.get("Content-Range")
            if response.status == 206 or content_range is not None:
                import re
                match = re.fullmatch(r"bytes 0-([0-9]{1,12})/([0-9]{1,12})", content_range or "")
                if not match or int(match[1]) + 1 != int(match[2]):
                    raise PreflightStopped("reference_download_incomplete")
                total = int(match[2])
                if not 0 < total <= MAX_REFERENCE_BYTES or expected is not None and expected != total:
                    raise PreflightStopped("reference_download_length_invalid")
                expected = total
            descriptor = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as destination:
                while True:
                    if time.monotonic() >= deadline:
                        raise PreflightStopped("reference_download_timed_out")
                    chunk = response.read(min(64 * 1024, MAX_REFERENCE_BYTES - count + 1))
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > MAX_REFERENCE_BYTES:
                        raise PreflightStopped("reference_download_size_unsupported")
                    destination.write(chunk)
                    digest.update(chunk)
                destination.flush()
                os.fsync(destination.fileno())
        if not count or expected is not None and count != expected:
            raise PreflightStopped("reference_download_incomplete")
        with target.open("rb") as downloaded:
            with mmap.mmap(downloaded.fileno(), 0, access=mmap.ACCESS_READ) as contents:
                if not _mp4(contents):
                    raise PreflightStopped("reference_download_not_mp4")
        return {"verified": True, "bytes": count, "sha256": digest.hexdigest()}
    except Exception as exc:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        if isinstance(exc, PreflightStopped):
            raise
        if isinstance(exc, HTTPError) and type(exc.code) is int and 100 <= exc.code <= 599:
            raise PreflightStopped("reference_download_http_" + str(exc.code)) from None
        raise PreflightStopped("reference_download_failed") from None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--summary', required=True)
    args = parser.parse_args()
    summary = {'status': 'stopped', 'source_id': '7706633304702848276'}
    try:
        if os.environ.get('GITHUB_ACTIONS') != 'true':
            raise PreflightStopped('github_cloud_execution_required')
        resolved = resolve_tiktok_reference('https://www.tiktok.com/@bizzyboom/video/7706633304702848276')
        if resolved.video_id != summary['source_id'] or resolved.author.casefold() != 'bizzyboom':
            raise PreflightStopped('source_identity_mismatch')
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        summary.update(download_reference(resolved.url, destination))
        summary.update(status='source_verified', duration=resolved.duration)
    except Exception as exc:
        summary['reason'] = exc.reason if isinstance(exc, PreflightStopped) else 'source_unavailable'
    target = Path(args.summary)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary), flush=True)
    return 0 if summary['status'] == 'source_verified' else 1

if __name__ == '__main__':
    raise SystemExit(main())
