"""국토교통부 아파트 매매 실거래가 API → CSV.

여기(개발 환경)에서는 data.go.kr에 접속이 안 되므로 GitHub Actions(fetch-apt-trades.yml)나 PC에서 실행한다.

- 인증키: 환경변수 DATA_GO_KR_KEY (공공데이터포털의 "Decoding" 키). 키는 출력·저장하지 않는다.
- 엔드포인트: 환경변수 APT_TRADE_URL로 바꿀 수 있다. 출처마다 이름이 달라(…/RTMSDataSvcAptTrade, …Dev)
  첫 실행에서 실제로 되는 주소를 확인해야 한다.
- 응답 필드 이름은 가정하지 않는다. <item>의 하위 항목을 모두 문자열 그대로 저장하고(원본 단계),
  정리·형 변환은 실제 필드를 확인한 뒤 SQL로 한다.

사용: DATA_GO_KR_KEY=... python scripts/fetch_apt_trades.py --start 202401 --end 202412
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

DEFAULT_URL = "https://apis.data.go.kr/1613000/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade"
OK_CODES = {"00", "000"}
ROOT = Path(__file__).resolve().parents[1]

# 서울 25개 구 (법정동코드 앞 5자리)
SEOUL = {
    "11110": "종로구", "11140": "중구", "11170": "용산구", "11200": "성동구", "11215": "광진구",
    "11230": "동대문구", "11260": "중랑구", "11290": "성북구", "11305": "강북구", "11320": "도봉구",
    "11350": "노원구", "11380": "은평구", "11410": "서대문구", "11440": "마포구", "11470": "양천구",
    "11500": "강서구", "11530": "구로구", "11545": "금천구", "11560": "영등포구", "11590": "동작구",
    "11620": "관악구", "11650": "서초구", "11680": "강남구", "11710": "송파구", "11740": "강동구",
}  # fmt: skip


class ApiError(Exception):
    pass


def months(start: str, end: str) -> list[str]:
    y, m = int(start[:4]), int(start[4:])
    out = []
    while f"{y}{m:02d}" <= end:
        out.append(f"{y}{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def parse_page(xml_text: str) -> tuple[list[dict], int]:
    """XML 한 쪽을 (행 목록, 전체 건수)로 바꾼다. 오류 코드면 ApiError."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise ApiError(f"XML이 아닌 응답: {xml_text[:200]!r}") from e
    code = (root.findtext(".//resultCode") or root.findtext(".//returnReasonCode") or "").strip()
    msg = (root.findtext(".//resultMsg") or root.findtext(".//returnAuthMsg") or "").strip()
    if code not in OK_CODES:
        raise ApiError(f"API 오류 코드 {code or '?'}: {msg or xml_text[:200]!r}")
    items = [{c.tag: (c.text or "").strip() for c in item} for item in root.iter("item")]
    total = int((root.findtext(".//totalCount") or "0").strip() or 0)
    return items, total


def fetch(url: str, params: dict, *, retries: int = 3) -> str:
    full = url + "?" + urllib.parse.urlencode(params)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(full, timeout=30) as r:
                return r.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == retries - 1:
                # 오류 메시지에 URL(=키)이 들어가지 않게 원인만 남긴다
                reason = getattr(e, "code", None) or getattr(e, "reason", type(e).__name__)
                raise ApiError(f"요청 실패: {reason}") from None
            time.sleep(2 ** (attempt + 1))
    raise AssertionError("unreachable")


def fetch_month(url: str, key: str, lawd: str, ym: str, rows_per_page: int = 1000) -> list[dict]:
    out, page = [], 1
    while True:
        params = {"serviceKey": key, "LAWD_CD": lawd, "DEAL_YMD": ym,
                  "pageNo": page, "numOfRows": rows_per_page}  # fmt: skip
        items, total = parse_page(fetch(url, params))
        out.extend(items)
        if not items or len(out) >= total:
            return out
        page += 1


def write_csv(path: Path, rows: list[dict], first: list[str]) -> list[str]:
    cols = first + sorted({k for r in rows for k in r} - set(first))
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    return cols


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--start", required=True, help="YYYYMM")
    p.add_argument("--end", required=True, help="YYYYMM")
    p.add_argument("--regions", default=",".join(SEOUL), help="법정동코드 5자리, 쉼표로 구분")
    p.add_argument("--out", default=str(ROOT / "data" / "raw" / "apt"))
    p.add_argument("--sleep", type=float, default=0.2)
    a = p.parse_args(argv)

    key = os.environ.get("DATA_GO_KR_KEY", "").strip()
    if not key:
        print(
            "DATA_GO_KR_KEY가 없습니다. 저장소 Secrets나 환경변수로 넣어 주세요.", file=sys.stderr
        )
        return 2
    url = os.environ.get("APT_TRADE_URL", "").strip() or DEFAULT_URL
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    rows, report = [], {"url": url, "requests": [], "errors": []}
    for lawd in a.regions.split(","):
        for ym in months(a.start, a.end):
            try:
                items = fetch_month(url, key, lawd, ym)
            except ApiError as e:
                report["errors"].append({"lawd_cd": lawd, "deal_ymd": ym, "error": str(e)})
                print(f"{lawd} {ym}: 실패 — {e}", file=sys.stderr)
                if not rows and len(report["errors"]) >= 3:
                    break  # 처음부터 연속 실패면 키·주소 문제일 가능성이 커서 멈춘다
                continue
            for it in items:
                it["lawd_cd"], it["deal_ymd"] = lawd, ym
            rows.extend(items)
            report["requests"].append({"lawd_cd": lawd, "deal_ymd": ym, "rows": len(items)})
            print(f"{lawd} {ym}: {len(items)}건")
            time.sleep(a.sleep)
        else:
            continue
        break

    cols = write_csv(out / "apt_trades.csv", rows, ["lawd_cd", "deal_ymd"]) if rows else []
    write_csv(out / "districts.csv",
              [{"lawd_cd": k, "name": v} for k, v in SEOUL.items()], ["lawd_cd", "name"])  # fmt: skip
    report.update(total_rows=len(rows), columns=cols)
    (out / "fetch_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"총 {len(rows)}건, 컬럼: {', '.join(cols)}")
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
