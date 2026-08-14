#!/usr/bin/env bash
# 대외비 유출 스캔 게이트 — 공개 전 트리 전체를 검사한다.
# 사용: scripts/scan_confidential.sh [ROOT]   (기본: repo 루트)
# 종료코드: 0 = 통과(매치 0건), 1 = 대외비 의심 매치 발견
set -uo pipefail

ROOT="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
SELF="scripts/scan_confidential.sh"

# 원 출처(사내) 유래 어휘·식별자·인프라 흔적. 예외 0건이 통과 기준.
PATTERNS=(
  '대외비'
  '내부용'
  'confidential'
  '계약업무규정'
  '위임전결'
  '계약교재'                # 사내 교재 4종(공사/용역/물품/공공구매계약교재)
  '교재 ?p[0-9]'            # "교재 p42" 류 페이지 인용
  '교재 ?발췌'
  # '교재' 단독은 조달 품목명(교재, 표본및관련교재)·용역 키워드에 정당 등장 — 오탐이라 제외
  '탁상용'
  '팀원의견'
  'K-water'
  'kwater'
  'k-water'
  '케이워터'
  '수자원공사'   # '수자원' 단독은 조달청 분류명(수자원개발서비스 등)에 정당 등장 — 오탐
  'C1[0-9]{9}'          # 사내 계약대장 계약번호
  'media01'
  '203\.237\.'          # BASA 사내 프록시
  'sword33'
  'B551'                # 기관코드
  'sk-[A-Za-z0-9_-]{20}'   # OpenAI 실키
  'AIzaSy[A-Za-z0-9_-]{10}' # Google 실키
)

# 커밋되지 않는 로컬 데이터(.gitignore 대상)는 스캔 제외 — 공개 법령 XML은
# 조문에 '한국수자원공사법' 등 공기업명이 정당하게 등장한다.
EXCLUDES=(
  --exclude-dir=.git
  --exclude-dir=node_modules
  --exclude-dir=dist
  --exclude-dir=__pycache__
  --exclude-dir=.venv
  --exclude-dir=chroma_db
  --exclude-dir=laws
  --exclude-dir=admin_rules
  --exclude-dir=source_docs
  --exclude-dir=logs
)

fail=0
for pat in "${PATTERNS[@]}"; do
  # /etl/data/: 공개 간행물 파싱 산출물(.gitignore) — 원문에 공기업명이 정당 등장
  # tests/qa_bank.json: leak 탐침 질문("한국수자원공사 내규…")이 의도적으로 포함된 테스트 데이터
  # /\.env(\.bak…)?: — .env 본체와 그 날짜접미사 백업(.gitignore:41 '*.bak-*')은 커밋되지
  # 않으므로 공개 표면이 아니다. .env.example(추적 대상)은 계속 스캔한다.
  hits=$(grep -rInE "${EXCLUDES[@]}" -e "$pat" "$ROOT" 2>/dev/null | grep -v "$SELF" | grep -vE "/\.env(\.bak[^:]*)?:" | grep -v "/etl/data/" | grep -v "tests/qa_bank.json" || true)
  if [[ -n "$hits" ]]; then
    fail=1
    echo "=== 매치: /$pat/ ==="
    echo "$hits" | head -30
    n=$(echo "$hits" | wc -l)
    [[ $n -gt 30 ]] && echo "... (+$((n-30))건 더)"
    echo
  fi
done

# 바이너리·데이터 반입 금지 확장자 (커밋 대상 트리 기준 — gitignore 디렉토리는 제외)
# git 저장소가 있으면 추적 파일만, 없으면 로컬 데이터 디렉토리 제외 find.
if [[ -d "$ROOT/.git" ]]; then
  bins=$(cd "$ROOT" && git ls-files | grep -E '\.(pdf|hwpx|docx|xlsx|parquet|sqlite3|db|pkl)$' || true)
else
  bins=$(find "$ROOT" -type f \( -name '*.pdf' -o -name '*.hwpx' -o -name '*.docx' -o -name '*.xlsx' \
    -o -name '*.parquet' -o -name '*.sqlite3' -o -name '*.db' -o -name '*.pkl' \) \
    -not -path '*/node_modules/*' -not -path '*/.git/*' -not -path '*/chroma_db/*' \
    -not -path '*/source_docs/*' -not -path '*/etl/data/*' -not -name 'sessions.db' 2>/dev/null)
fi
if [[ -n "$bins" ]]; then
  fail=1
  echo "=== 반입 금지 바이너리/데이터 파일 ==="
  echo "$bins"
  echo
fi

if [[ $fail -eq 0 ]]; then
  echo "PASS: 대외비 스캔 매치 0건 ($ROOT)"
else
  echo "FAIL: 대외비 의심 매치 존재 — 공개 금지"
fi
exit $fail
