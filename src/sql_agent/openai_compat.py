"""OpenAI 호환 chat/completions 어댑터. 기본 용도는 로컬 Ollama(무료, 키 불필요).

- Ollama: `ollama serve` 후 http://localhost:11434/v1/chat/completions 로 호출한다.
  GitHub Actions에서도 같은 방식으로 돌린다 (.github/workflows/eval.yml).
- 다른 OpenAI 호환 서버도 endpoint와 token만 바꿔 쓸 수 있다.
- 추가 패키지 없이 표준 라이브러리(urllib)로 호출한다.
- 429·5xx는 retry-after(없으면 지수 대기)만큼 기다렸다가 다시 시도한다.

참고: GitHub Models(models.github.ai)도 시도했으나 2026-11-04 Actions에서 확인한 결과
추론·모델 목록 요청 모두 HTTP 200 text/plain "OK"만 돌려줘 쓸 수 없었다.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from typing import Any

from .models import ModelTurn, ToolCall
from .tools import Tool

OLLAMA_ENDPOINT = "http://localhost:11434/v1/chat/completions"
DEFAULT_MODEL = "qwen2.5:7b"


class OpenAICompatError(Exception):
    pass


def to_openai_tools(tools: Sequence[Tool]) -> list[dict]:
    return [
        {"type": "function",
         "function": {"name": t.name, "description": t.description, "parameters": t.input_schema}}
        for t in tools
    ]  # fmt: skip


def to_openai_messages(system: str, messages: list[dict]) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": m.get("text") or None}
            if m.get("tool_calls"):
                msg["tool_calls"] = [
                    {"id": c["id"], "type": "function",
                     "function": {"name": c["name"],
                                  "arguments": json.dumps(c["args"], ensure_ascii=False)}}
                    for c in m["tool_calls"]
                ]  # fmt: skip
            out.append(msg)
        elif m["role"] == "tool":
            for r in m["results"]:
                content = r["content"]
                if r["is_error"]:
                    content = "[오류] " + content
                out.append({"role": "tool", "tool_call_id": r["tool_call_id"], "content": content})
    return out


def _post(url: str, token: str, body: dict, timeout: float) -> tuple[int, dict, dict]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, dict(r.headers), json.loads(raw)
            except json.JSONDecodeError:
                return (
                    502,
                    dict(r.headers),
                    {"error": {"message": f"JSON이 아닌 응답: {raw[:200]!r}"}},
                )
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"error": {"message": raw[:500]}}
        return e.code, dict(e.headers or {}), payload


class OpenAICompatModel:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        endpoint: str = OLLAMA_ENDPOINT,
        token: str = "",
        max_tokens: int = 4000,
        max_retries: int = 4,
        min_interval_s: float = 0.0,
        timeout_s: float = 900.0,  # CPU에서 돌리는 로컬 모델은 느리다
        post: Callable[[str, str, dict, float], tuple[int, dict, dict]] = _post,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.endpoint = endpoint
        self.token = token
        self.model = model
        self.name = f"openai-compat:{model}"
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self._post = post
        self._sleep = sleep
        self._last = 0.0

    def complete(self, system: str, messages: list[dict], tools: Sequence[Tool]) -> ModelTurn:
        body = {
            "model": self.model,
            "messages": to_openai_messages(system, messages),
            "tools": to_openai_tools(tools),
            "max_tokens": self.max_tokens,
        }
        for attempt in range(self.max_retries + 1):
            wait = self.min_interval_s - (time.monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
            self._last = time.monotonic()
            status, headers, data = self._post(self.endpoint, self.token, body, self.timeout_s)
            if status == 200:
                return self._parse(data)
            msg = (data.get("error") or {}).get("message", "") if isinstance(data, dict) else ""
            retryable = status == 429 or status >= 500
            if not retryable or attempt == self.max_retries:
                raise OpenAICompatError(f"HTTP {status}: {msg[:300]}")
            retry_after = headers.get("retry-after") or headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else 2.0 * 2**attempt
            except ValueError:
                delay = 2.0 * 2**attempt
            if delay > 300:
                # 일 단위 한도 소진처럼 오래 기다려야 하면 멈춘다
                raise OpenAICompatError(f"호출 제한: {delay:.0f}초 뒤에 다시 가능 ({msg[:200]})")
            self._sleep(delay)
        raise AssertionError("unreachable")

    def _parse(self, data: dict) -> ModelTurn:
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        text = msg.get("content") or ""
        calls = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = None  # call_tool이 "JSON 객체여야 합니다" 오류로 돌려준다
            calls.append(ToolCall(id=tc.get("id") or f"call_{i}", name=fn.get("name", ""),
                                  args=args))  # fmt: skip
        if choice.get("finish_reason") == "length" and calls:
            calls = []
            text += "\n(응답이 max_tokens에서 잘렸습니다)"
        usage = data.get("usage") or {}
        return ModelTurn(
            text=text,
            tool_calls=calls,
            input_tokens=usage.get("prompt_tokens") or 0,
            output_tokens=usage.get("completion_tokens") or 0,
            served_model=data.get("model"),
        )
