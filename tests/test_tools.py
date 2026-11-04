import json

from sql_agent.tools import call_tool


def test_list_and_describe(ctx):
    out, err = call_tool(ctx, "list_tables", {})
    assert not err and {t["name"] for t in out["tables"]} == {"customers", "orders"}
    out, err = call_tool(ctx, "describe_table", {"table": "Orders"})
    assert not err and out["row_count"] == 6 and len(out["sample_rows"]) == 5
    json.dumps(out)  # 날짜·Decimal도 직렬화돼야 한다


def test_describe_unknown_table(ctx):
    out, err = call_tool(ctx, "describe_table", {"table": "nope"})
    assert err and "있는 테이블" in out["error"]


def test_run_sql(ctx):
    out, err = call_tool(
        ctx,
        "run_sql",
        {"sql": "select city, count(*) n from customers group by city order by n desc, city"},
    )
    assert not err
    assert out["rows"][0] == ["서울", 2]


def test_run_sql_blocked(ctx):
    out, err = call_tool(ctx, "run_sql", {"sql": "drop table orders"})
    assert err and out["error"].startswith("SQL 검사를 통과하지 못했습니다")


def test_run_sql_runtime_error_is_readable(ctx):
    out, err = call_tool(ctx, "run_sql", {"sql": "select cast(name as integer) from customers"})
    assert err and "실행 오류" in out["error"]


def test_make_chart(ctx):
    out, err = call_tool(
        ctx,
        "make_chart",
        {
            "sql": "select status, sum(amount) total from orders group by status",
            "kind": "bar",
            "x": "status",
            "y": "total",
        },
    )
    assert not err, out
    assert ctx.charts and ctx.chart_dir.joinpath("chart_1.png").exists()


def test_final_answer_requires_sql(ctx):
    out, err = call_tool(ctx, "final_answer", {"status": "answered", "answer": "42"})
    assert err
    out, err = call_tool(ctx, "final_answer", {"status": "refused", "answer": "삭제는 못 합니다"})
    assert not err


def test_unknown_tool_and_bad_args(ctx):
    assert call_tool(ctx, "rm_rf", {})[1]
    assert call_tool(ctx, "run_sql", "select 1")[1]
