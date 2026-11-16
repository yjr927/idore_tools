#!/usr/bin/env python3
"""One authorized KAI motion-reference run; transient player URLs stay in memory.

The browser opens only the public player, uses no login state or stealth options,
and stops at access challenges. No source media, browser state, resolved URLs or
environment dumps are written. Paid submission is delegated once to the existing
video runner after public media preflights pass.
"""
from __future__ import annotations

import asyncio
from contextlib import redirect_stderr
import io
import json
import math
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from video_service import _media_url
try:
    from scripts import run_video_generation
except ImportError:
    import run_video_generation


PLAYER_URL = "https://www.tiktok.com/player/v1/7706633304702848276"
IMAGE_URL = "https://raw.githubusercontent.com/yjr927/idore_tools/bd19680c2f14b89be1a1025b7825d9e49bd4b433/reference-assets/kai-full.png"
MODEL = "bytedance/seedance-2.0-fast"
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


async def access_blocked(page):
    path = urlsplit(page.url).path.lower()
    if "/login" in path or "/captcha" in path:
        return True
    for selector in ('iframe[src*="captcha"]', '[id*="captcha"]', '[data-testid*="captcha"]'):
        locators = page.locator(selector)
        if await locators.count() and await locators.first.is_visible():
            return True
    dialogs = page.get_by_role("dialog")
    for index in range(min(await dialogs.count(), 3)):
        dialog = dialogs.nth(index)
        if await dialog.is_visible():
            content = (await dialog.inner_text(timeout=1500)).lower()
            if any(text in content for text in ("log in", "sign in", "登录", "验证码")):
                return True
    text = (await page.locator("body").inner_text(timeout=1500))[:12000].lower()
    return any(phrase in text for phrase in (
        "log in to continue", "log in to watch", "sign in to continue", "verify to continue",
        "verify you are human", "complete the captcha", "when's your birthday", "when’s your birthday",
        "enter your birthdate", "verify your age", "安全验证", "拖动滑块", "登录后观看"))


async def _resolve_player():
    from playwright.async_api import async_playwright
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
            if await access_blocked(page):
                raise PreflightStopped("public_player_access_blocked")
            play = page.get_by_role("button", name="Play video", exact=True)
            if await play.count() and await play.first.is_visible():
                await play.first.click(timeout=5000)
            while time.monotonic() < deadline:
                if await access_blocked(page):
                    raise PreflightStopped("public_player_access_blocked")
                videos = page.locator("video:visible")
                if await videos.count():
                    media = await videos.first.evaluate("video => ({url: video.currentSrc, duration: video.duration})", timeout=3000)
                    url = media.get("url") if isinstance(media, dict) else None
                    duration = media.get("duration") if isinstance(media, dict) else None
                    if (isinstance(url, str) and url.startswith("https://") and type(duration) in (int, float)
                            and math.isfinite(duration) and duration > 0):
                        return url, duration
                await page.wait_for_timeout(500)
            raise PreflightStopped("public_player_has_no_playable_video")
        finally:
            try:
                await asyncio.wait_for(browser.close(), timeout=5)
            except Exception:
                pass


async def resolve_player():
    try:
        return await asyncio.wait_for(_resolve_player(), timeout=115)
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


def write_summary(output, summary):
    output.mkdir(parents=True, exist_ok=True)
    path = output / "cloud-generation-summary.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main():
    output = Path(os.environ.get("VIDEO_OUTPUT_DIR", "generation-output"))
    summary = {"schema_version": 1, "status": "stopped", "player_resolution": "not_run",
               "image_preflight": "not_run", "video_preflight": "not_run", "generation_runner_invoked": False}
    try:
        if os.environ.get("GITHUB_RUN_ATTEMPT", "1") != "1":
            raise PreflightStopped("rerun_refused_to_avoid_duplicate_charge")
        if os.environ.get("KAI_ALLOW_PAID") != "true":
            raise PreflightStopped("explicit_paid_authorization_required")
        if os.environ.get("GITHUB_ACTIONS") != "true":
            raise PreflightStopped("authorized_github_run_required")
        if not os.environ.get("OPENROUTER_API_KEY", "").strip():
            raise PreflightStopped("openrouter_secret_missing")
        with redirect_stderr(io.StringIO()):
            url, duration = asyncio.run(resolve_player())
        summary["player_resolution"] = "passed"
        if type(duration) not in (int, float) or not math.isfinite(duration) or not 2 <= duration <= 15.05:
            raise PreflightStopped("reference_video_duration_unsupported")
        _media_url(url, "video_url")
        range_preflight(IMAGE_URL, "image_url")
        summary["image_preflight"] = "passed"
        range_preflight(url, "video_url")
        summary["video_preflight"] = "passed"
        settings = {"model": MODEL, "mode": "references", "prompt": PROMPT, "duration": 6,
            "resolution": "720p", "aspect_ratio": "9:16", "generate_audio": False, "allow_paid": True,
            "first_frame_url": IMAGE_URL, "reference_video_url": url}
        previous = os.environ.get("GENERATION_INPUTS")
        summary["generation_runner_invoked"] = True
        try:
            # The dynamic URL is never a workflow input, source file or artifact.
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


if __name__ == "__main__":
    raise SystemExit(main())
