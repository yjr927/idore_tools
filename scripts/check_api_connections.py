#!/usr/bin/env python3
"""Verify saved API keys and model catalogs without generating paid content.

Even if a service implementation changes, the injected transport permits only
the three fixed read-only endpoints below. Artifacts contain whitelisted public
model metadata, never key/account responses, balances or raw error messages.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import creative_service
import video_service


OPENAI_READS = {"https://api.openai.com/v1/models"}
OPENROUTER_READS = {"https://openrouter.ai/api/v1/key", "https://openrouter.ai/api/v1/videos/models"}
MAX_BYTES = 4 * 1024 * 1024
PRICE_KEYS = {"video_tokens", "video_tokens_without_audio", "video_tokens_with_video_input"}


class ReadOnlyViolation(Exception):
    pass


def read_only_transport(delegate, allowed_urls):
    def request(req, *, timeout, max_bytes):
        if req.get_method() != "GET" or req.full_url not in allowed_urls or req.data is not None:
            raise ReadOnlyViolation("Connection checks permit fixed GET endpoints only.")
        return delegate(req, timeout=min(timeout, 30), max_bytes=min(max_bytes, MAX_BYTES))
    return request


def public_string(value, pattern, secrets=()):
    return (isinstance(value, str) and re.fullmatch(pattern, value) is not None
            and not any(secret and secret in value for secret in secrets))


def model_ids(rows, prefix, secrets):
    if not isinstance(rows, list):
        raise ValueError("Invalid model catalog.")
    return sorted({row["id"] for row in rows if isinstance(row, dict)
                   and public_string(row.get("id"), prefix + r"[A-Za-z0-9._-]{1,160}", secrets)})


def video_models(rows, secrets):
    if not isinstance(rows, list):
        raise ValueError("Invalid video catalog.")
    models = []
    for row in rows:
        if not isinstance(row, dict) or not public_string(row.get("id"), r"bytedance/seedance-[a-z0-9.\-]{1,80}", secrets):
            continue
        model = {"id": row["id"]}
        durations = row.get("supported_durations")
        if isinstance(durations, list):
            model["supported_durations"] = [value for value in durations
                if type(value) in (int, float) and math.isfinite(value) and 0 < value <= 3600]
        for key in ("supported_resolutions", "supported_aspect_ratios", "supported_frame_images"):
            if isinstance(row.get(key), list):
                model[key] = [value for value in row[key] if public_string(value, r"[A-Za-z0-9_.:+-]{1,40}", secrets)]
        if type(row.get("generate_audio")) is bool:
            model["generate_audio"] = row["generate_audio"]
        prices = row.get("pricing_skus")
        if isinstance(prices, dict):
            safe = {}
            for label, price in prices.items():
                if label not in PRICE_KEYS:
                    continue
                if type(price) in (int, float) and math.isfinite(price) and 0 <= price <= 1e6:
                    safe[label] = price
                elif public_string(price, r"(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,12})?", secrets):
                    safe[label] = price
            if safe:
                model["pricing_skus"] = safe
        models.append(model)
    return sorted(models, key=lambda model: model["id"])


def failure_code(exc):
    status = getattr(exc, "status", None)
    if type(status) is not int:
        return "connection_or_catalog_failed"
    return {400: "invalid_configuration", 401: "authentication_failed", 402: "access_denied",
            403: "permission_denied", 404: "endpoint_unavailable", 429: "rate_limited"}.get(
                status, "connection_or_catalog_failed")


def check_connections(output):
    keys = {"openai": os.environ.get("OPENAI_API_KEY", "").strip(),
            "openrouter": os.environ.get("OPENROUTER_API_KEY", "").strip()}
    secrets = tuple(value for value in keys.values() if value)
    providers = {}
    for name in ("openai", "openrouter"):
        state = {"status": "failed", "connection_verified": False, "catalog_checked": False}
        providers[name] = state
        if not keys[name]:
            state["reason"] = "missing_secret"
            continue
        try:
            if name == "openai":
                service = creative_service.CreativeService(output / "openai-check",
                    request_fn=read_only_transport(creative_service._transport, OPENAI_READS))
            else:
                service = video_service.VideoService(output / "openrouter-check",
                    request_fn=read_only_transport(video_service._transport, OPENROUTER_READS))
            verified = service.connect({"api_key": keys[name]})
            if not isinstance(verified, dict) or verified.get("connected") is not True:
                raise ValueError("Connection was not verified.")
            state["connection_verified"] = True
            catalog = service.models()
            if not isinstance(catalog, dict):
                raise ValueError("Invalid model catalog.")
            if name == "openai":
                state["text_models"] = model_ids(catalog.get("text_models"), r"gpt-", secrets)
                state["image_models"] = model_ids(catalog.get("image_models"), r"gpt-image-", secrets)
            else:
                state["video_models"] = video_models(catalog.get("models"), secrets)
            state.update(status="passed", catalog_checked=True)
        except Exception as exc:
            # Neither str(exc) nor any provider response becomes an artifact/log.
            state["reason"] = failure_code(exc)
    passed = sum(state["status"] == "passed" for state in providers.values())
    return {"schema_version": 1, "checked_at": datetime.now(timezone.utc).isoformat(),
            "status": "passed" if passed == 2 else "partial" if passed else "failed",
            "paid_requests": 0, "providers": providers}


def step_summary(summary):
    lines = ["## API 连接检查", "", "仅验证密钥与模型列表；未提交图片、视频、文案或联网检索生成。", ""]
    for name, label in (("openai", "OpenAI"), ("openrouter", "OpenRouter")):
        provider = summary["providers"][name]
        lines.append("- **" + label + "**: " + ("通过" if provider["status"] == "passed" else "未通过") +
                     ("（" + provider["reason"] + "）" if provider.get("reason") else ""))
        if name == "openai" and provider["status"] == "passed":
            for key, title in (("text_models", "文案模型"), ("image_models", "图片模型")):
                ids = provider.get(key, [])
                preview = "、".join("`" + ident + "`" for ident in ids[:8]) or "无匹配模型"
                lines.append("  - " + title + "（" + str(len(ids)) + "）: " + preview)
        elif name == "openrouter" and provider["status"] == "passed":
            rows = provider.get("video_models", [])
            preview = "、".join("`" + row["id"] + "`" for row in rows[:8]) or "无匹配模型"
            lines.append("  - Seedance 模型（" + str(len(rows)) + "）: " + preview)
    lines.extend(["", "全部明确模型 ID、视频能力与可用的目录报价见附件 `api-check-summary.json`；不展示账户余额或账号信息。", ""])
    return "\n".join(lines)


def main():
    output = Path(os.environ.get("API_CHECK_OUTPUT_DIR", "generation-output"))
    try:
        output.mkdir(parents=True, exist_ok=True)
        summary = check_connections(output)
        target = output / "api-check-summary.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        temporary.replace(target)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
                stream.write(step_summary(summary))
        for name, provider in summary["providers"].items():
            print(name + ": " + provider["status"], flush=True)
        print(json.dumps(summary, ensure_ascii=False, separators=(",", ":"), allow_nan=False), flush=True)
        return 0 if summary["status"] == "passed" else 1
    except Exception:
        print("无法保存连接检查结果；未提交任何付费生成。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
