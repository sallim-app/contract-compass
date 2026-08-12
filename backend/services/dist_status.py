"""배포된 프런트 산출물(dist) 신선도 판독 — /ready 경고 (T-2026W33-12, 2026-08-13).

배경(실사고)
------------
`frontend/dist/`는 .gitignore인데 `backend/main.py`가 그것을 서빙한다. 그래서 프런트
소스를 커밋해도 **누가 빌드를 돌리지 않으면 라이브가 안 바뀐다 — 그리고 아무도 모른다.**
2026-08-10 실측: 라이브 번들이 8/6 01:56 빌드본이라, 홈에서 생성 페이지 허브로 가는
링크 커밋(c75f2c9, 8/6 03:58)이 4일간 미반영이었다. 결과로 프로그래매틱 SEO 롱테일
113건이 sitemap에만 있고 내부링크가 0인 고아 페이지였다(유입 배관 사망). 같은 새벽의
수동 복사는 `/g/*.html`만 옮기고 `dist/assets`는 제외해 절반만 반영된 상태도 만들었다.

즉 결함의 정체는 "빌드를 잊는 것"이 아니라 **잊었는지 아무도 볼 수 없는 것**이다.
그래서 판정값을 파일·프롬프트가 아니라 **런타임이 대준다**(회사 기치: 도구 계층).

무엇을 신호로 쓰는가 — mtime을 쓰지 않는 곳
------------------------------------------
- **소스 최신성**: `frontend/src` 등의 **git 커밋 시각**을 쓴다. 파일 mtime은 체크아웃·
  생성기 재실행마다 갱신돼(실측: `build_seo_pages.py`가 동일 내용을 rmtree 후 재기록)
  영구 경고를 만든다. 커밋 시각은 그 잡음이 없고, 실사고(커밋 03:58 > 빌드 01:56)를
  정확히 잡는다.
- **생성 산출물 반영**: `frontend/public` → `frontend/dist` **바이트 대조**. 시각 비교가
  아니라 내용 대조라, 8/10 "절반만 복사" 같은 부분 반영을 확실히 잡는다.
- **번들 참조 무결성**: `dist/index.html`이 가리키는 `/assets/*`가 실물로 있는지.
  없으면 사용자에겐 빈 화면인데 HTTP는 200이라 밖에서 안 보인다.
- **허브 링크 생존**: 홈 번들 안에 `/g/index.html` 문자열이 있는지 — 롱테일 113건의
  유일한 사내 진입 경로다. 소스에는 있는데 번들에 없으면 그게 바로 실사고 상태다.

역순 적용 안전장치: `index_status`와 같은 규약을 따른다. `frontend/dist` 자체가 없으면
**신호 없음**(백엔드 단독 배포·폐쇄망 표준대기본이 정상적으로 그렇다)이고, dist 디렉토리는
있는데 index.html이 없을 때만 부분 빌드로 보고 경고한다. 전부 warning이며 failure로
올리지 않는다 — 이 판독이 서비스를 503으로 떨어뜨리는 일은 없어야 한다.

비용: /ready 1회당 public 116개 파일 + 번들 1.3MB 읽기(페이지 캐시 히트 ~수 ms).
같은 엔드포인트가 이미 chroma 20,000+ 청크 카운트를 돌리므로 캐시를 두지 않는다.
"""
from __future__ import annotations

import re
import subprocess
from datetime import datetime
from pathlib import Path

# dist/index.html이 참조하는 로컬 자산 (`src="/assets/index-XXXX.js"`)
_ASSET_REF = re.compile(r'(?:src|href)="/(assets/[A-Za-z0-9._@-]+)"')

# 롱테일 생성 페이지의 유일한 사내 진입 경로 (frontend/src/pages/HomeDashboard.tsx)
SEO_HUB_LINK = "/g/index.html"

# 빌드 결과를 바꾸는 소스 — 이 경로들의 최신 커밋이 dist보다 새로우면 재빌드가 밀렸다.
# public/은 제외한다(생성기 산출물이라 커밋 시각이 아니라 바이트 대조로 본다).
SOURCE_PATHS = (
    "frontend/src",
    "frontend/index.html",
    "frontend/package.json",
    "frontend/vite.config.ts",
)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def _git_last_commit_ts(repo_root: Path, paths: tuple[str, ...]) -> float | None:
    """주어진 경로들을 건드린 마지막 커밋 시각(epoch). 판독 불가면 None(=신호 없음)."""
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_root), "log", "-1", "--format=%ct", "--", *paths],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return None
        out = r.stdout.strip()
        return float(out) if out else None
    except Exception:
        return None


def _bundle_has(dist_dir: Path, needle: str) -> bool | None:
    """dist/assets의 js 번들 어딘가에 needle이 있나. 자산이 없으면 None(판정 불가)."""
    assets = dist_dir / "assets"
    if not assets.is_dir():
        return None
    js = sorted(assets.glob("*.js"))
    if not js:
        return None
    for f in js:
        try:
            if needle in f.read_text(encoding="utf-8", errors="ignore"):
                return True
        except OSError:
            return None
    return False


