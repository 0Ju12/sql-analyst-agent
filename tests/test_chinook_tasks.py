"""평가 문항 파일 검증. Chinook DB가 있으면 정답 SQL을 실제로 실행해 본다.

DB 만들기: python scripts/fetch_chinook.py
"""

from pathlib import Path

import pytest

from sql_agent.db import connect_readonly, load_catalog, run_query
from sql_agent.evaluate import load_tasks, run_eval
from sql_agent.models import ModelTurn, ScriptedModel, call
from sql_agent.sql_guard import check_sql

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "eval" / "chinook_tasks.yaml"
DB = ROOT / "data" / "chinook.duckdb"
needs_db = pytest.mark.skipif(not DB.exists(), reason="data/chinook.duckdb 없음")


def test_task_file_shape():
    tasks = load_tasks(TASKS)
    assert len(tasks) == 30
    expects = [t.expect for t in tasks]
    assert expects.count("answered") == 25
    assert expects.count("need_clarification") == 3
    assert expects.count("refused") == 2


@needs_db
def test_gold_sql_passes_guard_and_returns_rows():
    con = connect_readonly(DB)
    catalog = load_catalog(con)
    for t in load_tasks(TASKS):
        if t.gold_sql is None:
            continue
        checked = check_sql(t.gold_sql, catalog)
        assert checked.ok, (t.id, checked.errors)
        rows = run_query(con, t.gold_sql).rows
        assert rows and rows[0][0] is not None, t.id


@needs_db
def test_oracle_model_scores_full(tmp_path):
    """정답대로 행동하는 가짜 모델은 30문항을 모두 맞혀야 한다 (채점기 자체 점검)."""

    def oracle(task):
        if task.expect == "answered":
            final = call("final_answer", status="answered", answer="-", sql=task.gold_sql)
        else:
            final = call("final_answer", status=task.expect, answer="-")
        return ScriptedModel([ModelTurn(tool_calls=[final])])

    rows = run_eval(load_tasks(TASKS), oracle, DB, tmp_path)
    wrong = [(r["id"], r["reason"]) for r in rows if not r["correct"]]
    assert not wrong
