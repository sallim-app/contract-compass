"""요금 페이지 구매 버튼 게이트 (2026-08-11, T-2026W33-59).

Creem 라이브 온보딩 미완이면 체크아웃이 **렌더 계층에서만** 'Payment Error'를 띄우고
HTTP는 200이라, 상태코드 감시가 원리적으로 못 잡는다. 그래서 "게이트가 꺼져 있으면
결제 링크가 페이지에 아예 없다"를 코드 레벨에서 못박아 둔다.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "mcp"))

pytestmark = pytest.mark.unit

server = pytest.importorskip("server", reason="mcp SDK 미설치 환경")


def test_gate_off_hides_all_purchase_links(monkeypatch):
    monkeypatch.delenv("CREEM_CHECKOUT_LIVE", raising=False)
    html = server._pricing_html()
    assert "creem.io/product/" not in html
    assert "결제 개통 준비 중" in html
    assert "contract@sallim.app" in html
    # 무료 티어 안내는 살아 있어야 한다 — 결제만 막고 서비스는 그대로.
    assert "IP당 50콜" in html


def test_gate_on_restores_three_links(monkeypatch):
    monkeypatch.setenv("CREEM_CHECKOUT_LIVE", "1")
    html = server._pricing_html()
    for url in server._CHECKOUT_LINKS.values():
        assert url in html
    assert "결제 개통 준비 중" not in html


def test_no_unreplaced_placeholders(monkeypatch):
    for val in ("", "1"):
        monkeypatch.setenv("CREEM_CHECKOUT_LIVE", val)
        html = server._pricing_html()
        assert "<!--BUY_" not in html
        assert "<!--NOTICE-->" not in html
        assert "<!--PAY_SUFFIX-->" not in html
