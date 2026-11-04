"""OpenAI 호환(Ollama) 어댑터 오프라인 테스트 (가짜 HTTP)."""

import json

import pytest

from sql_agent.agent import run_agent
from sql_agent.openai_compat import OpenAICompatError, OpenAICompatModel, to_openai_messages

SQL = "select count(*) from customers"


def ok(tool_calls=None, content=None, finish="tool_calls", model="openai/gpt-4.1-mini"):
    return 200, {}, {
        "model": model,
        "choices": [{"finish_reason": finish,
                     "message": {"role": "assistant", "content": content,
                                 "tool_calls": tool_calls}}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 10},
    }  # fmt: skip


def tc(id_, name, args):
    raw = args if isinstance(args, str) else json.dumps(args)
    return {"id": id_, "type": "function", "function": {"name": name, "arguments": raw}}


class FakePost:
    def __init__(self, responses):
        self.responses = list(responses)
        self.bodies = []

    def __call__(self, url, token, body, timeout):
        assert url.endswith("/chat/completions")
        self.bodies.append(json.loads(json.dumps(body)))
        return self.responses.pop(0)


def model(post, **kw):
    return OpenAICompatModel(post=post, sleep=lambda s: None, **kw)


def test_loop_with_bad_json_then_recovery(ctx):
    post = FakePost([
        ok([tc("a", "run_sql", "{not json")]),
        ok([tc("b", "run_sql", {"sql": SQL})]),
        ok([tc("c", "final_answer", {"status": "answered", "answer": "4명", "sql": SQL})]),
    ])  # fmt: skip
    r = run_agent("고객 수?", model(post), ctx)
    assert r.status == "answered" and r.tool_errors == 1
    assert r.input_tokens == 150
    last = post.bodies[-1]["messages"]
    assert last[0]["role"] == "system"
    assert [m["role"] for m in last[1:]] == ["user", "assistant", "tool", "assistant", "tool"]
    assert last[2]["tool_calls"][0]["function"]["name"] == "run_sql"
    assert last[3]["content"].startswith("[오류]")
    assert post.bodies[0]["tools"][0]["type"] == "function"


def test_rate_limit_retry():
    post = FakePost([(429, {"retry-after": "3"}, {"error": {"message": "slow down"}}),
                     ok(content="hi", finish="stop")])  # fmt: skip
    slept = []
    m = OpenAICompatModel(post=post, sleep=slept.append)
    turn = m.complete("s", [{"role": "user", "content": "q"}], [])
    assert turn.text == "hi" and 3.0 in slept


def test_long_wait_stops():
    post = FakePost([(429, {"retry-after": "86400"}, {"error": {"message": "daily limit"}})])
    with pytest.raises(OpenAICompatError, match="호출 제한"):
        model(post).complete("s", [], [])


def test_non_retryable_error():
    post = FakePost([(403, {}, {"error": {"message": "no models permission"}})])
    with pytest.raises(OpenAICompatError, match="403"):
        model(post).complete("s", [], [])


def test_truncated_tool_call_dropped():
    post = FakePost([ok([tc("a", "run_sql", {"sql": "x"})], finish="length")])
    turn = model(post).complete("s", [], [])
    assert turn.tool_calls == []


def test_token_header_only_when_given(monkeypatch):
    from sql_agent import openai_compat as oc

    seen = {}

    class R:
        status = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"OK"  # GitHub Models가 실제로 돌려준 응답 형태

    def fake_urlopen(req, timeout):
        seen.update(req.headers)
        return R()

    monkeypatch.setattr(oc.urllib.request, "urlopen", fake_urlopen)
    status, _, data = oc._post("http://x/chat/completions", "", {}, 1)
    assert "Authorization" not in seen
    assert status == 502 and "JSON이 아닌 응답" in data["error"]["message"]


def test_messages_conversion_multiple_results():
    msgs = to_openai_messages("sys", [
        {"role": "user", "content": "q"},
        {"role": "assistant", "text": "", "tool_calls": [
            {"id": "1", "name": "list_tables", "args": {}},
            {"id": "2", "name": "describe_table", "args": {"table": "고객"}}]},
        {"role": "tool", "results": [
            {"tool_call_id": "1", "name": "list_tables", "content": "{}", "is_error": False},
            {"tool_call_id": "2", "name": "describe_table", "content": "{}", "is_error": True}]},
    ])  # fmt: skip
    assert msgs[2]["content"] == ""  # null이면 Ollama가 400을 낸다
    assert '"고객"' in msgs[2]["tool_calls"][1]["function"]["arguments"]
    assert [m["tool_call_id"] for m in msgs[3:]] == ["1", "2"]


TOOLS_SET = {"list_tables", "describe_table", "run_sql", "final_answer"}


@pytest.mark.parametrize(
    "text, name, key",
    [
        # 실제 평가 로그에서 나온 형태들
        ('leton\n{"name": "describe_table", "arguments": {"table": "track"}}\n</tool_call>',
         "describe_table", "table"),
        ('INAL\n{"status": "answered", "answer": "총 246 건", "sql": "SELECT 1"}',
         "final_answer", "sql"),
        ('final_answer({"status": "answered", "answer": "59명", "sql": "SELECT COUNT(*) FROM customer"})',
         "final_answer", "sql"),
        ('final_answer(\n    status="answered",\n    answer="1. id: 6\\n2. id: 26",\n    sql="SELECT 1"\n)',
         "final_answer", "answer"),
    ],
)  # fmt: skip
def test_recover_text_tool_call(text, name, key):
    from sql_agent.openai_compat import recover_text_tool_call

    call = recover_text_tool_call(text, TOOLS_SET)
    assert call is not None and call.name == name and key in call.args


def test_no_recovery_from_plain_text():
    from sql_agent.openai_compat import recover_text_tool_call

    assert recover_text_tool_call("고객은 59명입니다. (참고: 표 1)", TOOLS_SET) is None
    assert recover_text_tool_call('{"unknown": 1} drop_table(x="y")', TOOLS_SET) is None


def test_recovered_call_counted_and_sampling_fixed(ctx):
    sql = "select count(*) from customers"
    args = {"status": "answered", "answer": "4", "sql": sql}
    text = json.dumps({"name": "final_answer", "arguments": args})
    post = FakePost([ok(content=text, finish="stop")])
    r = run_agent("고객 수?", model(post), ctx)
    assert r.status == "answered" and r.recovered_calls == 1
    assert post.bodies[0]["temperature"] == 0.0 and post.bodies[0]["seed"] == 42
