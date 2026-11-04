"""Claude 어댑터 오프라인 테스트: 가짜 클라이언트로 요청 형식과 응답 처리를 확인한다."""

from types import SimpleNamespace as NS

import pytest

from sql_agent.agent import run_agent
from sql_agent.claude import ClaudeModel, ModelRefusal, to_api_messages


def usage(i=100, o=20, cr=0):
    return NS(input_tokens=i, output_tokens=o, cache_read_input_tokens=cr,
              cache_creation_input_tokens=0)  # fmt: skip


def resp(content, stop="tool_use", model="claude-opus-5-5", **kw):
    return NS(content=content, stop_reason=stop, usage=usage(**kw), model=model, stop_details=None)


def tool_use(id_, name, inp):
    return NS(type="tool_use", id=id_, name=name, input=inp)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kw):
        self.requests.append(kw)
        return self.responses.pop(0)


SQL = "select count(*) from customers"


def test_full_loop_request_shapes(ctx):
    thinking = NS(type="thinking", thinking="", signature="sig")
    client = FakeClient([
        resp([thinking, tool_use("t1", "run_sql", {"sql": SQL})], cr=80),
        resp([tool_use("t2", "final_answer", {"status": "answered", "answer": "4명", "sql": SQL})],
             model="claude-opus-4-8"),
    ])  # fmt: skip
    model = ClaudeModel(client=client)
    r = run_agent("고객 수?", model, ctx)
    assert r.status == "answered" and r.sql == SQL
    assert r.input_tokens == 100 + 80 + 100

    first, second = client.requests
    assert first["model"] == "claude-opus-5-5"
    assert first["output_config"] == {"effort": "medium"}
    assert first["fallbacks"] == "default" and first["betas"] == ["server-side-fallback-2026-07-01"]
    assert "tool_choice" not in first  # 강제 도구 선택은 쓰지 않는다
    assert {t["name"] for t in first["tools"]} >= {"run_sql", "final_answer"}

    msgs = second["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[1]["content"][0] is thinking  # 생각 블록을 바꾸지 않고 그대로 돌려보냄
    result = msgs[2]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "t1"
    assert result["is_error"] is False and '"row_count": 1' in result["content"]
    # 대체 모델로 응답한 턴이 기록에 남는다
    served = [e["served_model"] for e in r.trace if e["type"] == "model"]
    assert served == ["claude-opus-5-5", "claude-opus-4-8"]


def test_refusal_becomes_model_error(ctx):
    refusal = NS(content=[], stop_reason="refusal", usage=usage(), model="x",
                 stop_details=NS(category="cyber"))  # fmt: skip
    model = ClaudeModel(client=FakeClient([refusal]))
    with pytest.raises(ModelRefusal):
        model.complete("s", [{"role": "user", "content": "q"}], [])
    r = run_agent("q", ClaudeModel(client=FakeClient([refusal])), ctx)
    assert r.status == "model_error" and "cyber" in r.answer


def test_truncated_tool_call_not_executed():
    model = ClaudeModel(client=FakeClient([
        resp([tool_use("t1", "run_sql", {"sql": "sel"})], stop="max_tokens")]))  # fmt: skip
    turn = model.complete("s", [{"role": "user", "content": "q"}], [])
    assert turn.tool_calls == [] and "잘렸습니다" in turn.text


def test_no_fallbacks_option():
    client = FakeClient([resp([NS(type="text", text="hi")], stop="end_turn")])
    ClaudeModel(client=client, fallbacks=False, effort="low").complete("s", [], [])
    assert "fallbacks" not in client.requests[0] and "betas" not in client.requests[0]


def test_messages_without_raw():
    msgs = to_api_messages([
        {"role": "user", "content": "q"},
        {"role": "assistant", "text": "보겠습니다", "raw": None,
         "tool_calls": [{"id": "a", "name": "list_tables", "args": {}}]},
        {"role": "tool", "results": [{"tool_call_id": "a", "name": "list_tables",
                                      "content": "{}", "is_error": True}]},
    ])  # fmt: skip
    assert msgs[1]["content"][1] == {"type": "tool_use", "id": "a", "name": "list_tables",
                                     "input": {}}  # fmt: skip
    assert msgs[2]["content"][0]["is_error"] is True