def scan_dist(dist_dir: Path, public_dir: Path, repo_root: Path) -> dict:
    """판정 재료 수집(불순부) — 판정은 evaluate_dist_freshness가 한다(테스트 대상).

    반환 키: dist_dir_exists · dist_built_at · source_committed_at · public_files ·
             public_unsynced · asset_refs_missing · seo_hub_linked
    """
    snap: dict = {
        "dist_dir_exists": dist_dir.is_dir(),
        "dist_built_at": None,
        "source_committed_at": None,
        "public_files": 0,
        "public_unsynced": [],
        "asset_refs_missing": [],
        "seo_hub_linked": None,
    }
    index = dist_dir / "index.html"
    if not index.is_file():
        return snap
    snap["dist_built_at"] = index.stat().st_mtime
    snap["source_committed_at"] = _git_last_commit_ts(repo_root, SOURCE_PATHS)

    # 번들 참조 무결성
    try:
        html = index.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        html = ""
    for rel in sorted(set(_ASSET_REF.findall(html))):
        if not (dist_dir / rel).is_file():
            snap["asset_refs_missing"].append(rel)

    # 생성 산출물(public) → dist 바이트 대조
    if public_dir.is_dir():
        n, unsynced = 0, []
        for f in sorted(public_dir.rglob("*")):
            if not f.is_file():
                continue
            n += 1
            rel = f.relative_to(public_dir).as_posix()
            target = dist_dir / rel
            try:
                if not target.is_file() or target.read_bytes() != f.read_bytes():
                    unsynced.append(rel)
            except OSError:
                unsynced.append(rel)
        snap["public_files"], snap["public_unsynced"] = n, unsynced

    # 허브 링크 생존 — 생성 페이지가 있을 때만 판정한다(없으면 링크가 없는 게 맞다)
    if (public_dir / "g" / "index.html").is_file():
        snap["seo_hub_linked"] = _bundle_has(dist_dir, SEO_HUB_LINK)
    return snap


def evaluate_dist_freshness(snap: dict, now_ts: float) -> tuple[dict, list[str]]:
    """dist 신선도 판정 (순수 함수 — /ready에서 호출, 단위테스트 대상).

    반환: (info, warnings). failure는 만들지 않는다 — 이 판독으로 503을 내지 않는다.
    """
    info: dict = {}
    warnings: list[str] = []
    snap = snap or {}
    built = snap.get("dist_built_at")
    if built is None:
        # dist 디렉토리 자체가 없으면 백엔드 단독 배포 — 신호 없음.
        info["dist_built_at"] = None
        if snap.get("dist_dir_exists"):
            warnings.append(
                "프런트 dist에 index.html이 없다 — 빌드가 중단됐거나 부분 복사된 상태"
                "(scripts/deploy.sh로 재빌드)")
        return info, warnings

    d: dict = {"built_at": _iso(built), "age_d": round((now_ts - built) / 86400, 1)}
    if snap.get("public_files"):
        d["public_files"] = snap["public_files"]

    src = snap.get("source_committed_at")
    if src is not None:
        d["source_committed_at"] = _iso(src)
        if src > built:
            warnings.append(
                f"프런트 dist가 소스보다 오래됐다 — 소스 커밋 {_iso(src)} > dist 빌드 "
                f"{_iso(built)}. 커밋한 프런트 변경이 라이브에 없다(scripts/deploy.sh로 재빌드)")

    unsynced = snap.get("public_unsynced") or []
    if unsynced:
        d["public_unsynced"] = len(unsynced)
        head = ", ".join(unsynced[:5])
        more = f" 외 {len(unsynced) - 5}건" if len(unsynced) > 5 else ""
        warnings.append(
            f"생성 산출물 {len(unsynced)}건이 dist에 미반영 — {head}{more}"
            " (public/ ↔ dist/ 내용 불일치, scripts/deploy.sh로 재빌드)")

    missing = snap.get("asset_refs_missing") or []
    if missing:
        d["asset_refs_missing"] = missing
        warnings.append(
            f"index.html이 참조하는 번들 {len(missing)}건이 없다: {', '.join(missing)}"
            " — 사용자에겐 빈 화면인데 HTTP는 200이다")

    if snap.get("seo_hub_linked") is False:
        warnings.append(
            f"홈 번들에 생성 페이지 허브 링크({SEO_HUB_LINK})가 없다 — 롱테일 페이지가"
            " sitemap에만 있고 내부링크 0인 고아 상태(2026-08-10 실사고와 동일)")
    elif snap.get("seo_hub_linked") is True:
        d["seo_hub_linked"] = True

    info["dist"] = d
    return info, warnings
