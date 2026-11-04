"""모델 어댑터.

에이전트 루프는 아래 중립 형식의 메시지만 다룬다.
  {"role": "user", "content": str}
  {"role": "assistant", "text": str, "tool_calls": [{"id", "name", "args"}]}
  {"role": "tool", "results": [{"tool_call_id", "name", "content", "is_error"}]}
각 어댑터가 이 형식을 해당 API 형식으로 바꾼다.

- ScriptedModel: 정해진 순서대로 도구를 부르는 가짜 모델. API 키 없이 루프·도구·평가를 테스트한다.
- ClaudeModel (claude.py): Anthropic Messages API
- (이후) Ollama 어댑터
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .tools import Tool


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class ModelTurn:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    raw: Any = None  # 제공자 원본 응답 블록 (다음 요청에 그대로 돌려보내야 하는 경우)
    served_model: str | None = None
    recovered_calls: int = 0  # 글로 쓴 도구 호출을 어댑터가 되살린 수  # 실제로 응답한 모델 (대체 모델로 넘어간 경우 확인용)


class Model(Protocol):
    name: str

    def complete(self, system: str, messages: list[dict], tools: Sequence[Tool]) -> ModelTurn: ...


Step = ModelTurn | Callable[[list[dict]], ModelTurn]


class ScriptedModel:
    """steps를 차례로 돌려준다. 함수를 넣으면 지금까지의 메시지를 보고 다음 턴을 만든다."""

    name = "scripted"

    def __init__(self, steps: Sequence[Step]):
        self._steps = list(steps)
        self._i = 0
        self.seen: list[list[dict]] = []  # 테스트에서 모델이 받은 메시지 확인용

    def complete(self, system: str, messages: list[dict], tools: Sequence[Tool]) -> ModelTurn:
        self.seen.append([dict(m) for m in messages])
        if self._i >= len(self._steps):
            raise RuntimeError("ScriptedModel: 준비된 턴을 모두 썼습니다.")
        step = self._steps[self._i]
        self._i += 1
        return step(messages) if callable(step) else step


def call(name: str, _id: str | None = None, **args) -> ToolCall:
    """테스트·스크립트용 축약: call("run_sql", sql="...")"""
    return ToolCall(id=_id or f"call_{name}", name=name, args=args)
