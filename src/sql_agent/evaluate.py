"""평가: 질문 세트로 에이전트를 돌리고 결과를 채점한다.

채점 기준
- expect가 need_clarification / refused인 문항: 에이전트의 status가 같으면 정답
- expect가 answered인 문항: 에이전트가 낸 SQL과 정답 SQL을 둘 다 실행해 결과를 비교
  · SQL 문장이 같은지는 보지 않는다
  · 컬럼 이름·순서는 보지 않는다. 에이전트 결과에 정답의 컬럼이 모두 들어 있으면 된다(추가 컬럼 허용)
  · 행 순서는 order_matters: true인 문항만 본다
  · 숫자는 소수 넷째 자리까지 비교한다. 숫자 모양의 문자열('2021')도 숫자로 본다
- 답변 숫자 검사(answered 문항): 답변 글에 나온 숫자가 최종 SQL의 실행 결과(또는 질문)에
  있는지 확인한다. SQL은 맞았는데 답변에 결과에 없는 숫자를 쓴 경우를 따로 센다.
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from itertools import permutations
from pathlib import Path

import yaml

from .agent import AgentResult, run_agent
from .db import connect_readonly, load_catalog, run_query
from .models import Model
from .sql_guard import check_sql
from .tools import ToolContext

EXPECTS = ("answered", "need_clarification", "refused")
EVAL_MAX_ROWS = 10_000


@dataclass
class Task:
    id: str
    question: str
    category: str
    expect: str = "answered"
    gold_sql: str | None = None
    order_matters: bool = False


def load_tasks(path: str | Path) -> list[Task]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    tasks = [Task(**t) for t in raw["tasks"]]
    ids = [t.id for t in tasks]
    dup = [i for i, n in Counter(ids).items() if n > 1]
    if dup:
        raise ValueError(f"중복된 문항 id: {dup}")
    for t in tasks:
        if t.expect not in EXPECTS:
            raise ValueError(f"{t.id}: expect는 {EXPECTS} 중 하나여야 합니다.")
        if t.expect == "answered" and not t.gold_sql:
            raise ValueError(f"{t.id}: answered 문항에는 gold_sql이 필요합니다.")
    return tasks


def _norm(v):
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float, decimal.Decimal)):
        return round(float(v), 4) + 0.0  # -0.0 → 0.0
    if isinstance(v, (dt.date, dt.datetime, dt.time)):
        return v.isoformat()
    s = str(v)
    if _NUMERIC.fullmatch(s.strip()):
        return round(float(s), 4) + 0.0
    return s


_NUMERIC = re.compile(r"[-+]?\d+(\.\d+)?")
_NUM_IN_TEXT = re.compile(r"(?<![A-Za-z0-9_.])[-+]?\d+(?:,\d{3})*(?:\.\d+)?")


def _numbers(text: str) -> list[tuple[str, float]]:
    out = []
    for m in _NUM_IN_TEXT.finditer(text or ""):
        raw = m.group(0)
        try:
            out.append((raw, float(raw.replace(",", ""))))
        except ValueError:
            continue
    return out


def unsupported_numbers(answer: str, rows: list[tuple], question: str = "") -> list[str]:
    """답변에 나온 숫자 중 실행 결과·질문 어디에도 없는 것.

    허용: 결과 값(소수 0~2자리 반올림 포함), 결과 속 문자열·날짜 안의 숫자, 질문 속 숫자,
    목록 번호(1~행 수)와 행 수.
    """
    allowed: set[float] = {float(i) for i in range(len(rows) + 1)}
    allowed |= {x for _, x in _numbers(question)}
    for r in rows:
        for v in r:
            if isinstance(v, bool) or v is None:
                continue
            if isinstance(v, (int, float, decimal.Decimal)):
                f = float(v)
                allowed |= {f, round(f), round(f, 1), round(f, 2)}
            else:
                allowed |= {x for _, x in _numbers(str(v))}
    bad = []
    for raw, x in _numbers(answer):
        if not any(abs(x - a) <= 0.005 + 1e-9 * abs(a) for a in allowed):
            bad.append(raw)
    return bad


def compare_results(gold: list[tuple], pred: list[tuple], *, order_matters: bool) -> bool:
    """pred의 컬럼 중 일부를 골라 순서를 맞췄을 때 gold와 같아지면 True."""
    if len(gold) != len(pred):
        return False
    if not gold:
        return True
    g = [tuple(_norm(v) for v in r) for r in gold]
    p = [tuple(_norm(v) for v in r) for r in pred]
    ng, np_ = len(g[0]), len(p[0])
    if np_ < ng:
        return False

    # 컬럼별 값 묶음이 같은 후보만 남겨 조합 수를 줄인다
    def col(rows, j):
        return Counter(r[j] for r in rows)

    cand = [[k for k in range(np_) if col(p, k) == col(g, j)] for j in range(ng)]
    if any(not c for c in cand):
        return False
    target = g if order_matters else Counter(g)
    for mapping in permutations(range(np_), ng):
        if any(mapping[j] not in cand[j] for j in range(ng)):
            continue
        proj = [tuple(r[k] for k in mapping) for r in p]
        if (proj if order_matters else Counter(proj)) == target:
            return True
    return False


def score(task: Task, result: AgentResult, con, catalog) -> dict:
    out = {
        "id": task.id,
        "category": task.category,
        "expect": task.expect,
        "status": result.status,
        "correct": False,
        "reason": "",
        "unsupported_numbers": None,  # answered 문항에서만 채움
    }
    if result.status == "model_error":
        out["reason"] = f"모델 호출 오류: {result.answer[:300]}"
        return out
    if task.expect != "answered":
        out["correct"] = result.status == task.expect
        out["reason"] = "" if out["correct"] else f"{task.expect}이어야 하는데 {result.status}"
        return out
    if result.status != "answered" or not result.sql:
        out["reason"] = f"답하지 못함 ({result.status})"
        return out

    gold = run_query(con, task.gold_sql, max_rows=EVAL_MAX_ROWS).rows
    checked = check_sql(result.sql, catalog, max_rows=EVAL_MAX_ROWS)
    if not checked.ok:
        out["reason"] = "최종 SQL이 검사를 통과하지 못함: " + " / ".join(checked.errors)
        return out
    try:
        pred = run_query(con, checked.sql, max_rows=EVAL_MAX_ROWS).rows
    except Exception as e:
        out["reason"] = f"최종 SQL 실행 오류: {e}"
        return out
    out["unsupported_numbers"] = unsupported_numbers(result.answer, pred, task.question)
    out["correct"] = compare_results(gold, pred, order_matters=task.order_matters)
    out["reason"] = "" if out["correct"] else f"결과 불일치 (정답 {len(gold)}행, 답 {len(pred)}행)"
    return out


def run_eval(
    tasks: list[Task],
    make_model: Callable[[Task], Model],
    db_path: str | Path,
    out_dir: str | Path,
    *,
    max_steps: int = 10,
) -> list[dict]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    con = connect_readonly(db_path)
    catalog = load_catalog(con)
    rows = []
    for task in tasks:
        ctx = ToolContext(con=con, catalog=catalog, chart_dir=out_dir / "charts" / task.id)
        result = run_agent(
            task.question,
            make_model(task),
            ctx,
            max_steps=max_steps,
            trace_path=out_dir / "traces" / f"{task.id}.jsonl",
        )
        row = score(task, result, con, catalog)
        row.update(
            {
                k: getattr(result, k)
                for k in (
                    "steps",
                    "tool_calls",
                    "tool_errors",
                    "blocked_sql",
                    "input_tokens",
                    "output_tokens",
                    "elapsed_s",
                    "sql",
                    "answer",
                )
            }
        )
        rows.append(row)
    con.close()

    with (out_dir / "results.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    (out_dir / "summary.md").write_text(summarize(rows), encoding="utf-8")
    return rows


def summarize(rows: list[dict]) -> str:
    def line(name, rs):
        n = len(rs)
        ok = sum(r["correct"] for r in rs)
        checked = [r for r in rs if r.get("unsupported_numbers") is not None]
        bad = sum(bool(r["unsupported_numbers"]) for r in checked)
        avg = lambda k: sum(r[k] for r in rs) / n  # noqa: E731
        tokens = sum(r["input_tokens"] + r["output_tokens"] for r in rs)
        return (
            f"| {name} | {ok}/{n} | {bad}/{len(checked)} | {avg('steps'):.1f} "
            f"| {sum(r['tool_errors'] for r in rs)} | {sum(r['blocked_sql'] for r in rs)} | {tokens} |"
        )

    cats = sorted({r["category"] for r in rows})
    out = [
        "| 유형 | 정답 | 답변에 근거 없는 숫자 | 평균 단계 | 도구 오류 | 막힌 SQL | 토큰 |",
        "|---|---|---|---|---|---|---|",
    ]
    out += [line(c, [r for r in rows if r["category"] == c]) for c in cats]
    out.append(line("전체", rows))
    wrong = [r for r in rows if not r["correct"]]
    if wrong:
        out += ["", "틀린 문항", ""]
        out += [f"- `{r['id']}` ({r['category']}): {r['reason']}" for r in wrong]
    unfaithful = [r for r in rows if r.get("unsupported_numbers")]
    if unfaithful:
        out += ["", "답변에 실행 결과에 없는 숫자가 있는 문항", ""]
        out += [
            f"- `{r['id']}`{' (SQL은 정답)' if r['correct'] else ''}: "
            + ", ".join(r["unsupported_numbers"][:8])
            for r in unfaithful
        ]
    return "\n".join(out) + "\n"
