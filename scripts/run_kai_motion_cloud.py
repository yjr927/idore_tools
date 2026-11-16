#!/usr/bin/env python3
"""One authorized KAI motion-reference run; transient player URLs stay in memory.

The browser opens only the public player, uses no login state or stealth options,
and stops at access challenges. The full reference MP4 is verified in a private
temporary directory and deleted on exit; it is never a job artifact. Browser
state, resolved URLs and environment dumps are never written. Paid submission is
delegated once to the existing video runner after complete-source verification.
"""
from __future__ import annotations

import asyncio
from contextlib import redirect_stderr
import hashlib
import io
import json
import math
import mmap
import os
from pathlib import Path
import sys
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from video_service import _media_url, _mp4
try:
    from scripts import run_video_generation
except ImportError:
    import run_video_generation


PLAYER_URL = "https://www.tiktok.com/player/v1/7706633304702848276"
IMAGE_URL = "https://raw.githubusercontent.com/yjr927/idore_tools/bd19680c2f14b89be1a1025b7825d9e49bd4b433/reference-assets/kai-full.png"
MODEL = "bytedance/seedance-2.0-fast"
MAX_REFERENCE_BYTES = 200_000_000
PROMPT = (
    "Create one 6-second vertical video with one character, KAI. The IMAGE reference defines KAI's identity: "
    "preserve the exact face, hairstyle, hair color, body proportions, butterfly-pattern outfit, colors and accessories. "
    "Use the VIDEO reference ONLY for the visible hand gestures/dance, opening movement rhythm, pose changes, "
    "camera framing and motion. Keep KAI from the image throughout; do not replace KAI with the person from the video "
    "or copy that person's face, hair or clothes. Keep one character, with no extra people, text or logos. "
    "Follow the first six seconds of the motion reference as closely as the model allows. Do not generate audio."
)


class PreflightStopped(Exception):
    """A fixed status code, never a provider/browser error or URL."""
    def __init__(self, reason):
        self.reason = reason


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


async def access_blocked(page, diagnostics=None):
    def blocked(reason):
        if diagnostics is not None:
            diagnostics["access_gate"] = reason
        return True
    path = urlsplit(page.url).path.lower()
    if "/login" in path:
        return blocked("login_required")
    if "/captcha" in path:
        return blocked("challenge_required")
    for selector in ('iframe[src*="captcha"]', '[id*="captcha"]', '[data-testid*="captcha"]'):
        locators = page.locator(selector)
        if await locators.count() and await locators.first.is_visible():
            return blocked("challenge_required")
    dialogs = page.get_by_role("dialog")
    for index in range(min(await dialogs.count(), 3)):
        dialog = dialogs.nth(index)
        if await dialog.is_visible():
            content = (await dialog.inner_text(timeout=1500)).lower()
            if any(text in content for text in ("log in", "sign in", "登录")):
                return blocked("login_required")
            if "验证码" in content:
                return blocked("challenge_required")
    text = (await page.locator("body").inner_text(timeout=1500))[:12000].lower()
    for reason, phrases in (("login_required", ("log in to continue", "log in to watch", "sign in to continue", "登录后观看")),
            ("challenge_required", ("verify to continue", "verify you are human", "complete the captcha", "安全验证", "拖动滑块")),
            ("age_verification_required", ("when's your birthday", "when’s your birthday", "enter your birthdate", "verify your age"))):
        if any(phrase in text for phrase in phrases):
            return blocked(reason)
    return False


def access_failure(diagnostics):
    return {"login_required": "public_player_login_required", "challenge_required": "public_player_challenge_required",
        "age_verification_required": "public_player_age_verification_required"}.get(
            diagnostics.get("access_gate"), "public_player_access_blocked")


def player_ui_state():
    return {"play_seen": False, "play_clicked": False, "all_video_count": 0,
            "visible_video_count": 0, "src_kind": "empty", "network_loading": False,
            "page_on_public_player": False, "access_gate": "none"}


