"""模型客户端。

默认走 **OpenAI 兼容协议**（`POST {base}/chat/completions` + `tools`），
所以 DeepSeek / 通义 / Kimi / 火山方舟 / 各类中转站都能直接接，
只改 `.env` 里三个变量，**不用改代码**。

没配凭据时 `get_llm()` 返回 None，上层自动降级为规则模式（见 `rules.py`）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from .. import config


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMReply:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class LLMClient(Protocol):
    name: str

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> LLMReply: ...


class LLMError(RuntimeError):
    pass


class OpenAICompatibleClient:
    """OpenAI 兼容的 /chat/completions 客户端（用 httpx，没引 SDK）。"""

    name = "openai-compatible"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout: int | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout or config.LLM_TIMEOUT

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> LLMReply:
        payload: dict[str, Any] = {"model": self.model, "messages": messages}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        try:
            response = httpx.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"连不上模型服务：{exc}") from exc

        if response.status_code != 200:
            # 别把 key 回显到日志/页面里
            raise LLMError(f"模型服务返回 {response.status_code}：{response.text[:200]}")

        try:
            body = response.json()
            choice = body["choices"][0]["message"]
        except (KeyError, IndexError, ValueError) as exc:
            raise LLMError("模型返回的格式不认识") from exc

        calls: list[ToolCall] = []
        for index, raw in enumerate(choice.get("tool_calls") or []):
            function = raw.get("function") or {}
            calls.append(
                ToolCall(
                    id=raw.get("id") or f"call_{index}",
                    name=function.get("name") or "",
                    arguments=_parse_arguments(function.get("arguments")),
                )
            )
        return LLMReply(content=choice.get("content"), tool_calls=calls)


def _parse_arguments(raw: Any) -> dict:
    """模型给的 arguments 是 JSON 字符串（有时会带 markdown 围栏），尽量宽容地解析。"""
    import json

    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[-1] if "\n" in text else text
    try:
        parsed = json.loads(text)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


class ScriptedLLM:
    """测试替身：按预设脚本依次返回，用来确定性地验证编排与安全边界。"""

    name = "scripted"

    def __init__(self, replies: list[LLMReply]) -> None:
        self._replies = list(replies)
        self.seen_messages: list[list[dict]] = []

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> LLMReply:
        self.seen_messages.append([dict(m) for m in messages])
        if not self._replies:
            return LLMReply(content="（脚本已用完）")
        return self._replies.pop(0)


_client: LLMClient | None = None
_configured: bool | None = None


def is_configured() -> bool:
    return bool(config.LLM_BASE_URL and config.LLM_API_KEY and config.LLM_MODEL)


def get_llm() -> LLMClient | None:
    """没配凭据就返回 None，让上层走规则模式。"""
    global _client, _configured
    if not is_configured():
        return None
    if _client is None or _configured is not True:
        _client = OpenAICompatibleClient(config.LLM_BASE_URL, config.LLM_API_KEY, config.LLM_MODEL)
        _configured = True
    return _client


def set_client(client: LLMClient | None) -> None:
    """仅供测试：注入替身。"""
    global _client, _configured
    _client = client
    _configured = True if client is not None else None


def reset_client() -> None:
    global _client, _configured
    _client = None
    _configured = None
