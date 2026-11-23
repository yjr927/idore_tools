"""OpenRouter transport for the screenplay agent's strict JSON schemas."""
from __future__ import annotations

import json
import math
import os
import threading
from collections import Counter
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def default_model() -> str:
    return os.getenv("OPENROUTER_TEXT_MODEL", "").strip() or "openai/gpt-5.4-mini"


class ModelCallError(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _matches_schema(value: Any, schema: Dict[str, Any]) -> bool:
    """Validate the subset of JSON Schema used by script_agent.schemas."""
    kind = schema["type"]
    if kind == "object":
        props = schema["properties"]
        return (isinstance(value, dict)
                and set(schema["required"]) <= value.keys() <= props.keys()
                and all(_matches_schema(item, props[key]) for key, item in value.items()))
    if kind == "array":
        return isinstance(value, list) and all(_matches_schema(item, schema["items"]) for item in value)
    if kind == "string":
        return isinstance(value, str) and ("enum" not in schema or value in schema["enum"])
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "number":
        return (type(value) in (int, float) and math.isfinite(value)
                and schema.get("minimum", -math.inf) <= value <= schema.get("maximum", math.inf))
    return False


class StructuredModelClient:
    def __init__(self, api_key: Optional[str] = None):
        self._key = (api_key or os.getenv("OPENROUTER_API_KEY", "")).strip()
        if not self._key:
            raise ModelCallError("请在服务端设置 OPENROUTER_API_KEY。")
        self._max_calls = None
        limit = os.getenv("SCRIPT_AGENT_MAX_MODEL_CALLS", "").strip()
        if limit:
            try:
                self._max_calls = int(limit)
            except ValueError:
                raise ModelCallError("SCRIPT_AGENT_MAX_MODEL_CALLS 必须是正整数。") from None
            if self._max_calls < 1:
                raise ModelCallError("SCRIPT_AGENT_MAX_MODEL_CALLS 必须是正整数。")
        self._opener = build_opener(_NoRedirect())
        self._lock = threading.Lock()
        self._calls: Counter = Counter()

    def call_counts(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._calls)

    def generate(
        self,
        *,
        model: str,
        instructions: str,
        input_text: str,
        schema_name: str,
        schema: Dict[str, Any],
        max_output_tokens: int = 12000,
    ) -> Dict[str, Any]:
        if not 1 <= max_output_tokens <= 24000:
            raise ModelCallError("每次请求的输出上限须在 1 至 24000 token 之间。")
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": input_text},
            ],
            "max_tokens": max_output_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            },
            "provider": {"require_parameters": True},
        }
        request = Request(
            OPENROUTER_URL,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + self._key, "Content-Type": "application/json"},
            method="POST",
        )
        with self._lock:
            if self._max_calls is not None and sum(self._calls.values()) >= self._max_calls:
                raise ModelCallError("本次运行已达到 SCRIPT_AGENT_MAX_MODEL_CALLS 调用上限。")
            self._calls[schema_name] += 1
        try:
            with self._opener.open(request, timeout=180) as response:
                body = json.load(response)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            hints = {401: "请检查密钥", 402: "请检查余额和密钥额度", 429: "已限流，请稍后继续"}
            raise ModelCallError(
                f"OpenRouter {schema_name} 请求失败 (HTTP {status})。{hints.get(status, '')}"
            ) from None
        except (UnicodeError, ValueError):
            raise ModelCallError(f"OpenRouter {schema_name} 返回了无效的响应。") from None
        except (URLError, TimeoutError, OSError):
            raise ModelCallError(f"OpenRouter {schema_name} 连接失败或超时。") from None
        try:
            choice = body["choices"][0]
            if choice["finish_reason"] != "stop" or choice["message"].get("refusal"):
                raise ModelCallError(f"OpenRouter {schema_name} 未完整返回结构化结果。")
            result = json.loads(choice["message"]["content"])
            if not _matches_schema(result, schema):
                raise ValueError
        except (KeyError, IndexError, TypeError, ValueError):
            raise ModelCallError(f"OpenRouter {schema_name} 返回的 JSON 不符合所需结构。") from None
        return result