async def _poll_player(page, deadline, diagnostics):
    while time.monotonic() < deadline:
        location = urlsplit(page.url)
        diagnostics["page_on_public_player"] = (location.scheme == "https" and location.hostname in ("www.tiktok.com", "tiktok.com")
            and location.path == "/player/v1/7706633304702848276")
        if await access_blocked(page, diagnostics):
            raise PreflightStopped(access_failure(diagnostics))
        diagnostics["all_video_count"] = min(100, await page.locator("video").count())
        videos = page.locator("video:visible")
        count = await videos.count()
        diagnostics["visible_video_count"] = min(100, count)
        media = {}
        if count == 1:
            try:
                media = await videos.first.evaluate(
                    "video => ({url: video.currentSrc, duration: video.duration, networkState: video.networkState})", timeout=3000)
            except Exception:
                pass  # A normal DOM replacement can detach a player mid-poll.
        url = media.get("url") if isinstance(media, dict) else None
        duration = media.get("duration") if isinstance(media, dict) else None
        diagnostics["src_kind"] = ("https" if isinstance(url, str) and url.startswith("https://")
            else "blob" if isinstance(url, str) and url.startswith("blob:") else "empty" if not url else "other")
        diagnostics["network_loading"] = isinstance(media, dict) and media.get("networkState") == 2
        if (isinstance(url, str) and url.startswith("https://") and type(duration) in (int, float)
                and math.isfinite(duration) and duration > 0):
            return url, duration
        # React controls may appear well after DOMContentLoaded. Use only a
        # normal visible/enabled Play control, once successfully clicked; a
        # detached control may be tried again on the next bounded poll.
        for label in ("Play video", "Play"):
            play = page.get_by_role("button", name=label, exact=True)
            try:
                if await play.count() and await play.first.is_visible():
                    diagnostics["play_seen"] = True
                    if (not diagnostics["play_clicked"] and not diagnostics["network_loading"]
                            and await play.first.is_enabled(timeout=1000)):
                        await play.first.click(timeout=2000)
                        diagnostics["play_clicked"] = True
                        break
            except Exception:
                pass
        await page.wait_for_timeout(500)
    raise PreflightStopped("public_player_has_no_playable_video")


async def _resolve_player(diagnostics=None):
    from playwright.async_api import async_playwright
    diagnostics = diagnostics if diagnostics is not None else player_ui_state()
    deadline = time.monotonic() + 105
    async with async_playwright() as playwright:
        browser_env = {key: value for key, value in os.environ.items()
                       if key not in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "GENERATION_INPUTS")}
        browser = await playwright.chromium.launch(headless=True, timeout=25000, env=browser_env)
        try:
            # Default fresh context: no cookies, stored login, custom user agent,
            # request interception, tracing, screenshots or challenge handling.
            context = await browser.new_context()
            page = await context.new_page()
            response = await page.goto(PLAYER_URL, wait_until="domcontentloaded", timeout=45000)
            if response is not None and response.status >= 400:
                raise PreflightStopped("public_player_unavailable")
            if await access_blocked(page, diagnostics):
                raise PreflightStopped(access_failure(diagnostics))
            return await _poll_player(page, deadline, diagnostics)
        finally:
            try:
                await asyncio.wait_for(browser.close(), timeout=5)
            except Exception:
                pass


async def resolve_player(diagnostics=None):
    try:
        return await asyncio.wait_for(_resolve_player(diagnostics), timeout=115)
    except PreflightStopped:
        raise
    except Exception:
        raise PreflightStopped("public_player_resolution_failed") from None


def range_preflight(url, kind):
    validated = _media_url(url, kind)
    request = Request(validated, headers={"Range": "bytes=0-1023", "Accept-Encoding": "identity",
        "Accept": "image/png" if kind == "image_url" else "video/mp4", "User-Agent": "idore-media-preflight/1.0"}, method="GET")
    # This opener never receives API credentials and never follows redirects.
    try:
        with build_opener(NoRedirect).open(request, timeout=15) as response:
            status = response.status
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            prefix = response.read(1024)
    except Exception:
        raise PreflightStopped("public_media_preflight_failed") from None
    if status not in (200, 206):
        raise PreflightStopped("public_media_preflight_failed")
    valid = (content_type in ("image/png", "application/octet-stream") and prefix.startswith(b"\x89PNG\r\n\x1a\n")) if kind == "image_url" else (
        content_type in ("video/mp4", "application/octet-stream") and len(prefix) >= 12 and prefix[4:8] == b"ftyp")
    if not valid:
        raise PreflightStopped("public_media_format_unverified")


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
        raise PreflightStopped("reference_download_failed") from None


