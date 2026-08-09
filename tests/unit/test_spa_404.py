"""SPA 폴백의 소프트404 회귀 방지 (T-2026W33-11).

배경: serve_spa가 미존재 경로를 전부 index.html **200**으로 반환했다(라이브 실측
/g/anything.html·/totally-bogus-xyz·/g/ 모두 200). 프로그래매틱 SEO 롱테일 113건을
색인시킨 직후라 크롤러가 존재하지 않는 URL을 정상 페이지로 학습한다.

여기서 고정하는 불변식
  1. 확장자가 있는데 실물이 없으면 404            (spa_404_unknown_extension)
  2. g/·assets/ 네임스페이스의 미존재 경로는 404  (spa_404_g_namespace)
  3. 확장자 없는 일반 경로는 종전대로 200 폴백    (spa_fallback_preserved)
  4. 실물 정적 파일은 계속 200, dist 밖으로는 못 나간다
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from backend.main import DIST_DIR, resolve_dist_path, spa_should_404  # noqa: E402


# ── 순수 판정 (빌드 산출물 없이도 도는 층) ────────────────────────────────────
@pytest.mark.parametrize("path", [
    "anything.html", "g/anything.html", "favicon.ico", "a/b/c.txt", "index.php",
])
def test_extension_paths_are_404(path):
    assert spa_should_404(path) is True


@pytest.mark.parametrize("path", ["g", "g/", "g/bogus", "assets/nope", "g/a/b"])
def test_namespaces_are_404(path):
    """g/·assets/는 생성물 전용 네임스페이스 — SPA 라우트가 아니다."""
    assert spa_should_404(path) is True


@pytest.mark.parametrize("path", ["", "totally-bogus-xyz", "some/deep/route"])
def test_extensionless_paths_keep_spa_fallback(path):
    """라우터가 붙을 여지를 남긴다 — 확장자 없는 경로는 폴백 유지."""
    assert spa_should_404(path) is False


def test_path_traversal_is_contained():
    assert resolve_dist_path("../../etc/passwd") is None
    assert resolve_dist_path("g/../../backend/main.py") is None
    assert resolve_dist_path("g") is not None


# ── 실제 라우트 (dist 빌드가 있을 때만) ───────────────────────────────────────
pytestmark_dist = pytest.mark.skipif(
    not (DIST_DIR / "index.html").is_file(), reason="frontend/dist 빌드 없음")


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from backend.main import app
    return TestClient(app)  # lifespan 미실행 — 워밍업(모델 로드) 없이 라우팅만 탄다


@pytestmark_dist
@pytest.mark.parametrize("path", ["/nope.html", "/g/anything.html", "/favicon-not-here.ico"])
def test_live_unknown_extension_404(client, path):
    assert client.get(path).status_code == 404


@pytestmark_dist
def test_live_g_namespace_404(client):
    assert client.get("/g/bogus-page").status_code == 404


@pytestmark_dist
def test_live_fallback_preserved(client):
    r = client.get("/totally-bogus-xyz")
    assert r.status_code == 200 and "<!doctype html" in r.text.lower()
    assert client.get("/").status_code == 200


@pytestmark_dist
def test_live_real_pages_still_served(client):
    assert client.get("/sitemap.xml").status_code == 200
    assert client.get("/robots.txt").status_code == 200
    if (DIST_DIR / "g" / "index.html").is_file():
        assert client.get("/g/index.html").status_code == 200
        r = client.get("/g/", follow_redirects=False)
        assert r.status_code == 301 and r.headers["location"] == "/g/index.html"
