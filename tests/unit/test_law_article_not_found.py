"""조문 미발견 응답의 정직성 회귀 (T-2026W33-146, 2026-08-14).

결함: get_law_article이 **코퍼스에 없는 법령**과 **존재하지 않는 조문**을 같은
문장으로 답하고 법령명을 지웠다 — ref="민사집행법 제229조" / "국가계약법 시행령
제9999조"가 둘 다 {"detail": "제N조 조문을 찾을 수 없습니다"}. 기치② "못 봄 ≠ 없음"
정면 위반이며, 판례 참조조문을 교차확인하던 에이전트가 참조조문이 틀렸다고 말하거나
자체 지식으로 조용히 폴백하는 결과를 낳았다(Claude 탐침 실측).

여기서 지키는 것:
  (a) 코퍼스 밖 법령과 없는 조문이 **서로 다른** 메시지·law_in_corpus를 낸다
  (b) 응답에서 **법령명이 지워지지 않는다**(ref 전문·law_name 보존)
  (c) 조회 가능한 법령 목록(corpus_laws)과 행동 지침(hint)이 실린다
  (d) MCP _friendly_error가 그 구조를 str()로 뭉개지 않고 그대로 중계한다
chroma 미사용(fake col).
"""
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import backend.api.v1.law as law  # noqa: E402

pytestmark = pytest.mark.unit

# 코퍼스 실측 표본(law_name은 XML 약칭 표기 그대로)
ROWS = [
    ("국가계약법 시행령 제26조\n수의계약에 의할 수 있는 경우는 다음과 같다.",
     {"law_name": "국가계약법 시행령", "article_titles": "제26조",
      "law_ref": "국가계약법 시행령 제26조", "chunk_level": "child"}),
    ("지방계약법 시행령 제26조\n지방자치단체의 수의계약 사유.",
     {"law_name": "지방계약법 시행령", "article_titles": "제26조",
      "law_ref": "지방계약법 시행령 제26조", "chunk_level": "child"}),
    ("국가계약법 제27조\n부정당업자의 입찰참가자격 제한.",
     {"law_name": "국가계약법", "article_titles": "제27조",
      "law_ref": "국가계약법 제27조", "chunk_level": "child"}),
]


class _FakeCol:
    """where 필터만 흉내내는 가짜 컬렉션 — 전수 get(where=None)도 지원."""

    def __init__(self, rows):
        self.rows = rows

    def get(self, where=None, where_document=None, include=None, limit=None):
        rows = self.rows
        if where and "article_titles" in where:
            rows = [r for r in rows if r[1].get("article_titles") == where["article_titles"]]
        elif where and "parent_ref" in where:
            rows = [r for r in rows if r[1].get("parent_ref") == where["parent_ref"]]
        return {"documents": [d for d, _ in rows], "metadatas": [m for _, m in rows]}


@pytest.fixture(autouse=True)
def fake_collection(monkeypatch):
    col = _FakeCol(ROWS)
    monkeypatch.setattr(law, "_get_collection", lambda: col)
    law._corpus_law_names.cache_clear()
    yield col
    law._corpus_law_names.cache_clear()


def _detail(ref: str) -> dict:
    with pytest.raises(HTTPException) as ei:
        law.get_article(ref=ref)
    assert ei.value.status_code == 404
    detail = ei.value.detail
    assert isinstance(detail, dict), "404 detail이 구조화 dict가 아니다(문자열 회귀)"
    return detail


def test_outside_corpus_law_is_not_reported_as_missing_article():
    """민사집행법 = 이 코퍼스가 애초에 안 담는 법령 — '조문 없음'처럼 말하면 안 된다."""
    d = _detail("민사집행법 제229조")
    assert d["error"] == "article_not_found"
    assert d["law_in_corpus"] is False
    assert d["law_name"] == "민사집행법"
    assert "민사집행법" in d["message"], "법령명이 지워졌다(원결함 재발)"
    assert "제229조" in d["message"]
    # 부존재 단정 금지 지침이 실려야 한다
    assert "존재하지 않는다는 뜻이 아닙니다" in d["message"]
    assert d["hint"] and "없다고 말하지 마라" in d["hint"]


def test_missing_article_in_known_law_says_law_exists():
    """국가계약법 시행령은 있으나 제9999조는 없다 — 이건 진짜 부존재다."""
    d = _detail("국가계약법 시행령 제9999조")
    assert d["error"] == "article_not_found"
    assert d["law_in_corpus"] is True
    assert d["law_name"] == "국가계약법 시행령"
    assert "국가계약법 시행령" in d["message"] and "제9999조" in d["message"]


def test_two_failure_kinds_are_distinguishable():
    """핵심 회귀: 두 상황이 같은 문장을 내면 안 된다."""
    outside = _detail("민사집행법 제229조")
    missing = _detail("국가계약법 시행령 제9999조")
    assert outside["message"] != missing["message"]
    assert outside["law_in_corpus"] != missing["law_in_corpus"]


def test_official_name_and_alias_resolve_to_corpus():
    """정식명·약칭 어느 쪽으로 불러도 '코퍼스에 있음'으로 판정된다."""
    assert law._law_in_corpus("국가계약법 시행령") is True
    assert law._law_in_corpus("국가를 당사자로 하는 계약에 관한 법률") is True
    assert law._law_in_corpus("민사집행법") is False
    assert law._law_in_corpus("") is False


def test_corpus_laws_listed_for_reformulation():
    d = _detail("민사집행법 제229조")
    assert "국가계약법 시행령" in d["corpus_laws"]
    assert "민사집행법" not in d["corpus_laws"]


def test_law_name_missing_is_its_own_error():
    """'제30조'만 주면 조문 부존재가 아니라 '법령명 미특정'이다."""
    d = _detail("제30조")
    assert d["error"] == "law_not_specified"
    assert d["law_in_corpus"] is False
    assert "국가계약법 시행령" in d["corpus_laws"]


def test_other_laws_with_same_article_are_surfaced():
    """제26조는 국가·지방 '시행령'에 있다 — 모법으로 물으면 단서로 준다."""
    d = _detail("국가계약법 제26조")
    assert d["law_in_corpus"] is True  # 국가계약법 자체는 코퍼스에 있다
    assert d["article_found_in_other_laws"] == ["국가계약법 시행령", "지방계약법 시행령"]


def test_mcp_relays_structured_error_verbatim():
    """MCP _friendly_error가 backend_error로 뭉개면 판단 근거가 사라진다."""
    sys.path.insert(0, str(ROOT / "mcp"))
    import httpx
    import server as mcp_server

    detail = _detail("민사집행법 제229조")
    request = httpx.Request("GET", "http://x/api/v1/law/article")
    response = httpx.Response(404, json={"detail": detail}, request=request)
    out = mcp_server._friendly_error(
        httpx.HTTPStatusError("404", request=request, response=response))
    assert out["error"] == "article_not_found"
    assert out["law_in_corpus"] is False
    assert out["hint"] == detail["hint"]
    assert "민사집행법" in out["message"]
    assert out["status"] == 404
