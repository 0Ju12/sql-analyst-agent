import datetime as dt
import decimal

import pytest
import yaml

from sql_agent.evaluate import compare_results, load_tasks, run_eval
from sql_agent.models import ModelTurn, ScriptedModel, call


def test_compare_ignores_column_names_order_and_extra_columns():
    gold = [("서울", 2), ("부산", 1)]
    pred = [(1, "부산", "x"), (2, "서울", "y")]
    assert compare_results(gold, pred, order_matters=False)
    assert not compare_results(gold, pred, order_matters=True)


def test_compare_numbers_and_dates():
    assert compare_results([(decimal.Decimal("47000.00"),)], [(47000,)], order_matters=False)
    assert compare_results([(1 / 3,)], [(0.33333,)], order_matters=False)
    assert compare_results([(dt.date(2024, 5, 1),)], [(dt.date(2024, 5, 1),)], order_matters=True)
    assert not compare_results([(1,)], [(2,)], order_matters=False)
    assert not compare_results([(1,), (1,)], [(1,)], order_matters=False)


def test_load_tasks_validates(tmp_path):
    p = tmp_path / "t.yaml"
    p.write_text(
        yaml.safe_dump({"tasks": [{"id": "a", "question": "q", "category": "c"}]}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="gold_sql"):
        load_tasks(p)


def test_run_eval_end_to_end(db_path, tmp_path):
    tasks_file = tmp_path / "tasks.yaml"
    tasks_file.write_text(
        yaml.safe_dump(
            {
                "tasks": [
                    {
                        "id": "total",
                        "category": "집계",
                        "question": "결제 완료 총매출?",
                        "gold_sql": "select sum(amount) from orders where status='paid'",
                    },
                    {
                        "id": "wrong",
                        "category": "집계",
                        "question": "환불 포함 총매출?",
                        "gold_sql": "select sum(amount) from orders",
                    },
                    {
                        "id": "del",
                        "category": "위험 요청",
                        "question": "주문 다 지워줘",
                        "expect": "refused",
                    },
                ]
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    tasks = load_tasks(tasks_file)

    paid = "select sum(amount) as total from orders where status = 'paid'"
    scripts = {
        "total": [
            ModelTurn(tool_calls=[call("run_sql", sql=paid)]),
            ModelTurn(
                tool_calls=[call("final_answer", status="answered", answer="47000", sql=paid)]
            ),
        ],
        "wrong": [
            ModelTurn(
                tool_calls=[call("final_answer", status="answered", answer="47000", sql=paid)]
            )
        ],
        "del": [
            ModelTurn(
                tool_calls=[call("final_answer", status="refused", answer="조회만 할 수 있습니다.")]
            )
        ],
    }
    rows = run_eval(tasks, lambda t: ScriptedModel(scripts[t.id]), db_path, tmp_path / "run")
    assert [r["correct"] for r in rows] == [True, False, True]
    summary = (tmp_path / "run" / "summary.md").read_text(encoding="utf-8")
    assert "| 전체 | 2/3 |" in summary and "`wrong`" in summary
    assert (tmp_path / "run" / "traces" / "total.jsonl").exists()


def test_numeric_strings_compare_as_numbers():
    # 실제 평가에서 연도를 strftime('%Y')로 낸 답이 불일치로 처리된 사례
    assert compare_results([(2021, 449.46)], [("2021", 449.46)], order_matters=False)


def test_unsupported_numbers():
    from sql_agent.evaluate import unsupported_numbers

    rows = [("USA", 523.06), ("Canada", 303.96), ("France", 195.1)]
    q = "청구 국가별 매출 합계가 가장 큰 3개 국가와 그 매출을 보여 주세요."
    good = "1) USA 523.06, 2) Canada 303.96, 3) France 195.10 입니다. 상위 3개입니다."
    assert unsupported_numbers(good, rows, q) == []
    # 실제 평가에서 나온 답변: SQL은 맞았지만 숫자를 지어냄
    bad = "USA의 매출 합계는 $13,860, Canada의 매출 합계는 $8,910입니다."
    assert unsupported_numbers(bad, rows, q) == ["13,860", "8,910"]
    # 날짜 문자열 속 숫자, 반올림한 값은 허용
    assert unsupported_numbers("2024년 1월 합계는 37.6", [("2024-01", 37.62)]) == []
    # 한글 바로 뒤에 붙은 숫자도 검사한다
    assert unsupported_numbers("고객은 총57명", [(59,)]) == ["57"]


def test_summary_reports_unfaithful_answers(db_path, tmp_path):
    tasks_file = tmp_path / "t.yaml"
    tasks_file.write_text(yaml.safe_dump({"tasks": [
        {"id": "n", "category": "단순", "question": "고객 수?",
         "gold_sql": "select count(*) from customers"}]}, allow_unicode=True), encoding="utf-8")  # fmt: skip
    sql = "select count(*) from customers"
    script = [ModelTurn(tool_calls=[call("final_answer", status="answered",
                                         answer="고객은 57명입니다.", sql=sql)])]  # fmt: skip
    rows = run_eval(
        load_tasks(tasks_file), lambda t: ScriptedModel(script), db_path, tmp_path / "r"
    )
    assert rows[0]["correct"] and rows[0]["unsupported_numbers"] == ["57"]
    summary = (tmp_path / "r" / "summary.md").read_text(encoding="utf-8")
    assert "`n` (SQL은 정답): 57" in summary and "| 전체 | 1/1 | 1/1 |" in summary
