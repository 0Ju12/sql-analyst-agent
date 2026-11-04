"""명령줄: sql-agent load | tables | check | run | ask | eval"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from .db import connect_readonly, load_catalog
from .load import load_manifest
from .sql_guard import check_sql
from .tools import ToolContext, call_tool


def _claude(a):
    from .claude import ClaudeModel

    return ClaudeModel(a.model, effort=a.effort, fallbacks=not a.no_fallbacks)


def _model_args(s: argparse.ArgumentParser) -> None:
    s.add_argument("--model", default="claude-opus-5-5")
    s.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    s.add_argument("--no-fallbacks", action="store_true", help="거절 시 대체 모델 사용 안 함")
    s.add_argument("--max-steps", type=int, default=10)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sql-agent")
    p.add_argument("--db", default="data/warehouse.duckdb")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("load", help="manifest의 CSV를 DuckDB로 적재")
    s.add_argument("manifest")
    sub.add_parser("tables", help="테이블과 컬럼 보기")
    s = sub.add_parser("check", help="SQL 검사만")
    s.add_argument("sql")
    s = sub.add_parser("run", help="SQL 검사 후 실행")
    s.add_argument("sql")
    s = sub.add_parser("ask", help="에이전트에게 질문")
    s.add_argument("question")
    s.add_argument("--trace", default=None, help="실행 기록 JSONL 경로")
    _model_args(s)
    s = sub.add_parser("eval", help="평가 문항으로 에이전트 실행·채점")
    s.add_argument("tasks")
    s.add_argument("--out", default=None, help="결과 폴더 (기본: runs/<시각>)")
    s.add_argument("--only", default="", help="문항 id (쉼표 구분)")
    s.add_argument("--limit", type=int, default=0, help="앞에서부터 N문항만")
    _model_args(s)
    a = p.parse_args(argv)

    if a.cmd == "load":
        for name, n in load_manifest(a.manifest, a.db).items():
            print(f"{name}: {n}행")
        return 0

    if a.cmd == "eval":
        from .evaluate import load_tasks, run_eval, summarize

        tasks = load_tasks(a.tasks)
        if a.only:
            ids = set(a.only.split(","))
            tasks = [t for t in tasks if t.id in ids]
        if a.limit:
            tasks = tasks[: a.limit]
        out = Path(a.out or f"runs/{dt.datetime.now():%Y%m%d-%H%M%S}")
        model = _claude(a)
        rows = run_eval(tasks, lambda t: model, a.db, out, max_steps=a.max_steps)
        (out / "config.json").write_text(
            json.dumps({"model": a.model, "effort": a.effort, "fallbacks": not a.no_fallbacks,
                        "max_steps": a.max_steps, "tasks": a.tasks, "n": len(tasks)},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )  # fmt: skip
        print(summarize(rows))
        print(f"→ {out}")
        return 0

    con = connect_readonly(a.db)
    catalog = load_catalog(con)
    if a.cmd == "tables":
        for t, cols in catalog.items():
            print(f"{t}: " + ", ".join(f"{c} {ty}" for c, ty in cols.items()))
        return 0
    if a.cmd == "check":
        r = check_sql(a.sql, catalog)
        print(json.dumps(r.to_dict(), ensure_ascii=False, indent=2))
        return 0 if r.ok else 1
    if a.cmd == "ask":
        from .agent import run_agent

        ctx = ToolContext(con=con, catalog=catalog)
        r = run_agent(a.question, _claude(a), ctx, max_steps=a.max_steps, trace_path=a.trace)
        print(f"[{r.status}] {r.answer}")
        if r.sql:
            print(f"\nSQL: {r.sql}")
        for c in r.charts:
            print(f"차트: {c}")
        print(f"\n단계 {r.steps}, 도구 호출 {r.tool_calls}, 도구 오류 {r.tool_errors}, "
              f"토큰 {r.input_tokens + r.output_tokens}")  # fmt: skip
        return 0 if r.status in ("answered", "need_clarification", "refused") else 1
    out, is_error = call_tool(ToolContext(con=con, catalog=catalog), "run_sql", {"sql": a.sql})
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 1 if is_error else 0


if __name__ == "__main__":
    sys.exit(main())
