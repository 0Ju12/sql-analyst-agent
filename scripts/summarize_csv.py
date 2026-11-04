"""CSV 요약을 출력한다: 행 수, 컬럼별 비어 있지 않은 값 수·고유값 수·예시, 앞 N행.

Actions 결과물을 내려받을 수 없는 환경에서 수집 결과를 로그로 확인하기 위한 것.
사용: python scripts/summarize_csv.py data/raw/apt/apt_trades.csv [--rows 5]
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("csv")
    p.add_argument("--rows", type=int, default=5)
    a = p.parse_args()

    with open(a.csv, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print("행 없음")
        return 1
    cols = list(rows[0])
    print(f"행 수: {len(rows)}, 컬럼 수: {len(cols)}")
    print("\n컬럼 | 값 있음 | 고유값 | 자주 나온 값")
    for c in cols:
        vals = [r[c] for r in rows if r[c] not in ("", None)]
        top = ", ".join(f"{v!r}×{n}" for v, n in Counter(vals).most_common(3))
        print(f"{c} | {len(vals)} | {len(set(vals))} | {top}")
    print(f"\n앞 {a.rows}행")
    for r in rows[: a.rows]:
        print({k: v for k, v in r.items() if v})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
