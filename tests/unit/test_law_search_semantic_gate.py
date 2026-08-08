"""법령 검색 시맨틱 폴백 정직성 단위 테스트 (T-2026W32-184, 2026-08-09).

무의미 질의('존재하지않는법률용어_9f7c2a')에 시맨틱 폴백이 최근접 '삭제 <날짜>'
스텁 8건을 근거처럼 반환하던 결함의 회귀 — (a) 삭제 스텁 후보 제외,
(b) 거리 하한(_SEMANTIC_MAX_DIST) 미달 시 0건 실토, (c) 삭제 조문 note 표시.
chroma 미사용(fake col). 거리값은 2026-08-09 실측 분포를 재현한다.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import backend.api.v1.law as law  # noqa: E402

pytestmark = pytest.mark.unit

STUB_DOC = "공유재산법 시행령 제64조\n제64조 삭제 <2010.8.4>"
STUB_META = {"law_name": "공유재산법 시행령", "article_titles": "제64조",
             "law_ref": "공유재산법 시행령 제64조"}
STUB2_DOC = "전자조달법 제14조 제2항\n② ② 삭제 <2018.12.31>"
STUB2_META = {"law_name": "전자조달법", "article_titles": "제14조",
              "law_ref": "전자조달법 제14조 제2항"}
GOOD_DOC = "국가계약법 시행령 제26조\n수의계약에 의할 수 있는 사유는 다음과 같다."
GOOD_META = {"law_name": "국가계약법 시행령", "article_titles": "제26조",
             "law_ref": "국가계약법 시행령 제26조"}


def test_deleted_note_detects_observed_stub_variants():
    # 코퍼스 실측 변형: "제64조 삭제", "② ② 삭제", "제92조의8 삭제"
    assert law._deleted_note(STUB_DOC)
    assert law._deleted_note(STUB2_DOC)
    assert law._deleted_note("지방계약법 시행령 제92조의8\n제92조의8 삭제 <2024.2.13>")
    # 정상 조문·'삭제'가 본문 일부인 조문은 스텁이 아니다
    assert law._deleted_note(GOOD_DOC) is None
    assert law._deleted_note("어떤 법 제1조\n제1조 계약을 삭제 <2020.1.1> 이후 절차에 따라 정리한다.") is None


class _SemanticCol:
    """substring 전부 0건 → 시맨틱 폴백만 응답하는 가짜 컬렉션."""

    def __init__(self, triples):  # [(doc, meta, dist), ...] 거리 오름차순
        self.triples = triples

    def get(self, where=None, where_document=None, include=None, limit=None):
        return {"documents": [], "metadatas": []}

    def query(self, query_texts=None, n_results=None, include=None):
        t = self.triples[:n_results]
        return {"documents": [[d for d, _, _ in t]],
                "metadatas": [[m for _, m, _ in t]],
                "distances": [[x for _, _, x in t]]}


def test_nonsense_query_returns_zero_not_deleted_stubs(monkeypatch):
    # 무의미 질의 재현: 최근접이 전부 삭제 스텁(dist 1.01+) — 전부 걸러져 0건
    col = _SemanticCol([(STUB_DOC, STUB_META, 1.01), (STUB2_DOC, STUB2_META, 1.02)])
    monkeypatch.setattr(law, "_get_collection", lambda: col)
    assert law.search_law(q="존재하지않는법률용어_9f7c2a") == []


def test_semantic_hit_within_floor_survives_and_is_labeled(monkeypatch):
    # 관련 질의 분포(0.70~0.81) 안의 비스텁 문서는 살아남고 matched_by로 공시
    col = _SemanticCol([(STUB_DOC, STUB_META, 0.65), (GOOD_DOC, GOOD_META, 0.75)])
    monkeypatch.setattr(law, "_get_collection", lambda: col)
    hits = law.search_law(q="계약상대자를 정하는 방식")
    assert [h.law_ref for h in hits] == ["국가계약법 시행령 제26조"]
    assert hits[0].matched_by == "semantic"
    assert hits[0].note is None


def test_distance_floor_cuts_offdomain(monkeypatch):
    # 역외 질의 분포(0.92+)는 비스텁이라도 하한 미달 — 0건 실토
    col = _SemanticCol([(GOOD_DOC, GOOD_META, 0.92)])
    monkeypatch.setattr(law, "_get_collection", lambda: col)
    assert law.search_law(q="블록체인 스마트컨트랙트 가스비") == []


class _SubstringStubCol:
    """substring 매치로 삭제 스텁이 걸리는 가짜 컬렉션 (조문번호 검색 경로)."""

    def get(self, where=None, where_document=None, include=None, limit=None):
        if where and where.get("article_titles") == "제64조":
            return {"documents": [STUB_DOC], "metadatas": [STUB_META]}
        return {"documents": [], "metadatas": []}


def test_article_lookup_marks_deleted(monkeypatch):
    # 조문번호로 정당하게 찾은 삭제 조문도 note로 삭제 사실을 공시
    monkeypatch.setattr(law, "_get_collection", lambda: _SubstringStubCol())
    hits = law.search_law(q="공유재산법 시행령 제64조")
    assert len(hits) == 1
    assert "삭제" in (hits[0].note or "")
