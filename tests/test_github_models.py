"""GitHub Models 어댑터 오프라인 테스트 (가짜 HTTP)."""

import json

import pytest

from sql_agent.agent import run_agent
from sql_agent.github_models import GitHubModel, GitHubModelsError, to_openai_messages

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
        assert token == "tok" and url.endswith("/chat/completions")
        self.bodies.append(json.loads(json.dumps(body)))
        return self.responses.pop(0)


def model(post, **kw):
    return GitHubModel(token="tok", post=post, sleep=lambda s: None, **kw)


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
    m = GitHubModel(token="tok", post=post, sleep=slept.append)
    turn = m.complete("s", [{"role": "user", "content": "q"}], [])
    assert turn.text == "hi" and 3.0 in slept


def test_long_wait_stops():
    post = FakePost([(429, {"retry-after": "86400"}, {"error": {"message": "daily limit"}})])
    with pytest.raises(GitHubModelsError, match="호출 제한"):
        model(post).complete("s", [], [])


def test_non_retryable_error():
    post = FakePost([(403, {}, {"error": {"message": "no models permission"}})])
    with pytest.raises(GitHubModelsError, match="403"):
        model(post).complete("s", [], [])


def test_truncated_tool_call_dropped():
    post = FakePost([ok([tc("a", "run_sql", {"sql": "x"})], finish="length")])
    turn = model(post).complete("s", [], [])
    assert turn.tool_calls == []


def test_missing_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(GitHubModelsError):
        GitHubModel()


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
    assert msgs[2]["content"] is None
    assert '"고객"' in msgs[2]["tool_calls"][1]["function"]["arguments"]
    assert [m["tool_call_id"] for m in msgs[3:]] == ["1", "2"]
