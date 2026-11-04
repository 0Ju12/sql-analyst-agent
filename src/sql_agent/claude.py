"""Claude 어댑터 (Anthropic Messages API, 수동 도구 루프).

- 인증: ANTHROPIC_API_KEY 환경변수 등 SDK 기본 방식. 키를 코드·로그에 남기지 않는다.
- 모델 응답 블록(생각 블록 포함)은 다음 요청에 그대로 돌려보낸다. 대화 기록은 덧붙이기만 한다.
- 안전 분류기가 요청을 거절하면 서버 쪽 대체 모델(fallbacks="default")이 이어받는다.
  실제로 응답한 모델은 ModelTurn.served_model로 기록한다.
- 도구·시스템 프롬프트는 매 요청 같으므로 프롬프트 캐시를 켠다.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .models import ModelTurn, ToolCall
from .tools import Tool

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ModelRefusal(Exception):
    """대체 모델까지 모두 거절한 경우."""


def to_api_tools(tools: Sequence[Tool]) -> list[dict]:
    return [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in tools
    ]


def to_api_messages(messages: list[dict]) -> list[dict]:
    """에이전트의 중립 메시지 → Messages API 형식."""
    out: list[dict] = []
    for m in messages:
        role = m["role"]
        if role == "user":
            out.append({"role": "user", "content": m["content"]})
        elif role == "assistant":
            if m.get("raw") is not None:
                content: Any = m["raw"]  # 원본 블록을 바꾸지 않고 그대로
            else:
                content = []
                if m.get("text"):
                    content.append({"type": "text", "text": m["text"]})
                for c in m.get("tool_calls", []):
                    content.append(
                        {"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["args"]}
                    )
            out.append({"role": "assistant", "content": content})
        elif role == "tool":
            # 한 턴의 도구 결과는 한 메시지에 모두 담는다
            out.append({
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": r["tool_call_id"],
                        "content": r["content"],
                        "is_error": r["is_error"],
                    }
                    for r in m["results"]
                ],
            })  # fmt: skip
        else:
            raise ValueError(f"알 수 없는 메시지 role: {role}")
    return out


class ClaudeModel:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        effort: str = "medium",
        max_tokens: int = 16000,
        fallbacks: bool = True,
        client: Any = None,
    ):
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self.client = client
        self.model = model
        self.name = f"{model}@{effort}"
        self.effort = effort
        self.max_tokens = max_tokens
        self.fallbacks = fallbacks

    def complete(self, system: str, messages: list[dict], tools: Sequence[Tool]) -> ModelTurn:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "tools": to_api_tools(tools),
            "messages": to_api_messages(messages),
            "output_config": {"effort": self.effort},
            "cache_control": {"type": "ephemeral"},
        }
        if self.fallbacks:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        resp = self.client.beta.messages.create(**kwargs)

        if resp.stop_reason == "refusal":
            details = getattr(resp, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise ModelRefusal(f"모델이 요청을 거절했습니다 (분류: {category})")

        text = "".join(b.text for b in resp.content if b.type == "text")
        calls = [
            ToolCall(id=b.id, name=b.name, args=dict(b.input or {}))
            for b in resp.content
            if b.type == "tool_use"
        ]
        if resp.stop_reason == "max_tokens" and calls:
            # 잘린 도구 입력은 실행하지 않는다
            calls = []
            text += "\n(응답이 max_tokens에서 잘렸습니다)"
        u = resp.usage
        cached = (getattr(u, "cache_read_input_tokens", 0) or 0) + (
            getattr(u, "cache_creation_input_tokens", 0) or 0
        )
        return ModelTurn(
            text=text,
            tool_calls=calls,
            input_tokens=(u.input_tokens or 0) + cached,
            output_tokens=u.output_tokens or 0,
            raw=resp.content,
            served_model=getattr(resp, "model", None),
        )
