"""배포 산출물(dist) 신선도 회귀 방지 (T-2026W33-12).

배경: `frontend/dist/`는 .gitignore인데 backend/main.py가 그것을 서빙한다. 2026-08-10
실측으로 라이브 번들이 8/6 01:56 빌드본이었고, 홈→생성 페이지 허브 링크 커밋(c75f2c9,
8/6 03:58)이 4일간 미반영이었다. 롱테일 113건이 sitemap에만 있고 내부링크 0인 고아
페이지 상태였는데 **밖에서 보이는 신호가 하나도 없었다**(HTTP 200, /ready ok).

여기서 고정하는 불변식
  1. dist_freshness_ready_warning — 소스 커밋이 dist 빌드보다 새로우면 /ready가 경고한다
     (부분 반영·번들 참조 누락·허브 링크 소실도 같은 층에서 경고)
  2. seo_pages_linked_from_home — 홈에서 생성 페이지 허브로 가는 링크가 소스에 있고,
     빌드된 번들에도 살아 있다
그리고 이 판독이 **절대 failure(503)로 올라가지 않는다**는 것도 함께 고정한다.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from backend.services.dist_status import (  # noqa: E402
    SEO_HUB_LINK,
    evaluate_dist_freshness,
    scan_dist,
)

NOW = 1786500000.0  # 2026-08-12 근처 고정 기준시 — 경과일 계산이 오늘 날짜에 흔들리지 않게


def _snap(**over) -> dict:
    """정상 상태 스냅샷 — 개별 결함만 덮어써서 판정을 하나씩 검사한다."""
    base = {
        "dist_dir_exists": True,
        "dist_built_at": NOW - 3600,
        "source_committed_at": NOW - 7200,
        "public_files": 116,
        "public_unsynced": [],
        "asset_refs_missing": [],
        "seo_hub_linked": True,
    }
    base.update(over)
    return base


# ── 1) dist_freshness_ready_warning ──────────────────────────────────────────
def test_healthy_dist_has_no_warning():
    info, warns = evaluate_dist_freshness(_snap(), NOW)
    assert warns == []
    assert info["dist"]["seo_hub_linked"] is True
    assert info["dist"]["public_files"] == 116


def test_source_newer_than_dist_warns():
    """실사고 재현: 소스 커밋 03:58 > dist 빌드 01:56 → 경고."""
    info, warns = evaluate_dist_freshness(
        _snap(dist_built_at=NOW - 7200, source_committed_at=NOW - 3600), NOW)
    assert len(warns) == 1
    assert "dist가 소스보다 오래됐다" in warns[0]
    assert "scripts/deploy.sh" in warns[0]


def test_dist_newer_than_source_is_silent():
    """빌드가 소스보다 새로우면 조용해야 한다 — 상시 경고는 경고가 아니다."""
    _, warns = evaluate_dist_freshness(
        _snap(dist_built_at=NOW - 60, source_committed_at=NOW - 86400), NOW)
    assert warns == []


def test_missing_dist_dir_is_no_signal():
    """백엔드 단독 배포(표준대기본·폐쇄망)는 정상 — 신호 없음으로 다룬다."""
    info, warns = evaluate_dist_freshness(
        {"dist_dir_exists": False, "dist_built_at": None}, NOW)
    assert warns == []
    assert info["dist_built_at"] is None


def test_partial_build_warns():
    """dist 디렉토리는 있는데 index.html이 없다 = 중단된 빌드·부분 복사."""
    _, warns = evaluate_dist_freshness(
        {"dist_dir_exists": True, "dist_built_at": None}, NOW)
    assert len(warns) == 1 and "index.html이 없다" in warns[0]


def test_unsynced_public_files_warn_with_count_and_sample():
    """8/10 '절반만 복사' 재현 — 개수와 표본을 함께 알려야 사람이 판단할 수 있다."""
    unsynced = [f"g/page-{i}.html" for i in range(9)]
    info, warns = evaluate_dist_freshness(_snap(public_unsynced=unsynced), NOW)
    assert len(warns) == 1
    assert "9건이 dist에 미반영" in warns[0]
    assert "g/page-0.html" in warns[0] and "외 4건" in warns[0]
    assert info["dist"]["public_unsynced"] == 9


def test_missing_asset_refs_warn():
    """index.html이 없는 번들을 가리키면 사용자에겐 빈 화면인데 HTTP는 200이다."""
    _, warns = evaluate_dist_freshness(
        _snap(asset_refs_missing=["assets/index-CIZAVlOl.js"]), NOW)
    assert len(warns) == 1 and "빈 화면" in warns[0]


def test_seo_hub_link_missing_from_bundle_warns():
    """실사고의 핵심 — 소스엔 링크가 있는데 배포 번들엔 없는 상태."""
    _, warns = evaluate_dist_freshness(_snap(seo_hub_linked=False), NOW)
    assert len(warns) == 1
    assert SEO_HUB_LINK in warns[0] and "고아" in warns[0]


def test_seo_hub_link_unknown_is_silent():
    """생성 페이지가 없으면 링크가 없는 게 맞다 — 판정 불가는 경고가 아니다."""
    _, warns = evaluate_dist_freshness(_snap(seo_hub_linked=None), NOW)
    assert warns == []


def test_evaluate_never_raises_on_garbage():
    """판독부는 /ready 안에서 돈다 — 어떤 입력에도 예외를 던지면 안 된다."""
    for bad in ({}, None, {"dist_built_at": None}, {"dist_built_at": NOW, "public_unsynced": None}):
        info, warns = evaluate_dist_freshness(bad, NOW)
        assert isinstance(info, dict) and isinstance(warns, list)


# ── 스캐너: 합성 트리로 실제 파일 판정 검사 ────────────────────────────────────
def _make_tree(tmp_path: Path, *, bundle_body: str, copy_public: bool = True):
    dist, public = tmp_path / "dist", tmp_path / "public"
    (public / "g").mkdir(parents=True)
    (public / "g" / "index.html").write_text("<html>허브</html>", encoding="utf-8")
    (public / "robots.txt").write_text("User-agent: *\n", encoding="utf-8")
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        '<html><script src="/assets/index-AAA.js"></script></html>', encoding="utf-8")
    (dist / "assets" / "index-AAA.js").write_text(bundle_body, encoding="utf-8")
    if copy_public:
        (dist / "g").mkdir()
        (dist / "g" / "index.html").write_text("<html>허브</html>", encoding="utf-8")
        (dist / "robots.txt").write_text("User-agent: *\n", encoding="utf-8")
    return dist, public


def test_scan_detects_synced_tree(tmp_path):
    dist, public = _make_tree(tmp_path, bundle_body=f'a("{SEO_HUB_LINK}")')
    snap = scan_dist(dist, public, tmp_path)
    assert snap["public_unsynced"] == [] and snap["public_files"] == 2
    assert snap["asset_refs_missing"] == [] and snap["seo_hub_linked"] is True
    assert evaluate_dist_freshness(snap, NOW)[1] == []


def test_scan_detects_orphan_and_unsynced(tmp_path):
    """허브 링크 없는 번들 + public 미복사 = 2026-08-10 라이브 상태."""
    dist, public = _make_tree(tmp_path, bundle_body='a("nothing")', copy_public=False)
    snap = scan_dist(dist, public, tmp_path)
    assert snap["seo_hub_linked"] is False
    assert sorted(snap["public_unsynced"]) == ["g/index.html", "robots.txt"]
    warns = evaluate_dist_freshness(snap, NOW)[1]
    assert len(warns) == 2


def test_scan_detects_missing_bundle(tmp_path):
    dist, public = _make_tree(tmp_path, bundle_body="x")
    (dist / "assets" / "index-AAA.js").unlink()
    snap = scan_dist(dist, public, tmp_path)
    assert snap["asset_refs_missing"] == ["assets/index-AAA.js"]


def test_scan_no_git_is_no_signal(tmp_path):
    """git 없는 트리에서 소스 최신성은 None — 경고를 쏟지 않는다."""
    dist, public = _make_tree(tmp_path, bundle_body=f'a("{SEO_HUB_LINK}")')
    assert scan_dist(dist, public, tmp_path)["source_committed_at"] is None


# ── 2) seo_pages_linked_from_home ────────────────────────────────────────────
def test_home_source_links_seo_hub():
    """소스 층 — 홈에 허브 링크가 있어야 한다(리팩터로 사라지면 여기서 잡힌다)."""
    home = ROOT / "frontend" / "src" / "pages" / "HomeDashboard.tsx"
    assert home.is_file(), "HomeDashboard.tsx 경로가 바뀌었다 — 이 검사를 갱신하라"
    assert f'href="{SEO_HUB_LINK}"' in home.read_text(encoding="utf-8"), (
        f"홈에서 생성 페이지 허브({SEO_HUB_LINK})로 가는 유일한 사내 진입 경로가 없다")


@pytest.mark.skipif(not (ROOT / "frontend" / "dist" / "index.html").is_file(),
                    reason="frontend/dist 빌드 없음")
def test_built_bundle_links_seo_hub_and_is_synced():
    """빌드 층 — 실사고를 그대로 잡는 검사. 소스에 있어도 번들에 없으면 라이브엔 없다."""
    snap = scan_dist(ROOT / "frontend" / "dist", ROOT / "frontend" / "public", ROOT)
    assert snap["seo_hub_linked"] is True, "배포 번들에 허브 링크가 없다 — 재빌드 필요"
    assert snap["public_unsynced"] == [], f"생성 산출물 미반영: {snap['public_unsynced'][:5]}"
    assert snap["asset_refs_missing"] == []


# ── /ready 계약: warning만, failure 없음 ──────────────────────────────────────
@pytest.mark.skipif(not (ROOT / "frontend" / "dist" / "index.html").is_file(),
                    reason="frontend/dist 빌드 없음")
def test_ready_reports_dist_info_and_never_fails():
    from fastapi.testclient import TestClient
    from backend.main import app
    r = TestClient(app).get("/ready")
    assert r.status_code in (200, 503)
    body = r.json()
    assert "dist" in body["info"] or body["info"].get("dist_built_at") is None
    assert not any("dist" in f for f in body["failures"]), (
        "dist 판독이 failure로 올라갔다 — 이 층은 503을 내지 않아야 한다")
