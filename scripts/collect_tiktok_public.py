#!/usr/bin/env python3
"""Bounded direct TikTok public-page discovery for GitHub-hosted jobs.

No third-party trend feed, undocumented API, login credentials or anti-bot bypass.
The collector reads only visible anchors/text. A found link is not a verified trend.
Playwright is imported only when the CLI runs, so unit tests need no browser.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlencode, urlsplit
import uuid

DEFAULT_QUERIES = (
    ("hands", "easy solo hand dance trend"),
    ("transition", "solo outfit transition trend"),
    ("pov", "solo expression lip sync trend"),
)
MAX_QUERIES = 3
MAX_VIDEOS = 8
MAX_RUN_HISTORY = 90


class PublicPageError(Exception):
    """Expected invalid input; does not contain page state or credentials."""


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def canonical_video(value):
    if not isinstance(value, str):
        raise PublicPageError("Expected a complete TikTok video URL.")
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError:
        raise PublicPageError("Invalid video URL.") from None
    if (parsed.scheme != "https" or parsed.hostname not in ("www.tiktok.com", "tiktok.com")
            or parsed.username or parsed.password or port not in (None, 443)):
        raise PublicPageError("Expected an HTTPS TikTok video URL.")
    match = re.fullmatch(r"/@([A-Za-z0-9_.-]{1,100})/video/(\d{10,25})/?", parsed.path)
    if not match:
        raise PublicPageError("Only individual TikTok video links are accepted.")
    author, ident = match.groups()
    return f"https://www.tiktok.com/@{author}/video/{ident}", author, ident


def access_blocker(text, page_url="", challenge_visible=False, login_dialog_visible=False):
    """Recognize an actual challenge or login gate, not the normal Log in button."""
    value = " ".join(str(text or "").lower().split())
    if challenge_visible or any(phrase in value for phrase in (
        "verify to continue", "verify you are human", "verify that you are human",
        "complete the captcha", "drag the slider", "security verification",
        "请完成验证", "拖动滑块", "请验证", "验证码",
    )):
        return "页面要求人工验证，已停止采集；不会自动处理验证码。"
    try:
        path = urlsplit(page_url).path
    except ValueError:
        path = ""
    if login_dialog_visible or path.startswith(("/login", "/signup")) or any(phrase in value for phrase in (
        "log in to continue", "login to continue", "sign in to continue",
        "log in to search", "log in to see results", "登录后继续", "登录以继续",
    )):
        return "页面要求登录，已停止采集；此任务未配置账号登录。"
    if any(phrase in value for phrase in (
        "access denied", "this page isn't available", "this page is not available",
        "something went wrong", "server error", "访问受限", "页面不可用",
    )):
        return "TikTok 公开页面暂不可读取，已停止本轮采集。"
    return None


def candidate_from_anchor(anchor, kind, query, source_url, stamp):
    url, author, ident = canonical_video(anchor.get("href"))
    title = " ".join(str(anchor.get("text") or anchor.get("label") or "").split())[:300]
    if not title:
        title = f"@{author} 的 TikTok 视频候选"
    return {
        "id": "tiktok-" + ident, "video_id": ident, "url": url,
        "author": author, "title": title, "kind": kind, "kinds": [kind],
        "first_seen_at": stamp, "last_seen_at": stamp, "published_at": None,
        "duration": None, "music_id": "", "music_name": "",
        "metrics": {"views": None, "likes": None, "comments": None, "shares": None},
        "source": "TikTok 公开搜索页", "source_url": source_url,
        "search_query": query, "source_kind": "tiktok_public_browser",
        "assessment": "待看原片",
        "assessment_note": "仅从 TikTok 公开搜索页发现具体视频链接。尚未观看原片核验单人、重复动作、固定机位或多个作者跟拍同一玩法。",
        "motion_verified": False, "trend_verified": False, "favorite": False,
        "availability": "仅发现公开链接；播放待确认",
    }


def extract_candidates(anchors, kind, query, source_url, stamp, limit=MAX_VIDEOS):
    rows, seen = [], set()
    for anchor in anchors:
        if not isinstance(anchor, dict):
            continue
        try:
            row = candidate_from_anchor(anchor, kind, query, source_url, stamp)
        except PublicPageError:
            continue
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def merge_candidates(previous, new_rows):
    """Preserve user flags and original discovery time while updating observations."""
    rows = copy.deepcopy(previous)
    by_id = {row.get("id"): row for row in rows if isinstance(row, dict)}
    added = 0
    for row in new_rows:
        existing = by_id.get(row["id"])
        if existing is None:
            stored = copy.deepcopy(row)
            rows.append(stored)
            by_id[row["id"]] = stored
            added += 1
        else:
            first_seen = existing.get("first_seen_at") or row["first_seen_at"]
            favorite = existing.get("favorite", False)
            kinds = list(dict.fromkeys(existing.get("kinds", [existing.get("kind")]) + row.get("kinds", [row["kind"]])))
            existing.update(copy.deepcopy(row))
            existing.update(first_seen_at=first_seen, favorite=favorite, kinds=[k for k in kinds if k])
    return rows, added


def build_result(previous, new_rows, reports, started_at, finished_at, failure=None):
    videos, added = merge_candidates(previous.get("videos", []), new_rows)
    successes = sum(report["status"] == "ok" for report in reports)
    blocked = sum(report["status"] == "blocked" for report in reports)
    found = len({row["id"] for row in new_rows})
    if failure:
        status, message = "failed", failure
    elif found:
        status = "succeeded" if successes == len(reports) else "partial"
        message = f"直接从 TikTok 公开页面发现 {found} 条视频链接，新增 {added} 条。当前热度和动作仍待核验。"
    elif blocked:
        status, message = "blocked", "TikTok 公开页面未返回可读取的视频列表；可能需要登录或人工验证。已有候选保留。"
    elif successes:
        status, message = "empty", "公开页面读取完成，本次没有发现视频链接。已有候选保留。"
    else:
        status, message = "failed", "本轮未能读取 TikTok 公开搜索页。已有候选保留。"
    run = {
        "id": uuid.uuid4().hex, "status": status, "kind": "all", "query": "",
        "started_at": started_at, "finished_at": finished_at,
        "found": found, "added": added, "message": message, "sources": reports,
    }
    result = {
        "collector": "tiktok_public_browser", "videos": videos,
        "runs": [run] + previous.get("runs", [])[:MAX_RUN_HISTORY - 1],
        "last_attempt_at": finished_at,
        "last_success_at": finished_at if found else previous.get("last_success_at"),
    }
    if "work_imports" in previous:
        result["work_imports"] = copy.deepcopy(previous["work_imports"])
    return result


def diagnostics_path(value):
    """Keep diagnostic files inside a relative workspace directory."""
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise PublicPageError("Diagnostics directory must be relative and cannot contain '..'.")
    try:
        path.resolve().relative_to(Path.cwd().resolve())
    except ValueError:
        raise PublicPageError("Diagnostics directory must stay within the working directory.") from None
    return path


async def save_diagnostics(page, directory, index, report, text=None, anchor_count=None):
    """Save only the public viewport and bounded rendered text, never browser state."""
    if directory is None:
        return
    # Numeric IDs only: no query, caption or website text becomes a filename.
    stem = f"search-{int(index):02d}"
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "search_url": report["url"], "status": report["status"],
        "message": report.get("message", ""), "observed_at": utc_now(),
        "title": None, "url": None, "body_text": None,
        "anchor_count": anchor_count, "errors": [],
    }
    try:
        # The browser only visits anonymous public search pages in this job.
        payload["url"] = str(page.url)[:2000]
        payload["title"] = (await page.title())[:500]
    except Exception as exc:
        payload["errors"].append("page_info:" + type(exc).__name__)
    try:
        if text is None:
            text = await page.locator("body").inner_text(timeout=2000)
        payload["body_text"] = str(text)[:12000]
    except Exception as exc:
        payload["errors"].append("body_text:" + type(exc).__name__)
    try:
        if anchor_count is None:
            payload["anchor_count"] = await page.locator('a[href*="/video/"]:visible').count()
    except Exception as exc:
        payload["errors"].append("anchor_count:" + type(exc).__name__)
    try:
        await page.screenshot(path=str(directory / (stem + ".png")), full_page=False, timeout=5000)
    except Exception as exc:
        payload["errors"].append("screenshot:" + type(exc).__name__)
    # This sidecar is an artifact, not part of the published candidate JSON.
    (directory / (stem + ".json")).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def collect_page(page, kind, query, limit, diagnostics_dir=None, diagnostic_index=1):
    url = "https://www.tiktok.com/search?" + urlencode({"q": query})
    report = {"name": "TikTok · " + query, "url": url, "status": "failed", "found": 0, "total_links": 0}
    text, anchor_count = None, None
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        if response is not None and response.status >= 400:
            report.update(status="blocked" if response.status in (401, 403, 429) else "failed",
                          message=f"TikTok 返回 HTTP {response.status}，未继续访问。")
            return [], report, response.status in (401, 403, 429)
        # Bounded render wait: only wait for visible public video anchors; no private API.
        try:
            await page.locator('a[href*="/video/"]:visible').first.wait_for(state="visible", timeout=8000)
        except Exception:
            pass
        text = (await page.locator("body").inner_text(timeout=5000))[:20000]
        challenge_visible = False
        for selector in ('iframe[src*="captcha"]', '[id*="captcha"]', '[data-testid*="captcha"]'):
            locator = page.locator(selector)
            if await locator.count() and await locator.first.is_visible():
                challenge_visible = True
                break
        login_dialog_visible = False
        dialogs = page.get_by_role("dialog")
        for index in range(min(await dialogs.count(), 3)):
            dialog = dialogs.nth(index)
            if await dialog.is_visible():
                dialog_text = (await dialog.inner_text())[:1500].lower()
                if "log in" in dialog_text or "sign in" in dialog_text or "登录" in dialog_text:
                    login_dialog_visible = True
                    break
        blocker = access_blocker(text, page.url, challenge_visible, login_dialog_visible)
        if blocker:
            report.update(status="blocked", message=blocker)
            return [], report, True
        # Only rendered anchors with visible geometry. Never read hydration blobs or cookies.
        anchors = await page.locator('a[href*="/video/"]').evaluate_all("""elements => elements.filter(el => {
          const style = getComputedStyle(el);
          return el.getClientRects().length > 0 && style.visibility !== 'hidden' && style.display !== 'none';
        }).map(el => ({href:el.href,text:el.innerText,label:el.getAttribute('aria-label')||''})).slice(0,80)""")
        rows = extract_candidates(anchors, kind, query, url, utc_now(), limit)
        anchor_count = len(anchors)
        report.update(status="ok", found=len(rows), total_links=len(anchors))
        if not rows:
            explicit_empty = any(phrase in text.lower() for phrase in ("no results found", "no videos found", "couldn't find", "未找到", "暂无结果"))
            report.update(status="ok" if explicit_empty else "blocked", message="未发现结果。" if explicit_empty else "公开页面没有提供视频列表；未尝试绕过访问限制。")
        return rows, report, False
    except Exception as exc:
        # Error text can contain page content/URLs; expose the class only.
        report.update(status="failed", message=f"公开页面读取失败（{type(exc).__name__}）；已有内容保留。")
        return [], report, False
    finally:
        if diagnostics_dir is not None:
            try:
                await save_diagnostics(page, diagnostics_dir, diagnostic_index, report, text, anchor_count)
            except Exception:
                # Diagnostics must not change or hide the actual collection result.
                pass


async def collect(queries, limit, diagnostics_dir=None):
    from playwright.async_api import async_playwright
    rows, reports, seen = [], [], set()
    async with async_playwright() as driver:
        browser = await driver.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            for diagnostic_index, (kind, query) in enumerate(queries[:MAX_QUERIES], 1):
                if len(rows) >= limit:
                    break
                found, report, stop = await collect_page(
                    page, kind, query, limit - len(rows), diagnostics_dir, diagnostic_index)
                reports.append(report)
                for row in found:
                    if row["id"] not in seen:
                        seen.add(row["id"])
                        rows.append(row)
                if stop:
                    break
        finally:
            await browser.close()
    return rows, reports


def read_previous(path):
    if not path.exists():
        return {"videos": [], "runs": []}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("videos"), list) or not isinstance(value.get("runs"), list):
        raise PublicPageError("Existing output has an invalid shape; it was not overwritten.")
    return value


def write_result(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=8, choices=range(1, MAX_VIDEOS + 1))
    parser.add_argument("--query", action="append", default=[], help="Optional free-text public search; at most 3 queries.")
    parser.add_argument("--diagnostics-dir", help="Optional relative artifact directory for public-page screenshots and rendered text.")
    args = parser.parse_args(argv)
    if len(args.query) > MAX_QUERIES or any(not query.strip() or len(query) > 150 for query in args.query):
        parser.error("Use at most three nonempty search queries, each no longer than 150 characters.")
    try:
        diagnostic_directory = diagnostics_path(args.diagnostics_dir) if args.diagnostics_dir else None
    except PublicPageError as exc:
        parser.error(str(exc))
    try:
        previous = read_previous(args.output)
    except (OSError, ValueError, PublicPageError) as exc:
        print(f"Cannot read previous data: {exc}", file=sys.stderr)
        return 1
    queries = [("hands", query.strip()) for query in args.query] if args.query else list(DEFAULT_QUERIES)
    started = utc_now()
    failure = None
    try:
        rows, reports = asyncio.run(collect(queries, args.limit, diagnostic_directory))
    except ImportError:
        rows, reports = [], []
        failure = "采集环境缺少 Playwright；本轮未启动浏览器。已有候选保留。"
    except Exception as exc:
        rows, reports = [], []
        failure = f"云端浏览器未能运行（{type(exc).__name__}）。已有候选保留。"
    result = build_result(previous, rows, reports, started, utc_now(), failure)
    write_result(args.output, result)
    latest = result["runs"][0]
    print(json.dumps({"status": latest["status"], "found": latest["found"], "added": latest["added"], "message": latest["message"]}, ensure_ascii=False))
    # Publishing an honest failure status is a useful successful workflow output.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
