import json

from sql_agent.agent import run_agent
from sql_agent.models import ModelTurn, ScriptedModel, call

TOTAL_SQL = "select sum(amount) from orders where status = 'paid'"


def test_happy_path_with_self_correction(ctx, tmp_path):
    model = ScriptedModel(
        [
            ModelTurn(tool_calls=[call("list_tables")]),
            ModelTurn(tool_calls=[call("describe_table", table="orders")]),
            ModelTurn(tool_calls=[call("run_sql", sql="select sum(amout) from orders")]),  # 오타
            lambda msgs: ModelTurn(tool_calls=[call("run_sql", sql=TOTAL_SQL)]),
            ModelTurn(
                tool_calls=[
                    call(
                        "final_answer",
                        status="answered",
                        answer="결제 완료 매출은 47,000원입니다.",
                        sql=TOTAL_SQL,
                    )
                ]
            ),
        ]
    )
    trace = tmp_path / "t.jsonl"
    r = run_agent("결제 완료된 주문의 총매출은?", model, ctx, trace_path=trace)
    assert r.status == "answered" and r.sql == TOTAL_SQL
    assert r.steps == 5 and r.tool_calls == 5
    assert r.tool_errors == 1 and r.blocked_sql == 1
    # 모델은 오타 오류 메시지(비슷한 컬럼 안내)를 받았어야 한다
    last_tool_msg = model.seen[3][-1]
    assert last_tool_msg["role"] == "tool" and "amount" in last_tool_msg["results"][0]["content"]
    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    assert events[0]["type"] == "question" and events[-1]["type"] == "end"


def test_max_steps(ctx):
    model = ScriptedModel([ModelTurn(tool_calls=[call("list_tables")])] * 5)
    r = run_agent("?", model, ctx, max_steps=3)
    assert r.status == "max_steps" and r.steps == 3


def test_text_only_reply_gets_nudged(ctx):
    model = ScriptedModel(
        [
            ModelTurn(text="아마 47000원일 거예요"),
            ModelTurn(
                tool_calls=[
                    call(
                        "final_answer", status="need_clarification", answer="어느 기간 기준인가요?"
                    )
                ]
            ),
        ]
    )
    r = run_agent("매출 알려줘", model, ctx)
    assert r.status == "need_clarification" and r.steps == 2
    assert model.seen[1][-1] == {"role": "user", "content": "답은 final_answer 도구로 내 주세요."}


def test_invalid_final_answer_does_not_end_loop(ctx):
    model = ScriptedModel(
        [
            ModelTurn(
                tool_calls=[call("final_answer", status="answered", answer="47000")]
            ),  # sql 없음
            ModelTurn(
                tool_calls=[call("final_answer", status="answered", answer="47000", sql=TOTAL_SQL)]
            ),
        ]
    )
    r = run_agent("총매출?", model, ctx)
    assert r.status == "answered" and r.steps == 2 and r.tool_errors == 1


def test_model_error_is_captured(ctx):
    r = run_agent("?", ScriptedModel([]), ctx)
    assert r.status == "model_error"
