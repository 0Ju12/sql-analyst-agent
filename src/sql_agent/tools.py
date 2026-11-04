"""에이전트가 쓸 수 있는 도구.

각 도구는 (이름, 설명, 입력 JSON 스키마, 처리 함수)로 정의한다.
모델 종류와 상관없이 같은 정의를 쓰고, 모델 어댑터가 각 API 형식으로 바꾼다.
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

from .db import Catalog, QueryTimeout, run_query
from .sql_guard import check_sql

FINAL_ANSWER = "final_answer"
ANSWER_STATUSES = ("answered", "need_clarification", "refused")


@dataclass
class ToolContext:
    con: duckdb.DuckDBPyConnection
    catalog: Catalog
    chart_dir: Path = Path("charts")
    max_rows: int = 200
    preview_rows: int = 30  # 모델에게 보여 줄 행 수 (토큰 절약)
    timeout_s: float = 10.0
    charts: list[str] = field(default_factory=list)


class ToolError(Exception):
    """모델에게 그대로 보여 줄 오류 (모델이 읽고 고칠 수 있는 내용)."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[ToolContext, dict], dict]


def _jsonable(v: Any) -> Any:
    if isinstance(v, (dt.date, dt.datetime, dt.time)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (bytes, bytearray)):
        return f"<{len(v)} bytes>"
    return v


def _rows(rows: list[tuple]) -> list[list]:
    return [[_jsonable(v) for v in r] for r in rows]


def _require_table(ctx: ToolContext, name: str) -> str:
    key = (name or "").lower()
    if key not in ctx.catalog:
        raise ToolError(f"없는 테이블: {name}. 있는 테이블: {', '.join(sorted(ctx.catalog))}")
    return key


# ---- 처리 함수 ---------------------------------------------------------------


def _list_tables(ctx: ToolContext, args: dict) -> dict:
    return {"tables": [{"name": t, "columns": len(c)} for t, c in sorted(ctx.catalog.items())]}


def _describe_table(ctx: ToolContext, args: dict) -> dict:
    table = _require_table(ctx, args.get("table", ""))
    n = ctx.con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
    sample = ctx.con.execute(f'SELECT * FROM "{table}" LIMIT 5')
    return {
        "table": table,
        "row_count": n,
        "columns": [{"name": c, "type": t} for c, t in ctx.catalog[table].items()],
        "sample_rows": _rows(sample.fetchall()),
    }


def _check_sql(ctx: ToolContext, args: dict) -> dict:
    return check_sql(args.get("sql", ""), ctx.catalog, max_rows=ctx.max_rows).to_dict()


def _execute_checked(ctx: ToolContext, sql: str):
    checked = check_sql(sql, ctx.catalog, max_rows=ctx.max_rows)
    if not checked.ok:
        raise ToolError("SQL 검사를 통과하지 못했습니다: " + " / ".join(checked.errors))
    try:
        result = run_query(ctx.con, checked.sql, timeout_s=ctx.timeout_s, max_rows=ctx.max_rows)
    except QueryTimeout as e:
        raise ToolError(str(e)) from e
    except duckdb.Error as e:
        raise ToolError(f"실행 오류: {e}") from e
    return checked, result


def _run_sql(ctx: ToolContext, args: dict) -> dict:
    checked, result = _execute_checked(ctx, args.get("sql", ""))
    return {
        "executed_sql": checked.sql,
        "warnings": checked.warnings,
        "columns": result.columns,
        "row_count": len(result.rows),
        "truncated": result.truncated,
        "rows": _rows(result.rows[: ctx.preview_rows]),
        "rows_shown": min(len(result.rows), ctx.preview_rows),
    }


