"""에이전트 루프: 모델 호출 → 도구 실행 → 결과 전달을 final_answer가 나올 때까지 반복."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .models import Model
from .tools import FINAL_ANSWER, TOOLS, ToolContext, call_tool, dumps

SYSTEM_PROMPT = """\
당신은 DuckDB 데이터베이스를 조회해 질문에 답하는 데이터 분석가입니다.

작업 순서
1. list_tables, describe_table로 필요한 테이블과 컬럼을 먼저 확인합니다. 컬럼 이름을 추측하지 않습니다.
2. SQL을 작성해 run_sql로 실행합니다. 확신이 없으면 check_sql로 먼저 검사합니다.
3. 오류가 나면 오류 내용을 읽고 SQL을 고쳐 다시 실행합니다.
4. final_answer로 답을 냅니다.

규칙
- 답에 쓰는 숫자는 반드시 실행 결과에서 가져옵니다. 결과에 없는 내용을 지어내지 않습니다.
- status=answered일 때는 답의 근거가 된 최종 SQL을 sql에 넣습니다.
- 기간·기준·대상이 모호해 답이 크게 달라지는 질문은 need_clarification으로 되묻습니다.
- 데이터 추가·수정·삭제, 파일 읽기처럼 조회가 아닌 요청은 refused로 거절하고 이유를 씁니다.
- 답은 한국어로, 짧고 분명하게 씁니다.
"""


@dataclass
class AgentResult:
    question: str
    status: str  # answered | need_clarification | refused | max_steps | model_error
    answer: str = ""
    sql: str | None = None
    charts: list[str] = field(default_factory=list)
    steps: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    blocked_sql: int = 0  # 검사기가 막은 SQL 수
    input_tokens: int = 0
    output_tokens: int = 0
    elapsed_s: float = 0.0
    trace: list[dict] = field(default_factory=list)

    def summary(self) -> dict:
        d = asdict(self)
        d.pop("trace")
        return d


def _is_blocked(name: str, result: dict, is_error: bool) -> bool:
    if name == "check_sql":
        return not result.get("ok", True)
    return is_error and str(result.get("error", "")).startswith("SQL 검사를 통과하지 못했습니다")


def run_agent(
    question: str,
    model: Model,
    ctx: ToolContext,
    *,
    max_steps: int = 10,
    system: str = SYSTEM_PROMPT,
    trace_path: str | Path | None = None,
) -> AgentResult:
    res = AgentResult(question=question, status="max_steps")
    messages: list[dict] = [{"role": "user", "content": question}]
    start = time.perf_counter()

    def log(event: dict) -> None:
        event = {"t": round(time.perf_counter() - start, 3), **event}
        res.trace.append(event)

    log({"type": "question", "question": question, "model": model.name})

    for step in range(1, max_steps + 1):
        res.steps = step
        try:
            turn = model.complete(system, messages, TOOLS)
        except Exception as e:
            res.status, res.answer = "model_error", f"{type(e).__name__}: {e}"
            log({"type": "model_error", "step": step, "error": res.answer})
            break
        res.input_tokens += turn.input_tokens
        res.output_tokens += turn.output_tokens
        messages.append(
            {
                "role": "assistant",
                "text": turn.text,
                "tool_calls": [
                    {"id": c.id, "name": c.name, "args": c.args} for c in turn.tool_calls
                ],
                "raw": turn.raw,
            }
        )
        log(
            {
                "type": "model",
                "step": step,
                "text": turn.text,
                "tool_calls": [{"name": c.name, "args": c.args} for c in turn.tool_calls],
                "input_tokens": turn.input_tokens,
                "output_tokens": turn.output_tokens,
                "served_model": turn.served_model,
            }
        )

        if not turn.tool_calls:
            # 도구 없이 글로만 답한 경우: final_answer로 내도록 한 번 더 요청한다.
            messages.append({"role": "user", "content": "답은 final_answer 도구로 내 주세요."})
            log({"type": "nudge", "step": step})
            continue

        results, final = [], None
        for c in turn.tool_calls:
            out, is_error = call_tool(ctx, c.name, c.args)
            res.tool_calls += 1
            res.tool_errors += is_error
            res.blocked_sql += _is_blocked(c.name, out, is_error)
            if c.name == FINAL_ANSWER and not is_error:
                final = c.args
            results.append(
                {"tool_call_id": c.id, "name": c.name, "content": dumps(out), "is_error": is_error}
            )
            log(
                {
                    "type": "tool",
                    "step": step,
                    "name": c.name,
                    "args": c.args,
                    "is_error": is_error,
                    "result": out,
                }
            )
        messages.append({"role": "tool", "results": results})

        if final is not None:
            res.status = final.get("status", "answered")
            res.answer = final.get("answer", "")
            res.sql = final.get("sql")
            break

    res.charts = list(ctx.charts)
    res.elapsed_s = round(time.perf_counter() - start, 3)
    log({"type": "end", "status": res.status, "steps": res.steps})

    if trace_path:
        p = Path(trace_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            for ev in res.trace:
                f.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
    return res
