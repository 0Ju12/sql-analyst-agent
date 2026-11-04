"""실거래가 수집 스크립트의 오프라인 테스트.

아래 XML은 공공데이터포털 응답의 일반적인 구조(header/body/items/item)를 본떠 직접 만든 것이다.
실제 필드 이름은 첫 실행에서 확인한다 — 스크립트는 필드 이름을 가정하지 않는다.
"""

import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "fetch_apt", Path(__file__).resolve().parents[1] / "scripts" / "fetch_apt_trades.py"
)
fa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fa)


def page(items, total, code="000", msg="OK"):
    body = "".join(
        "<item>" + "".join(f"<{k}>{v}</{k}>" for k, v in it.items()) + "</item>" for it in items
    )
    return (
        f"<response><header><resultCode>{code}</resultCode><resultMsg>{msg}</resultMsg></header>"
        f"<body><items>{body}</items><totalCount>{total}</totalCount></body></response>"
    )


def test_months_crosses_year():
    assert fa.months("202411", "202502") == ["202411", "202412", "202501", "202502"]


def test_parse_page_keeps_all_fields():
    items, total = fa.parse_page(page([{"a": " 1 ", "b": "x"}, {"a": "2", "c": "y"}], 2))
    assert total == 2 and items == [{"a": "1", "b": "x"}, {"a": "2", "c": "y"}]


@pytest.mark.parametrize("xml", [page([], 0, code="30", msg="SERVICE KEY IS NOT REGISTERED"),
                                 "Unexpected errors"])  # fmt: skip
def test_parse_page_errors(xml):
    with pytest.raises(fa.ApiError):
        fa.parse_page(xml)


def test_pagination_and_output(tmp_path, monkeypatch):
    calls = []

    def fake_fetch(url, params, retries=3):
        calls.append(params)
        n = params["pageNo"]
        return page([{"v": f"{params['LAWD_CD']}-{n}"}], total=2) if n <= 2 else page([], 2)

    monkeypatch.setattr(fa, "fetch", fake_fetch)
    monkeypatch.setattr(fa.time, "sleep", lambda s: None)
    monkeypatch.setenv("DATA_GO_KR_KEY", "test-key")
    rc = fa.main(["--start", "202401", "--end", "202402", "--regions", "11110",
                  "--out", str(tmp_path)])  # fmt: skip
    assert rc == 0
    assert len(calls) == 4  # 2개월 × 2쪽
    csv_text = (tmp_path / "apt_trades.csv").read_text(encoding="utf-8").splitlines()
    assert csv_text[0] == "lawd_cd,deal_ymd,v" and len(csv_text) == 5
    report = json.loads((tmp_path / "fetch_report.json").read_text(encoding="utf-8"))
    assert report["total_rows"] == 4 and "test-key" not in json.dumps(report)


def test_missing_key(monkeypatch, tmp_path):
    monkeypatch.delenv("DATA_GO_KR_KEY", raising=False)
    assert fa.main(["--start", "202401", "--end", "202401", "--out", str(tmp_path)]) == 2


def test_stops_after_repeated_failures(tmp_path, monkeypatch):
    def failing(url, params, retries=3):
        raise fa.ApiError("요청 실패: 403")

    monkeypatch.setattr(fa, "fetch", failing)
    monkeypatch.setenv("DATA_GO_KR_KEY", "k")
    rc = fa.main(["--start", "202401", "--end", "202412", "--out", str(tmp_path)])
    report = json.loads((tmp_path / "fetch_report.json").read_text(encoding="utf-8"))
    assert rc == 1 and len(report["errors"]) == 3


# GitHub Actions에서 잘못된 키로 실제 API를 호출했을 때 받은 응답 (2026-11-04)
REAL_INVALID_KEY_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<OpenAPI_ServiceResponse>
<cmmMsgHeader>
  <errMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</errMsg>
  <returnAuthMsg>등록되지 않은 서비스키</returnAuthMsg>
  <returnReasonCode>30</returnReasonCode>
</cmmMsgHeader>
</OpenAPI_ServiceResponse>"""


def test_real_invalid_key_response():
    with pytest.raises(fa.ApiError, match="30.*등록되지 않은 서비스키"):
        fa.parse_page(REAL_INVALID_KEY_RESPONSE)
