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


def test_two_column_source_carries_warning_in_every_collection():
    """같은 PDF가 컬렉션마다 접두어를 달고 들어온다 — 접두어 때문에 경고가 빠지면 안 된다."""
    for did in ("general_감사원공공계", "faq_general_감사원공공계"):
        out = _quality_gate([_chunk(did)])
        assert out[0]["extraction_quality"] == "two_column_pdf", did
        assert "2단" in out[0]["quality_warning"], did


def test_clean_sources_are_untouched():
    out = _quality_gate([_chunk("국가계약법 시행령", "① 각 중앙관서의 장은")])
    assert "extraction_quality" not in out[0] and "quality_warning" not in out[0]


def test_gate_tables_are_documented():
    """게이트 목록은 '왜 뺐는지'가 값으로 남아야 원복 판단이 가능하다."""
    assert all(v for v in _EXCLUDED_SOURCES.values())
    assert all(v for v in _DEGRADED_SOURCES.values())
