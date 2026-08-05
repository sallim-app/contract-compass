#!/usr/bin/env python3
"""IndexNow 색인 제출 — sitemap.xml의 URL을 Bing/Yandex 계열에 알린다.

**반드시 라이브 반영 후에만 실행하라.** 아직 서빙되지 않는 URL을 제출하면 크롤러가
404/소프트404를 받아 그 URL이 색인 후보에서 밀린다. 순서는 하나뿐이다:

    1) python3 tools/build_seo_pages.py
    2) cd frontend && npm run build        # dist로 복사 = 라이브 반영
    3) python3 tools/indexnow_ping.py --verify-live   # 200 확인 후 제출

키 파일: `frontend/public/<key>.txt` (내용이 키와 같아야 IndexNow가 소유권을 인정한다).
현재 키는 리포에 이미 있다 — 이 스크립트가 파일명에서 키를 읽는다.

구글은 IndexNow를 쓰지 않는다. 구글 색인은 sitemap.xml + Search Console 몫이다
(GSC 자동점검은 별건 과제 T-2026W32-82).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PUBLIC = BASE / "frontend" / "public"
SITEMAP = PUBLIC / "sitemap.xml"
HOST = "contract.sallim.app"
ENDPOINT = "https://api.indexnow.org/IndexNow"


def find_key() -> str | None:
    for f in PUBLIC.glob("*.txt"):
        if re.fullmatch(r"[0-9a-f]{8,128}", f.stem):
            content = f.read_text(encoding="utf-8").strip()
            if content != f.stem:
                print(f"✗ 키 파일 내용 불일치: {f.name} (내용='{content}') — "
                      "IndexNow는 파일 내용이 키와 같아야 소유권을 인정한다")
                return None
            return f.stem
    return None


def sitemap_urls() -> list[str]:
    if not SITEMAP.exists():
        return []
    return re.findall(r"<loc>(.*?)</loc>", SITEMAP.read_text(encoding="utf-8"))


def check_live(urls: list[str], limit: int = 8) -> bool:
    """대표 표본이 실제로 200을 주는지 확인. 하나라도 실패면 제출하지 않는다."""
    step = max(1, len(urls) // limit)
    sample = urls[::step][:limit]
    ok = True
    for u in sample:
        try:
            req = urllib.request.Request(u, method="GET")
            with urllib.request.urlopen(req, timeout=15) as r:
                code, body = r.status, r.read(65536).decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as e:
            print(f"  ERR {u} — {e}")
            ok = False
            continue
        # 판정 기준은 **그 URL의 canonical이 박혀 있는가**다. `id="root"` 부재를
        # 근거로 삼으면(2026-08-06 codex 지적) CDN 오류 페이지·다른 정적 문서가 200을
        # 주는 순간 "반영됨"으로 오판한다. 200은 증거가 아니다 — 내가 만든 그 문서가
        # 맞다는 증거가 필요하다.
        if u.rstrip("/") == f"https://{HOST}":
            served = code == 200
            why = "홈"
        else:
            served = code == 200 and f'<link rel="canonical" href="{u}">' in body
            why = "OK" if served else "미반영(canonical 불일치 — SPA 폴백/오류 페이지 의심)"
        print(f"  {code} {why} {u}")
        if not served:
            ok = False
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify-live", action="store_true",
                    help="제출 전 표본 URL이 실제로 서빙되는지 확인(권장)")
    ap.add_argument("--yes", action="store_true", help="확인 없이 제출")
    args = ap.parse_args()

    urls = sitemap_urls()
    if not urls:
        print("✗ sitemap.xml에 URL이 없다 — 먼저 build_seo_pages.py를 실행하라")
        return 1
    key = find_key()
    if not key:
        print("✗ IndexNow 키 파일을 찾지 못했다 (frontend/public/<64hex>.txt)")
        return 1
    print(f"URL {len(urls)}건 · 키 {key[:8]}… · 호스트 {HOST}")

    if args.verify_live:
        print("라이브 표본 확인:")
        if not check_live(urls):
            print("✗ 라이브에 반영되지 않은 URL이 있다 — `cd frontend && npm run build` 후 다시 시도하라. 제출하지 않았다.")
            return 1
        print("  표본 전건 200 · SPA 폴백 없음")
    elif not args.yes:
        print("✗ --verify-live 또는 --yes 없이는 제출하지 않는다 "
              "(미반영 URL 제출은 색인에 해롭다)")
        return 1

    payload = json.dumps({"host": HOST, "key": key, "urlList": urls}).encode()
    req = urllib.request.Request(ENDPOINT, data=payload,
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"제출 완료: HTTP {r.status} · URL {len(urls)}건")
    except urllib.error.HTTPError as e:
        print(f"✗ 제출 실패: HTTP {e.code} {e.read(500).decode('utf-8', 'replace')}")
        return 1
    except (urllib.error.URLError, OSError) as e:
        print(f"✗ 제출 실패: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
