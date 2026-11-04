#!/usr/bin/env bash
# 커밋되면 안 되는 파일(.env, DB 파일, 원본 데이터, 실행 기록)이 추적되고 있으면 실패한다.
set -euo pipefail
pattern='(^|/)\.env($|\.)|\.duckdb(\.wal)?$|^data/raw/|^traces/|^runs/'
found=$(git ls-files | grep -E "$pattern" || true)
if [[ -n "$found" ]]; then
  echo "커밋하면 안 되는 파일이 있습니다:"
  echo "$found"
  exit 1
fi
echo "금지 파일 없음"
