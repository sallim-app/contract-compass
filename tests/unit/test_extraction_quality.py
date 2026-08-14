"""추출 품질 게이트 — 깨진 PDF 추출본이 '근거'로 나가지 않게 한다 (T-2026W32-161).

대상 문서는 코퍼스 전수 스캔으로 특정했다(2026-08-14):
  · service_sw_guide_2025 — public_guides 56청크 **전부** 폰트 인코딩 손상(판독 불가) → 제외
  · general_감사원공공계   — 842청크 중 621건 제어문자(BEL)+2단 편집 행교차 → 세정 + 경고
법령(law_articles 8,953)·예규(admin_rules 1,345)는 오염 0건이라 핵심 근거는 온전하다.
"""
from backend.services.rag_service import (_DEGRADED_SOURCES, _EXCLUDED_SOURCES,
                                          _quality_gate)


def _chunk(did: str, content: str = "본문", section: str = "제목") -> dict:
    return {"chunk_id": did + section, "document_id": did,
            "content": content, "section_title": section}


def test_unreadable_document_is_excluded_and_disclosed():
    out = _quality_gate([_chunk("service_sw_guide_2025"), _chunk("admin_rules")])
    assert [c["document_id"] for c in out] == ["admin_rules"]
    # 뺐으면 뺐다고 말한다 — '자료가 없다'와 '판독 못 해 뺐다'는 다르다
    dropped = out[0]["_gate_dropped"]
    assert dropped[0]["source"] == "service_sw_guide_2025" and dropped[0]["chunks"] == 1
    assert dropped[0]["reason"]


def test_control_chars_are_stripped():
    dirty = "Q. \x07 시공사의 귀책사유로​ 기간이"
    out = _quality_gate([_chunk("admin_rules", dirty, "\x07제목")])
    assert "\x07" not in out[0]["content"] and "​" not in out[0]["content"]
    assert "\x07" not in out[0]["section_title"]
    assert out[0]["extraction_quality"] == "control_chars_cleaned"


def test_degraded_sources_are_disclosed_with_prefix_match():
    """손상 목록에 있는 문서는 컬렉션 접두어가 붙어도 경고가 실려야 한다.

    목록이 비어 있는 상태(전부 재추출 완료)도 정상이다 — 그때는 검사할 대상이 없다.
    """
    for did in _DEGRADED_SOURCES:
        for variant in (did, f"faq_{did}"):
            out = _quality_gate([_chunk(variant)])
            assert out[0]["extraction_quality"] == "two_column_pdf", variant
            assert out[0]["quality_warning"], variant


def test_reextracted_guide_has_no_control_chars_in_corpus():
    """재추출(T-2026W33-173)의 완료 판정 — 실제 코퍼스를 본다.

    감사원 「공공계약 실무가이드」는 열 인식 정렬로 재색인했으므로 제어문자가 0이어야 한다.
    다시 오염되면(추출기 회귀·재색인 실수) 여기서 잡힌다.
    """
    import chromadb

    from backend.config import get_settings
    from backend.services.rag_service import _CTRL_RE
    cl = chromadb.PersistentClient(get_settings().chroma_path)
    for name in ("public_guides", "faq"):
        docs = cl.get_collection(name).get(
            where={"document_id": "general_감사원공공계"}, include=["documents"])["documents"]
        assert docs, f"{name}: 감사원 가이드 청크가 없다(재색인 실패?)"
        dirty = [d for d in docs if _CTRL_RE.search(d or "")]
        assert not dirty, f"{name}: 제어문자 청크 {len(dirty)}건 — 추출 회귀"


def test_clean_sources_are_untouched():
    out = _quality_gate([_chunk("국가계약법 시행령", "① 각 중앙관서의 장은")])
    assert "extraction_quality" not in out[0] and "quality_warning" not in out[0]


def test_gate_tables_are_documented():
    """게이트 목록은 '왜 뺐는지'가 값으로 남아야 원복 판단이 가능하다."""
    assert all(v for v in _EXCLUDED_SOURCES.values())
    assert all(v for v in _DEGRADED_SOURCES.values())
