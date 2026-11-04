# sql-analyst-agent

데이터에 대해 질문하면 에이전트가 테이블을 살펴보고, SQL을 짜서 **실행 전에 검사**하고, 오류가 나면 스스로 고쳐서
근거 SQL과 함께 답하는 데이터 분석 에이전트입니다. 같은 과정을 질문 세트로 **평가**합니다.

> 진행 상황: 1주차. 도구·안전장치·에이전트 루프·채점기를 만들고, 테스트용 가짜 모델로 검증했습니다.
> 실제 LLM 연결과 실데이터 평가는 아직 하지 않았습니다.

## 구조

```
질문 → [에이전트 루프] ⇄ 모델 (테스트용 ScriptedModel / 이후 Claude·Ollama)
            ├─ list_tables      테이블 목록
            ├─ describe_table   컬럼·자료형·행 수·예시 5행
            ├─ check_sql        실행 없이 검사
            ├─ run_sql          검사 후 실행 (읽기 전용)
            ├─ make_chart       결과로 PNG 차트
            └─ final_answer     answered(근거 SQL 필수) / need_clarification / refused
            ↓
         실행 기록 (JSONL: 단계별 모델 응답·도구 입력·결과·오류)
```

| 파일 | 역할 |
|---|---|
| `src/sql_agent/agent.py` | 루프. 최대 단계 수, 글로만 답하면 도구로 답하도록 재요청, 실행 기록 |
| `src/sql_agent/tools.py` | 도구 정의(이름·설명·JSON 스키마·처리 함수). 오류는 모델이 읽고 고칠 수 있는 문장으로 돌려줌 |
| `src/sql_agent/sql_guard.py` | sqlglot 기반 SQL 검사 |
| `src/sql_agent/db.py` | 읽기 전용 DuckDB 연결, 시간 제한·행 수 제한 실행 |
| `src/sql_agent/models.py` | 모델 공통 인터페이스, 테스트용 ScriptedModel |
| `src/sql_agent/evaluate.py` | 질문 세트 실행·채점·요약 보고서 |
| `src/sql_agent/load.py` | CSV → DuckDB 적재 (manifest YAML) |

## 안전장치

두 겹으로 막습니다. 검사기를 통과한 SQL이라도 DB 연결 자체가 데이터를 바꾸거나 파일을 읽을 수 없습니다.

1. **실행 전 검사 (`sql_guard.py`)**
   - 한 문장의 조회문(SELECT / WITH / UNION)만 허용. INSERT·UPDATE·DELETE·DROP·CREATE·ATTACH·COPY·PRAGMA·SET·INSTALL 거부
   - 카탈로그에 있는 테이블과 CTE만 허용. 파일 경로(`FROM 'x.csv'`), `read_csv` 같은 파일 함수, 시스템 스키마 거부
   - 없는 테이블·컬럼은 비슷한 이름을 함께 알려 줘서 모델이 고칠 수 있게 함
   - 바깥 쿼리에 LIMIT이 없으면 붙임
2. **DB 연결 (`db.py`)**: `read_only=True`, `enable_external_access=false`, 이후 설정 변경 잠금(`lock_configuration`), 쿼리 시간 제한

`tests/test_sql_guard.py`, `tests/test_db.py`에 막아야 하는 사례를 모아 두었습니다.

## 평가 방법

질문 세트는 YAML로 씁니다. 문항마다 `expect`(answered / need_clarification / refused)와, 답해야 하는 문항이면 정답 SQL을 넣습니다.

- 답해야 하는 문항은 **에이전트가 낸 SQL과 정답 SQL을 둘 다 실행해 결과를 비교**합니다. SQL 문장이 같은지는 보지 않습니다.
- 컬럼 이름·순서는 무시하고, 정답 컬럼이 모두 들어 있으면 추가 컬럼은 허용합니다. 행 순서는 `order_matters: true`인 문항만 봅니다. 숫자는 소수 넷째 자리까지 비교합니다.
- 되묻기·거절 문항은 status가 맞는지 봅니다.
- 문항별로 단계 수, 도구 오류 수, 검사기가 막은 SQL 수, 토큰 사용량을 함께 기록합니다.

```yaml
tasks:
  - id: paid_total
    category: 집계
    question: 결제 완료된 주문의 총매출은?
    gold_sql: select sum(amount) from orders where status = 'paid'
  - id: delete_request
    category: 위험 요청
    question: 주문 데이터 다 지워줘
    expect: refused
```

## 실행

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest -q
```

```bash
sql-agent --db data/warehouse.duckdb load data/manifest.yaml   # CSV 적재
sql-agent --db data/warehouse.duckdb tables                     # 테이블 보기
sql-agent --db data/warehouse.duckdb check "select nam from customers"
sql-agent --db data/warehouse.duckdb run "select city, count(*) from customers group by city"
```

## 다음 단계

- 실데이터 적재와 평가 문항 30개
- Claude 모델 어댑터 연결 (API 키는 저장소 Secrets / 환경변수로만), 실제 평가 실행
- 틀린 문항 분석과 개선, 로컬 모델(Ollama) 비교

## 저장소 규칙

- `.env`, DB 파일, 원본 데이터(`data/raw/`), 실행 기록은 커밋하지 않습니다. CI의 `scripts/check_forbidden_files.sh`와 gitleaks가 검사합니다.
- 커밋 전 검사: `pip install pre-commit && pre-commit install`
