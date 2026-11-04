"""명령줄: sql-agent load | tables | check | run"""

from __future__ import annotations

import argparse
import json
import sys

from .db import connect_readonly, load_catalog
from .load import load_manifest
from .sql_guard import check_sql
from .tools import ToolContext, call_tool


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
    a = p.parse_args(argv)

    if a.cmd == "load":
        for name, n in load_manifest(a.manifest, a.db).items():
            print(f"{name}: {n}행")
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
    out, is_error = call_tool(ToolContext(con=con, catalog=catalog), "run_sql", {"sql": a.sql})
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 1 if is_error else 0


if __name__ == "__main__":
    sys.exit(main())
