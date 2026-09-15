"""Chat Completions wire adapter. No orchestration framework or hidden mock."""
import asyncio
from dataclasses import dataclass, field
import json
import random
from typing import Protocol

import httpx

from .config import Settings


class ProviderError(Exception):
    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(detail)


@dataclass
class ModelTurn:
    message: dict
    stop_reason: str
    usage: dict = field(default_factory=dict)
    request_id: str | None = None
    attempts: int = 1

    @property
    def tool_calls(self):
        return self.message.get("tool_calls") or []


class Provider(Protocol):
    async def generate(self, messages: list[dict], tools: list[dict]) -> ModelTurn: ...


def normalize(payload: dict) -> ModelTurn:
    """Validate the envelope, leaving argument validation to the tool registry."""
    try:
        choice = payload["choices"][0]
        message = choice["message"]
        reason = choice["finish_reason"]
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise ValueError("role")
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise ValueError("Only text responses are supported")
        safe = {"role": "assistant", "content": content}
        # GLM interleaved thinking requires this field to be replayed, not displayed.
        for field_name in ("reasoning_content", "refusal"):
            if message.get(field_name) is not None:
                if not isinstance(message[field_name], str):
                    raise ValueError(field_name)
                safe[field_name] = message[field_name]
        calls = message.get("tool_calls") or []
        if not isinstance(calls, list) or len(calls) > 16:
            raise ValueError("Too many or malformed tool calls")
        seen = set()
        for call in calls:
            cid = call["id"]
            fn = call["function"]
            if not isinstance(cid, str) or not cid or cid in seen or len(cid) > 256:
                raise ValueError("Invalid/duplicate call id")
            if call.get("type") != "function" or not isinstance(fn.get("name"), str):
                raise ValueError("Invalid function")
            if not isinstance(fn.get("arguments"), str):
                raise ValueError("Arguments must be a JSON string")
            seen.add(cid)
        if calls:
            safe["tool_calls"] = calls
        usage = payload.get("usage") or {}
        clean_usage = {k: v for k, v in usage.items() if k in (
            "prompt_tokens", "completion_tokens", "total_tokens"
        ) and type(v) is int and v >= 0}
        request_id = payload.get("id")
        return ModelTurn(safe, reason, clean_usage, request_id if isinstance(request_id, str) else None)
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
        raise ProviderError("protocol_error", "模型响应不符合文本工具调用协议") from exc


class ChatProvider:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=settings.llm_timeout, follow_redirects=False)

    async def close(self):
        await self.client.aclose()

    async def generate(self, messages, tools):
        cfg = self.settings
        if not cfg.configured:
            raise ProviderError("not_configured", "请在 .env 配置 LLM_BASE_URL、LLM_MODEL 和 LLM_API_KEY，再重启服务")
        url = cfg.base_url.rstrip("/") + "/chat/completions"
        body = {"model": cfg.model, "messages": messages, "tools": tools,
                "tool_choice": "auto", "max_tokens": cfg.max_output_tokens,
                "stream": False, **cfg.extra_body}
        last_code = "provider_unavailable"
        for attempt in range(cfg.max_retries + 1):
            try:
                # Hard deadline includes streamed response read; capped to protect memory.
                async with asyncio.timeout(cfg.llm_timeout):
                    async with self.client.stream("POST", url, json=body, headers={
                        "Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"
                    }) as response:
                        if response.status_code in (408, 429) or response.status_code >= 500:
                            last_code = "rate_limited" if response.status_code == 429 else "provider_unavailable"
                        elif response.status_code >= 300:
                            # Never echo response bodies: they can contain keys/prompts.
                            raise ProviderError("provider_http_error", f"模型接口返回 HTTP {response.status_code}；请检查配置与权限")
                        else:
                            data = bytearray()
                            async for chunk in response.aiter_bytes():
                                data.extend(chunk)
                                if len(data) > 2_000_000:
                                    raise ProviderError("response_too_large", "模型响应超过 2 MB 上限")
                            try:
                                turn = normalize(json.loads(data))
                            except (ValueError, UnicodeError) as exc:
                                raise ProviderError("protocol_error", "模型接口没有返回有效 JSON") from exc
                            turn.attempts = attempt + 1
                            return turn
            except (httpx.TimeoutException, TimeoutError):
                last_code = "provider_timeout"
            except httpx.TransportError:
                last_code = "network_error"
            if attempt < cfg.max_retries:
                await asyncio.sleep(min(2 ** attempt * 0.25 + random.random() * 0.1, 2))
        raise ProviderError(last_code, "模型请求在有限次数重试后失败，请稍后再试")

