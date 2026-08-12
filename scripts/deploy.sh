#!/usr/bin/env bash
# 프런트 배포 절차 고정 (T-2026W33-12, 2026-08-13)
#
# 왜 스크립트인가
# ---------------
# `frontend/dist/`는 .gitignore인데 `backend/main.py`가 그것을 서빙한다. 그래서 프런트
# 소스를 커밋해도 누가 빌드를 돌리지 않으면 라이브가 안 바뀐다. 2026-08-10 실측: 라이브
# 번들이 8/6 01:56 빌드본이라 홈→생성 페이지 허브 링크 커밋(c75f2c9, 8/6 03:58)이 4일간
# 미반영이었고, 롱테일 113건이 sitemap에만 있고 내부링크 0인 고아 페이지였다. 같은 새벽의
# 수동 복사는 `/g/*.html`만 옮기고 `dist/assets`를 빼서 절반만 반영된 상태도 만들었다.
# 절차가 사람 기억에 있으면 이 사고가 다시 난다 — 순서를 파일로 고정한다.
#
# 순서가 중요한 이유
#   1. 생성 페이지·sitemap은 `frontend/public/`에 만들어지고 vite가 그걸 dist로 복사한다.
#      따라서 **생성 → 빌드** 순서여야 한다. 반대로 하면 새 페이지가 라이브에 없다.
#   2. 생성기는 fail-closed다(게이트 위반 시 기록 안 함, rc=1) — 여기서 멈춰야 맞다.
#   3. 빌드는 **임시 디렉토리로** 낸다. vite는 outDir을 비우므로(emptyOutDir 기본참) 라이브
#      dist에 직접 빌드하면 tsc 실패 한 번이 사이트를 비운다. 성공한 산출물만 rename으로
#      갈아끼운다(교체 창 = rename 2회).
#   4. 교체 후 `dist_status`로 실제 산출물을 판정한다 — public 파리티·번들 참조·허브 링크.
#      "빌드가 rc=0이었다"는 배포됐다는 증거가 아니다.
#
# 백엔드 재시작은 필요 없다(FastAPI가 요청마다 dist를 읽는다). 색인 제출(indexnow)은
# 대외 발송이라 여기서 자동으로 하지 않는다 — 마지막에 명령만 안내한다.
#
# 사용법
#   scripts/deploy.sh            # 생성 → 빌드 → 교체 → 검증
#   scripts/deploy.sh --check    # 아무것도 쓰지 않고 현재 배포 상태만 판정
set -euo pipefail

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$BASE"

CHECK=0
[[ "${1:-}" == "--check" ]] && CHECK=1

verify() {
  python3 - <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
from datetime import datetime
from backend.services.dist_status import scan_dist, evaluate_dist_freshness

base = Path.cwd()
snap = scan_dist(base / "frontend" / "dist", base / "frontend" / "public", base)
info, warns = evaluate_dist_freshness(snap, datetime.now().timestamp())
d = info.get("dist") or {}
print(f"  빌드 {d.get('built_at')} · 소스 커밋 {d.get('source_committed_at')} · "
      f"생성 산출물 {d.get('public_files')}건 · 허브 링크 {snap.get('seo_hub_linked')}")
for w in warns:
    print(f"  ✗ {w}")
sys.exit(1 if warns else 0)
PY
}

if [[ $CHECK -eq 1 ]]; then
  echo "[검증만] 현재 배포 상태 판정"
  verify
  echo "  ✓ 경고 0건 — 라이브 dist가 소스·생성 산출물과 일치"
  exit 0
fi

echo "[1/4] 생성 페이지·sitemap 재생성 (fail-closed 게이트)"
python3 tools/build_seo_pages.py --strict

echo "[2/4] 프런트 빌드 → 임시 디렉토리 (실패해도 라이브 dist 무손상)"
[[ -d frontend/node_modules ]] || { echo "  ✗ frontend/node_modules 없음 — 먼저 'cd frontend && npm ci'" >&2; exit 1; }
STAGE="dist.stage.$$"
rm -rf "frontend/$STAGE"
trap 'rm -rf "$BASE/frontend/$STAGE"' EXIT
( cd frontend && npm run build -- --outDir "$STAGE" --emptyOutDir )
[[ -f "frontend/$STAGE/index.html" ]] || { echo "  ✗ 빌드 산출물에 index.html 없음" >&2; exit 1; }

echo "[3/4] 라이브 dist 교체 (rename — 백엔드 재시작 불필요)"
PREV="dist.prev.$$"
if [[ -d frontend/dist ]]; then mv frontend/dist "frontend/$PREV"; fi
mv "frontend/$STAGE" frontend/dist
rm -rf "frontend/$PREV"

echo "[4/4] 배포 산출물 검증 (public 파리티 · 번들 참조 · 허브 링크)"
if ! verify; then
  echo "  ✗ 검증 실패 — 위 경고를 해소하고 다시 실행하라" >&2
  exit 1
fi
echo "  ✓ 경고 0건"
echo
echo "완료. 색인 제출이 필요하면(대외 발송이라 자동 실행 안 함):"
echo "  python3 tools/indexnow_ping.py --verify-live"
