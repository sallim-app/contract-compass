"""면책 사유 지도(Phase 2) 회귀 — 지도가 판정표로 변질되지 않게 지킨다.

이 층의 위험은 계산 오류가 아니라 **월권**이다. 도구가 "이건 면책된다"고 말하기
시작하면, 일반조건이 발주기관의 인정을 요건으로 둔 판단을 우리가 대신한 것이 된다.
그래서 검사도 값이 아니라 계약(판정 거부·확인할 사실·출처·끊긴 인용 표시)을 본다.
"""
import json
from pathlib import Path

import pytest

from backend.services.delay_exemption import (RULES_PATH, DelayExemptionInputError,
                                              ground_ids, guide)

MAP = json.loads(Path(RULES_PATH).read_text(encoding="utf-8"))


def test_every_ground_has_basis_and_questions():
    """근거 없는 사유, 확인할 사실 없는 사유는 실무에서 쓸 수 없다."""
    for g in MAP["grounds"]:
        assert g["basis"], f"{g['id']}: 근거 조문 없음"
        assert g["must_establish"], f"{g['id']}: 확정해야 할 사실 없음"
        assert g["quote"], f"{g['id']}: 원문 인용 없음"


def test_refusal_contract_is_always_present():
    for kind in MAP["kind_to_family"]:
        d = guide(contract_kind=kind)
        assert "판정하지 않는다" in d["who_decides"]["tool_contract"]
        assert d["general_conditions_warning"]


def test_family_filtering():
    """공사 전용 사유가 용역 응답에 섞이면 안 된다(계열 혼입은 이 저장소의 재발 결함)."""
    svc = {g["id"] for g in guide(contract_kind="service")["grounds"]}
    cst = {g["id"] for g in guide(contract_kind="construction")["grounds"]}
    assert "design_change" in cst and "design_change" not in svc
    assert "sw_requirement_change" in svc and "sw_requirement_change" not in cst


def test_sw_half_rule_is_explicit():
    """용역 SW 사유는 전액이 아니라 1/2만 불산입 — 놓치면 계산이 두 배 틀린다."""
    g = guide(contract_kind="service", ground="sw_requirement_change")["grounds"][0]
    assert "1/2" in g["partial"]
    assert "excluded_days" in (g.get("warning") or "")


def test_truncated_quotes_are_flagged():
    """회수 인용이 끊긴 항목은 끊겼다고 말한다 — 이어 붙이지 않는다."""
    flagged = [g for g in MAP["grounds"] if g.get("quote_truncated")]
    assert flagged, "끊긴 인용이 하나도 없다면 표시 규약이 사라진 것 — 확인 필요"
    for g in flagged:
        assert g["quote"].endswith("…") or "…" in g["quote"]
        assert any("확인" in q for q in g["must_establish"])


def test_precedents_carry_source():
    """선례는 출처(회신 문서번호·일자) 없이 실리면 인용할 수 없다."""
    for p in MAP["precedents"]:
        assert p["source"] and any(ch.isdigit() for ch in p["source"])


def test_day_count_rules_cover_known_traps():
    ids = {r["id"] for r in MAP["day_count_rules"]}
    assert {"inspection_period", "final_contract_amount", "split_delivery",
            "long_term_annual"} <= ids


@pytest.mark.parametrize("kwargs,code", [
    ({"contract_kind": "무엇"}, "unknown_contract_kind"),
    ({"contract_kind": "service", "ground": "design_change"}, "ground_not_applicable"),
    ({"contract_kind": "service", "ground": "없는사유"}, "unknown_ground"),
])
def test_rejects_with_actionable_hint(kwargs, code):
    with pytest.raises(DelayExemptionInputError) as e:
        guide(**kwargs)
    assert e.value.code == code
    assert e.value.hint


def test_ground_ids_match_tool_literal():
    """MCP 도구의 Literal 목록과 룰 파일이 갈리면 스키마가 거짓말을 한다."""
    src = (Path(__file__).resolve().parents[2] / "mcp" / "server.py").read_text(encoding="utf-8")
    for gid in ground_ids():
        assert f'"{gid}"' in src, f"{gid}가 도구 Literal에 없다"


def test_precedents_are_scoped_to_family():
    """공사 전용 회신을 용역 질의에 얹지 않는다(2026-08-14 codex 탐침).

    전 계열 공통인 '최종 확정 계약금액 기준'만 남고, 동절기 공사중지·장기계속공사 하자
    회신은 공사에서만 나와야 한다.
    """
    svc = {p["id"] for p in guide(contract_kind="service")["precedents"]}
    cst = {p["id"] for p in guide(contract_kind="construction")["precedents"]}
    assert svc == {"final_amount_basis"}
    assert {"winter_suspension", "prior_phase_defect"} <= cst
    for pr in MAP["precedents"]:
        assert pr.get("applies_to"), f"{pr['id']}: 적용 계열 태그 없음"
