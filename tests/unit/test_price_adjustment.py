"""물가변동 계약금액 조정(Phase 3) 회귀 — 계산 계층.

기대값은 2026-08-14 라이브 코퍼스에서 읽은 조문으로 확정했다(국가계약법 시행령 제64조·
시행규칙 제74조 / 지방계약법 시행령 제73조). 이 축에서 우리가 특히 지켜야 하는 것은
**모르는 것을 모른다고 말하는 것**이다 — 조정률은 우리가 산정하지 못한다.
"""
import json
from pathlib import Path

import pytest

from backend.services.price_adjustment import (RULES_PATH, PriceAdjustmentInputError,
                                               check)

BASE = dict(org_type="national", contract_date="2026-01-01", check_date="2026-06-01")


def test_90day_boundary_is_inclusive():
    """'90일 이상'이므로 정확히 90일 경과한 날도 요건을 채운다(경계 포함)."""
    d = check(**{**BASE, "check_date": "2026-04-01", "adjustment_rate_pct": 4.0})
    assert d["period"]["elapsed_days"] == 90
    assert d["period"]["met"] is True
    d89 = check(**{**BASE, "check_date": "2026-03-31", "adjustment_rate_pct": 4.0})
    assert d89["period"]["elapsed_days"] == 89 and d89["period"]["met"] is False


def test_last_adjustment_date_becomes_new_basis():
    """2차 이후 조정은 직전 조정기준일부터 90일 — 계약체결일이 아니다."""
    d = check(**{**BASE, "last_adjustment_date": "2026-05-01", "adjustment_rate_pct": 4.0})
    assert d["period"]["basis_date"] == "2026-05-01"
    assert d["period"]["elapsed_days"] == 31 and d["period"]["met"] is False


def test_rate_threshold_absolute_value():
    """감액(음수) 조정도 대상이다 — 절대값으로 문턱을 본다."""
    up = check(**{**BASE, "adjustment_rate_pct": 3.0})
    down = check(**{**BASE, "adjustment_rate_pct": -3.5})
    assert up["rate"]["met"] is True
    assert down["rate"]["met"] is True and down["rate"]["direction"] == "감액"
    assert check(**{**BASE, "adjustment_rate_pct": 2.9})["rate"]["met"] is False


def test_single_item_threshold_differs_by_org():
    """단품 조정 문턱: 국가 15% / 지방 10%. 같은 12%가 정반대 결과가 된다."""
    args = dict(contract_date="2026-01-01", check_date="2026-06-01", is_construction=True,
                single_item_rate_pct=12, single_item_share_over_5permille=True)
    nat = check(org_type="national", **args)
    loc = check(org_type="local", **args)
    assert nat["single_item"]["threshold_pct"] == 15.0 and nat["single_item"]["met"] is False
    assert loc["single_item"]["threshold_pct"] == 10.0 and loc["single_item"]["met"] is True
    assert "국가" in nat["single_item"]["cross_profile_warning"]


def test_single_item_requires_construction():
    d = check(**{**BASE, "is_construction": False, "single_item_rate_pct": 30,
                 "single_item_share_over_5permille": True})
    assert d["single_item"]["met"] is False
    assert "공사계약" in d["single_item"]["not_construction"]


def test_unknown_rate_is_undetermined_not_denied():
    """조정률을 모르면 '요건 미충족'이 아니라 '판정 불가'다 — 못 봄 ≠ 없음."""
    d = check(**BASE)
    assert d["verdict"] == "undetermined"
    assert d["rate"]["met"] is None and d["rate"]["why_unknown"]
    assert any("산정" in c for c in d["cannot_do"])


def test_amount_and_advance_deduction_formula():
    """조정금액 = 적용대가 × 조정률, 공제 = 그 값 × 선금급률(시행규칙 제74조 제5·6항)."""
    d = check(**{**BASE, "adjustment_rate_pct": 4.2,
                 "adjustment_base_amount": 1_000_000_000, "advance_payment_ratio": 0.3})
    assert d["computed"]["adjustment_amount"] == 42_000_000
    assert d["computed"]["advance_deduction"]["amount"] == 12_600_000
    assert d["computed"]["net_amount"] == 29_400_000


def test_no_advance_ratio_warns_instead_of_zero():
    d = check(**{**BASE, "adjustment_rate_pct": 4.2, "adjustment_base_amount": 100_000_000})
    assert "advance_deduction" not in d["computed"]
    assert "선금급률" in d["computed"]["advance_deduction_note"]


def test_method_default_is_disclosed_as_assumption():
    """계약서 명시를 모르면 품목조정률이 기본 — 다만 그것이 추정임을 밝힌다."""
    d = check(**{**BASE, "adjustment_rate_pct": 4.0})
    assert d["method"]["applied"] == "item" and "확인" in d["method"]["assumption"]
    d2 = check(**{**BASE, "adjustment_rate_pct": 4.0, "method_specified_in_contract": "index"})
    assert d2["method"]["applied"] == "index" and "assumption" not in d2["method"]


def test_urgent_exception_names_the_decider():
    """예외는 자동 통과가 아니다 — 인정 주체(발주기관)를 함께 말한다."""
    d = check(**{**BASE, "check_date": "2026-02-01", "adjustment_rate_pct": 4.0,
                 "urgent_exception": True})
    assert d["verdict"] == "exception_path"
    assert "발주기관" in d["period"]["exception"]["caution"]


@pytest.mark.parametrize("kwargs,code", [
    ({"org_type": "무엇"}, "unknown_org_type"),
    ({"contract_date": "2026/01/01"}, "invalid_date"),
    ({"check_date": "2025-12-01"}, "check_before_contract"),
    ({"method_specified_in_contract": "무엇"}, "unknown_method"),
    ({"adjustment_rate_pct": 4.0, "adjustment_base_amount": 100, "advance_payment_ratio": 30},
     "invalid_advance_ratio"),
])
def test_rejects_with_hint(kwargs, code):
    with pytest.raises(PriceAdjustmentInputError) as e:
        check(**{**BASE, **kwargs})
    assert e.value.code == code and e.value.hint


def test_no_thresholds_hardcoded_in_service():
    """문턱의 진실원은 rules/price_adjustment_rules.json이다(CLAUDE.md 규칙)."""
    src = (Path(__file__).resolve().parents[2] / "backend" / "services"
           / "price_adjustment.py").read_text(encoding="utf-8")
    for literal in ("15.0", "10.0", "3.0", "90"):
        assert f"= {literal}" not in src, f"문턱 {literal}이 코드에 박혔다 — rules로 옮겨라"
    rules = json.loads(Path(RULES_PATH).read_text(encoding="utf-8"))
    assert rules["profiles"]["national"]["single_item_threshold_pct"] == 15.0
    assert rules["profiles"]["local"]["single_item_threshold_pct"] == 10.0