def _make_chart(ctx: ToolContext, args: dict) -> dict:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    kind = args.get("kind", "bar")
    if kind not in ("bar", "line", "scatter"):
        raise ToolError("kind는 bar, line, scatter 중 하나여야 합니다.")
    checked, result = _execute_checked(ctx, args.get("sql", ""))
    cols = [c.lower() for c in result.columns]
    x, y = (args.get("x") or "").lower(), (args.get("y") or "").lower()
    for c in (x, y):
        if c not in cols:
            raise ToolError(f"결과에 없는 컬럼: {c}. 결과 컬럼: {', '.join(result.columns)}")
    xs = [r[cols.index(x)] for r in result.rows]
    ys = [r[cols.index(y)] for r in result.rows]

    fig, ax = plt.subplots(figsize=(8, 4.5))
    {"bar": ax.bar, "line": ax.plot, "scatter": ax.scatter}[kind](
        [str(v) for v in xs] if kind == "bar" else xs, ys
    )
    ax.set_xlabel(x)
    ax.set_ylabel(y)
    if args.get("title"):
        ax.set_title(args["title"])
    fig.autofmt_xdate()
    ctx.chart_dir.mkdir(parents=True, exist_ok=True)
    path = ctx.chart_dir / f"chart_{len(ctx.charts) + 1}.png"
    fig.savefig(path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    ctx.charts.append(str(path))
    return {"path": str(path), "points": len(xs), "executed_sql": checked.sql}


def _final_answer(ctx: ToolContext, args: dict) -> dict:
    status = args.get("status", "answered")
    if status not in ANSWER_STATUSES:
        raise ToolError(f"status는 {', '.join(ANSWER_STATUSES)} 중 하나여야 합니다.")
    if status == "answered" and not args.get("sql"):
        raise ToolError("answered일 때는 답의 근거가 된 SQL을 sql에 넣어야 합니다.")
    return {"accepted": True}


# ---- 정의 --------------------------------------------------------------------

_SQL = {"type": "string", "description": "DuckDB SQL 조회문 한 문장"}

TOOLS: list[Tool] = [
    Tool(
        "list_tables",
        "데이터베이스의 테이블 목록과 각 테이블의 컬럼 수를 본다.",
        {"type": "object", "properties": {}},
        _list_tables,
    ),
    Tool(
        "describe_table",
        "테이블의 컬럼 이름·자료형, 전체 행 수, 예시 5행을 본다.",
        {"type": "object", "properties": {"table": {"type": "string"}}, "required": ["table"]},
        _describe_table,
    ),
    Tool(
        "check_sql",
        "SQL을 실행하지 않고 검사한다: 문법, 조회문 여부, 없는 테이블·컬럼, 파일 접근. "
        "통과하면 실제로 실행될 SQL(LIMIT 보정 포함)을 돌려준다.",
        {"type": "object", "properties": {"sql": _SQL}, "required": ["sql"]},
        _check_sql,
    ),
    Tool(
        "run_sql",
        "SQL을 검사한 뒤 실행하고 결과 일부를 돌려준다. 읽기 전용이며 조회문만 실행된다.",
        {"type": "object", "properties": {"sql": _SQL}, "required": ["sql"]},
        _run_sql,
    ),
    Tool(
        "make_chart",
        "SQL 결과로 차트(PNG)를 만든다. x·y는 결과 컬럼 이름.",
        {
            "type": "object",
            "properties": {
                "sql": _SQL,
                "kind": {"type": "string", "enum": ["bar", "line", "scatter"]},
                "x": {"type": "string"},
                "y": {"type": "string"},
                "title": {"type": "string"},
            },
            "required": ["sql", "kind", "x", "y"],
        },
        _make_chart,
    ),
    Tool(
        FINAL_ANSWER,
        "최종 답을 낸다. 데이터로 답했으면 status=answered와 근거 SQL을, "
        "질문이 모호하면 need_clarification과 되묻는 질문을, "
        "데이터 변경·삭제처럼 할 수 없는 요청이면 refused와 이유를 쓴다.",
        {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": list(ANSWER_STATUSES)},
                "answer": {"type": "string", "description": "사용자에게 보여 줄 답"},
                "sql": {"type": "string", "description": "답의 근거가 된 SQL (answered일 때 필수)"},
            },
            "required": ["status", "answer"],
        },
        _final_answer,
    ),
]

TOOLS_BY_NAME = {t.name: t for t in TOOLS}


def call_tool(ctx: ToolContext, name: str, args: dict) -> tuple[dict, bool]:
    """도구를 실행하고 (결과, 오류 여부)를 돌려준다. 예외는 모델이 읽을 오류 결과로 바꾼다."""
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        return {"error": f"없는 도구: {name}. 쓸 수 있는 도구: {', '.join(TOOLS_BY_NAME)}"}, True
    if not isinstance(args, dict):
        return {"error": "도구 입력은 JSON 객체여야 합니다."}, True
    try:
        return tool.handler(ctx, args), False
    except ToolError as e:
        return {"error": str(e)}, True
    except Exception as e:  # 예상 못 한 오류도 루프를 멈추지 않고 모델에게 알린다
        return {"error": f"도구 내부 오류: {type(e).__name__}: {e}"}, True


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
