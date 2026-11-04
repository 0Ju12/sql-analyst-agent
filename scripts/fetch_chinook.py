"""공개 예제 DB(Chinook, 가상의 음원 판매점)를 받아 DuckDB로 적재한다.

- 출처: https://github.com/lerocha/chinook-database (MIT 형식 라이선스, Copyright (c) 2008-2024 Luis Rocha)
- 원본 SQLite 파일은 data/raw/에만 두고 커밋하지 않는다. 받은 파일은 SHA-256으로 확인한다.
- 테이블·컬럼 이름은 snake_case로 바꾼다 (InvoiceLine.UnitPrice → invoice_line.unit_price).

사용: python scripts/fetch_chinook.py [--db data/chinook.duckdb]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import re
import sqlite3
import sys
import urllib.request
from pathlib import Path

URL = (
    "https://raw.githubusercontent.com/lerocha/chinook-database/master/"
    "ChinookDatabase/DataSources/Chinook_Sqlite.sqlite"
)
SHA256 = "7651ba378ac2fcd0dfc3c66fb101f7a7eed3ba39a612ec642b96e20702061f15"
ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "chinook"


def snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=str(ROOT / "data" / "chinook.duckdb"))
    a = p.parse_args()

    RAW.mkdir(parents=True, exist_ok=True)
    src = RAW / "chinook.sqlite"
    if not src.exists():
        print(f"내려받는 중: {URL}")
        urllib.request.urlretrieve(URL, src)
    digest = hashlib.sha256(src.read_bytes()).hexdigest()
    if digest != SHA256:
        print(f"SHA-256이 다릅니다 (원본이 바뀌었을 수 있음): {digest}", file=sys.stderr)
        return 1

    con = sqlite3.connect(src)
    tables = [
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    manifest = ["tables:"]
    for t in tables:
        cur = con.execute(f'SELECT * FROM "{t}"')
        cols = [snake(d[0]) for d in cur.description]
        out = RAW / f"{snake(t)}.csv"
        with out.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows(cur)
        manifest += [
            f"  - name: {snake(t)}",
            f"    file: {out.name}",
            "    options: {header: true}",
        ]
    con.close()
    (RAW / "manifest.yaml").write_text("\n".join(manifest) + "\n", encoding="utf-8")

    sys.path.insert(0, str(ROOT / "src"))
    from sql_agent.load import load_manifest

    Path(a.db).unlink(missing_ok=True)
    for name, n in load_manifest(RAW / "manifest.yaml", a.db).items():
        print(f"{name}: {n}행")
    print(f"→ {a.db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
