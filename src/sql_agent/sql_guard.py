"""실행 전 SQL 검사 (sqlglot).

검사 항목
1. 문법: 파싱이 되는가, 한 문장인가
2. 종류: SELECT / WITH / UNION 같은 조회문만 허용
3. 위험 명령: INSERT·DROP·ATTACH·COPY·PRAGMA·SET 등은 어디에 있어도 거부
4. 출처: 카탈로그에 있는 테이블(또는 CTE)만 허용. 파일 경로·테이블 함수(read_csv 등) 거부
5. 컬럼: 없는 컬럼을 쓰면 거부하고 비슷한 이름을 알려 준다
6. LIMIT: 바깥 쿼리에 LIMIT이 없으면 붙인다

거부 사유는 모델이 읽고 스스로 고칠 수 있게 구체적으로 쓴다.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.qualify import qualify

from .db import Catalog

DIALECT = "duckdb"

_FORBIDDEN_NODE_NAMES = (
    "Insert",
    "Update",
    "Delete",
    "Merge",
    "Drop",
    "Create",
    "Alter",
    "TruncateTable",
    "Attach",
    "Detach",
    "Copy",
    "Export",
    "Install",
    "Pragma",
    "Set",
    "Use",
    "Command",
    "Transaction",
    "Commit",
    "Rollback",
)
_FORBIDDEN_NODES = tuple(getattr(exp, n) for n in _FORBIDDEN_NODE_NAMES if hasattr(exp, n))
_FILE_FUNCTION = re.compile(r"^(read_|parquet_|sniff_|glob$|query_table$|query$)", re.I)


@dataclass
class CheckResult:
    ok: bool
    sql: str | None = None  # 실행할 SQL (LIMIT 보정 포함)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "sql": self.sql, "errors": self.errors, "warnings": self.warnings}


def _suggest(name: str, candidates: list[str]) -> str:
    close = difflib.get_close_matches(name.lower(), candidates, n=3, cutoff=0.5)
    return f" 비슷한 이름: {', '.join(close)}." if close else ""


def check_sql(sql: str, catalog: Catalog, *, max_rows: int = 200) -> CheckResult:
    sql = (sql or "").strip().rstrip(";").strip()
    if not sql:
        return CheckResult(False, errors=["SQL이 비어 있습니다."])

    # 1. 문법
    try:
        statements = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
    except ParseError as e:
        return CheckResult(
            False, errors=[f"문법 오류: {e.errors[0]['description'] if e.errors else e}"]
        )
    if len(statements) != 1:
        return CheckResult(False, errors=["한 번에 한 문장만 실행할 수 있습니다."])
    tree = statements[0]

    # 2·3. 종류와 위험 명령
    bad = [n for n in tree.walk() if isinstance(n, _FORBIDDEN_NODES)]
    if bad or not isinstance(tree, exp.Query):
        kind = type(bad[0]).__name__ if bad else type(tree).__name__
        return CheckResult(
            False,
            errors=[
                f"조회문(SELECT/WITH)만 실행할 수 있습니다. 허용되지 않는 명령: {kind.upper()}"
            ],
        )

    errors: list[str] = []
    tables = sorted(catalog)

    # 4. 출처
    for fn in tree.find_all(exp.Func):
        name = fn.sql_name() if not isinstance(fn, exp.Anonymous) else fn.name
        if isinstance(fn, exp.ReadCSV) or _FILE_FUNCTION.match(name or ""):
            errors.append(f"파일·외부 자료를 읽는 함수는 쓸 수 없습니다: {name}")

    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    for t in tree.find_all(exp.Table):
        if not isinstance(t.this, exp.Identifier):
            continue  # 테이블 함수는 위에서 처리
        name = t.name.lower()
        if t.db and t.db.lower() != "main":
            errors.append(f"main 스키마의 테이블만 쓸 수 있습니다: {t.db}.{t.name}")
        elif name in cte_names:
            continue
        elif name not in catalog:
            if "/" in name or "." in name:
                errors.append(f"파일 경로는 테이블로 쓸 수 없습니다: {t.name}")
            else:
                errors.append(
                    f"없는 테이블: {t.name}.{_suggest(name, tables)} 있는 테이블: {', '.join(tables)}"
                )
    if errors:
        return CheckResult(False, errors=list(dict.fromkeys(errors)))

    # 5. 컬럼
    warnings: list[str] = []
    try:
        qualify(tree.copy(), schema=catalog, dialect=DIALECT, validate_qualify_columns=True)
    except OptimizeError as e:
        msg = str(e)
        m = re.search(r"Column '([^']+)' could not be resolved", msg)
        if m:
            col = m.group(1)
            all_cols = sorted({c for cols in catalog.values() for c in cols})
            errors.append(f"없는 컬럼: {col}.{_suggest(col, all_cols)}")
        else:
            errors.append(f"컬럼·별칭을 확인할 수 없습니다: {msg}")
        return CheckResult(False, errors=errors)
    except Exception as e:  # 검사기 자체의 한계 — 막지 않고 경고만 (DB가 읽기 전용이므로 안전)
        warnings.append(f"컬럼 검사를 끝까지 하지 못했습니다: {type(e).__name__}")

    # 6. LIMIT
    if tree.args.get("limit") is None:
        tree = tree.limit(max_rows)
        warnings.append(f"LIMIT {max_rows}을 붙였습니다.")

    return CheckResult(True, sql=tree.sql(dialect=DIALECT), warnings=warnings)
