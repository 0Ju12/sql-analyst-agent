"""DuckDB 연결과 쿼리 실행.

에이전트는 항상 읽기 전용 + 외부 파일 접근 차단 연결만 쓴다.
sql_guard가 놓친 SQL이 있더라도 이 연결로는 데이터를 바꾸거나 파일을 읽을 수 없다.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path

import duckdb

Catalog = dict[str, dict[str, str]]  # {테이블: {컬럼: 자료형}}


def connect_readonly(path: str | Path) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(
        str(path),
        read_only=True,
        config={"enable_external_access": False},
    )
    # 이후 SET 문으로 위 설정을 되돌리지 못하게 잠근다.
    con.execute("SET lock_configuration = true")
    return con


def load_catalog(con: duckdb.DuckDBPyConnection) -> Catalog:
    rows = con.execute(
        """
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'main'
        ORDER BY table_name, ordinal_position
        """
    ).fetchall()
    catalog: Catalog = {}
    for table, column, dtype in rows:
        catalog.setdefault(table.lower(), {})[column.lower()] = dtype
    return catalog


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    truncated: bool
    elapsed_s: float


class QueryTimeout(Exception):
    pass


def run_query(
    con: duckdb.DuckDBPyConnection,
    sql: str,
    *,
    timeout_s: float = 10.0,
    max_rows: int = 200,
) -> QueryResult:
    """SQL을 실행하고 최대 max_rows행까지 가져온다. 시간을 넘기면 QueryTimeout."""
    timed_out = threading.Event()

    def _interrupt() -> None:
        timed_out.set()
        con.interrupt()

    timer = threading.Timer(timeout_s, _interrupt)
    start = time.perf_counter()
    timer.start()
    try:
        cur = con.execute(sql)
        columns = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(max_rows + 1)
    except duckdb.InterruptException as e:
        raise QueryTimeout(f"{timeout_s:g}초 안에 끝나지 않아 중단했습니다.") from e
    except Exception:
        if timed_out.is_set():
            raise QueryTimeout(f"{timeout_s:g}초 안에 끝나지 않아 중단했습니다.") from None
        raise
    finally:
        timer.cancel()
    elapsed = time.perf_counter() - start
    truncated = len(rows) > max_rows
    return QueryResult(columns, rows[:max_rows], truncated, elapsed)
