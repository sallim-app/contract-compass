"""모바일 인앱 브라우저 스크롤 불변식 회귀 방지 (T-2026W33-178).

배경(2026-08-14 실기기·헤드리스 실측): 스레드 인앱 브라우저에서 홈을 위로 드래그한 뒤
아래로 스크롤이 되지 않았다. 원인은 **스크롤 면이 둘**이었던 것 —
홈이 `position:fixed; inset:0; overflow:auto` 오버레이(844/1844)로 뜨고, 그 아래
위저드 문서가 따로 스크롤(1085)되고 있었다. 인앱 브라우저는 툴바 접힘·닫기 제스처를
**문서 스크롤러**에 묶으므로, 보이지 않는 문서가 드래그를 먹으면 화면은 멈춘 것처럼 보인다.

여기서 고정하는 불변식
  1. home_is_document_scroll — 홈 진입 화면은 고정 오버레이 스크롤러가 아니다
  2. wizard_folded_while_home — 홈이 열린 동안 뒤 위저드를 접어 문서가 둘로 늘지 않는다
  3. overlay_scrollers_contain — 전면 오버레이의 스크롤러는 뒤 문서로 스크롤을 흘리지 않는다

실측 확인 경로(사람): `python3 /data/ops/probe_browser.py shot --width 390 <URL>` +
문서/스크롤러 개수 점검. 여기서는 소스 불변식만 잠근다.
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "frontend" / "src"

HOME = SRC / "pages" / "HomeDashboard.tsx"
APP = SRC / "App.tsx"
RESPONSIVE = SRC / "styles" / "designer" / "flow-responsive.css"

# 전면(풀스크린) 오버레이 안에서 실제로 스크롤되는 요소들 — 뒤 문서로 스크롤이 이어지면
# 인앱 브라우저에서 "모달을 만졌는데 뒤 페이지가 움직이는" 상태가 된다.
OVERLAY_SCROLLERS = [
    (SRC / "styles" / "designer" / "glossary.css", ".gl-body"),
    (SRC / "styles" / "designer" / "source-drawer.css", ".src-scroll"),
]


def _rule(css: str, selector: str) -> str:
    """`selector { … }` 한 덩어리를 돌려준다(없으면 빈 문자열)."""
    for line in css.splitlines():
        stripped = line.strip()
        if stripped.startswith(selector) and "{" in stripped:
            return stripped
    return ""


def test_home_is_document_scroll():
    """홈 진입 화면이 고정 오버레이 스크롤러로 되돌아가면 실패한다."""
    home = HOME.read_text(encoding="utf-8")
    root = home.split("return (", 1)[1][:600]
    assert "className=\"home-page\"" in root, "홈 루트가 .home-page(문서 스크롤)가 아니다"
    assert "position: 'fixed'" not in root, (
        "홈 루트가 다시 position:fixed 오버레이다 — 문서 스크롤과 둘로 갈린다(T-2026W33-178)")
    assert ".home-page" in RESPONSIVE.read_text(encoding="utf-8"), ".home-page 스타일이 없다"


def test_wizard_folded_while_home():
    """홈이 열린 동안 위저드를 접지 않으면 문서에 스크롤 대상이 두 벌 쌓인다."""
    assert "home-open" in APP.read_text(encoding="utf-8"), (
        "App이 홈 열림 상태를 .home-open으로 표시하지 않는다")
    css = RESPONSIVE.read_text(encoding="utf-8")
    assert ".dt-app.home-open > .dt-top" in css and ".dt-app.home-open > .dt-cols" in css, (
        "홈이 열려도 위저드 상단바·본문이 접히지 않는다")
    assert "display: none" in css.split(".dt-app.home-open > .dt-cols", 1)[1][:40]


@pytest.mark.parametrize(("path", "selector"), OVERLAY_SCROLLERS,
                         ids=[s for _, s in OVERLAY_SCROLLERS])
def test_overlay_scrollers_contain(path: Path, selector: str):
    """오버레이 스크롤러는 overscroll-behavior-y:contain으로 뒤 문서와 끊겨 있어야 한다."""
    rule = _rule(path.read_text(encoding="utf-8"), selector)
    assert rule, f"{selector} 규칙을 {path.name}에서 찾지 못했다"
    assert "overscroll-behavior-y: contain" in rule, (
        f"{selector}에 overscroll-behavior-y:contain이 없다 — 스크롤이 뒤 문서로 샌다")
    assert "touch-action: pan-y" in rule, (
        f"{selector}에 touch-action:pan-y가 없다 — 인앱 브라우저 제스처와 충돌한다")
