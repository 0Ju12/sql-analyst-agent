"""CSV → DuckDB 적재. 적재는 쓰기 연결로 따로 하고, 에이전트는 결과 파일을 읽기 전용으로만 연다.

manifest 예시 (data/manifest.yaml)
  tables:
    - name: rentals
      file: raw/rentals.csv      # manifest 위치 기준 상대 경로
      options: {header: true}    # read_csv 옵션 (선택)
"""

from __future__ import annotations

import re
from pathlib import Path

import duckdb
import yaml

_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")


def _opt(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def load_manifest(manifest: str | Path, db_path: str | Path) -> dict[str, int]:
    manifest = Path(manifest)
    spec = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    counts: dict[str, int] = {}
    con = duckdb.connect(str(db_path))
    try:
        for t in spec["tables"]:
            name = t["name"]
            if not _NAME.match(name):
                raise ValueError(f"테이블 이름은 영문 소문자·숫자·_만: {name}")
            file = (manifest.parent / t["file"]).resolve()
            if not file.exists():
                raise FileNotFoundError(file)
            opts = "".join(f", {k} = {_opt(v)}" for k, v in (t.get("options") or {}).items())
            con.execute(
                f'CREATE OR REPLACE TABLE "{name}" AS '
                f"SELECT * FROM read_csv({_opt(str(file))}{opts})"
            )
            counts[name] = con.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
    finally:
        con.close()
    return counts