def write_summary(output, summary):
    output.mkdir(parents=True, exist_ok=True)
    path = output / "cloud-generation-summary.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main(resolved_reference=None):
    output = Path(os.environ.get("VIDEO_OUTPUT_DIR", "generation-output"))
    private_source = None
    summary = {"schema_version": 1, "status": "stopped", "player_resolution": "not_run",
               "image_preflight": "not_run", "video_preflight": "not_run", "generation_runner_invoked": False,
               "player_ui": player_ui_state(), "reference_download": {"verified": False}}
    try:
        if os.environ.get("GITHUB_RUN_ATTEMPT", "1") != "1":
            raise PreflightStopped("rerun_refused_to_avoid_duplicate_charge")
        if os.environ.get("KAI_ALLOW_PAID") != "true":
            raise PreflightStopped("explicit_paid_authorization_required")
        if os.environ.get("GITHUB_ACTIONS") != "true":
            raise PreflightStopped("authorized_github_run_required")
        if not os.environ.get("OPENROUTER_API_KEY", "").strip():
            raise PreflightStopped("openrouter_secret_missing")
        if resolved_reference is None:
            with redirect_stderr(io.StringIO()):
                url, duration = asyncio.run(resolve_player(summary["player_ui"]))
            summary["player_resolution"] = "passed"
        else:
            if not isinstance(resolved_reference, tuple) or len(resolved_reference) != 2:
                raise PreflightStopped("provided_reference_invalid")
            url, duration = resolved_reference
            summary["player_resolution"] = "skipped_for_in_memory_reference"
        if type(duration) not in (int, float) or not math.isfinite(duration) or not 2 <= duration <= 15.05:
            raise PreflightStopped("reference_video_duration_unsupported")
        _media_url(url, "video_url")
        range_preflight(IMAGE_URL, "image_url")
        summary["image_preflight"] = "passed"
        private_source = tempfile.TemporaryDirectory(prefix="kai-motion-")
        summary["reference_download"] = download_reference(url, Path(private_source.name) / "reference.mp4")
        summary["video_preflight"] = "passed"
        settings = {"model": MODEL, "mode": "references", "prompt": PROMPT, "duration": 6,
            "resolution": "720p", "aspect_ratio": "9:16", "generate_audio": False, "allow_paid": True,
            "first_frame_url": IMAGE_URL, "reference_video_url": url}
        previous = os.environ.get("GENERATION_INPUTS")
        summary["generation_runner_invoked"] = True
        try:
            # The full local MP4 has now been downloaded and verified. This API
            # contract receives its verified origin URL, not a binary upload;
            # neither that URL nor the private source file becomes an artifact.
            os.environ["GENERATION_INPUTS"] = json.dumps(settings)
            # Existing runner logs only task states; suppress its error channel
            # here to avoid exposing any unexpected upstream/browser details.
            with redirect_stderr(io.StringIO()):
                status = run_video_generation.main()
        finally:
            if previous is None:
                os.environ.pop("GENERATION_INPUTS", None)
            else:
                os.environ["GENERATION_INPUTS"] = previous
        summary["status"] = "completed" if status == 0 else "not_completed"
        write_summary(output, summary)
        print("KAI cloud run: " + summary["status"], flush=True)
        return 0 if status == 0 else 1
    except Exception as exc:
        if summary["generation_runner_invoked"]:
            summary["status"] = "not_completed"
        summary["reason"] = exc.reason if isinstance(exc, PreflightStopped) else "preflight_or_generation_failed"
        try:
            write_summary(output, summary)
        except OSError:
            pass
        print("KAI cloud run stopped: " + summary["reason"], flush=True)
        return 1
    finally:
        if private_source is not None:
            try:
                private_source.cleanup()
            except OSError:
                print("KAI private source cleanup could not finish; no source file was added to artifacts.", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
