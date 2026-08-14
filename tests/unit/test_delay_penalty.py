"""지체상금·지연배상금 산정 회귀 — 순수 계산 계층(LLM·네트워크 무소모).

기대값은 전부 2026-08-14 라이브 코퍼스에서 읽은 조문 원문으로 확정했다
(국가계약법 시행규칙 제75조·시행령 제74조 / 지방계약법 시행규칙 제75조·시행령 제90조).
설계·근거표는 docs/DELAY-PENALTY-AXIS.md.

MCP 도구 계층의 같은 성질은 tools/mcp_regression.py R32~R38이 검사한다 — 이쪽은
계산 자체를, 그쪽은 도구 응답 계약(필드·실토)을 본다.
"""
import pytest

from backend.services.delay_penalty import DelayPenaltyInputError, compute


def test_national_construction_rate():
    """국가 공사 = 1천분의 0.5. 10억 × 30일 = 1,500만원."""
    r = compute(contract_kind="construction", org_type="national",
                contract_amount=1_000_000_000, delay_days=30)
    assert r["rate"]["value"] == 0.0005
    assert r["amount"] == 15_000_000
    assert r["term"] == "지체상금"
    assert r["cap"]["applied"] is False


def test_local_service_rate_differs_from_national():
    """지방 용역은 1000분의 1.3 — 국가(1.25)와 다르다.

    이 저장소에서 두 번 재발한 '지방 판정에 국가 수치 혼입'(R15·R16) 계열이
    이 축에서 되풀이되지 않게 두 값을 한 테스트에서 대조한다.
    """
    local = compute(contract_kind="service", org_type="local",
                    contract_amount=100_000_000, delay_days=30)
    national = compute(contract_kind="service", org_type="national",
                       contract_amount=100_000_000, delay_days=30)
    assert local["rate"]["value"] == 0.0013
    assert national["rate"]["value"] == 0.00125
    assert local["amount"] == 3_900_000
    assert national["amount"] == 3_750_000
    assert local["amount"] != national["amount"]
    # 법정 용어도 다르다 — 지방 계약서엔 '지체상금'이라는 말이 없다
    assert local["term"] == "지연배상금"
    assert any("지연배상금" in w for w in local["warnings"])


def test_cap_30_percent_is_disclosed_not_silent():
    """한도로 깎였으면 원금액과 함께 말해야 한다(조용한 clamp 금지)."""
    r = compute(contract_kind="construction", org_type="national",
                contract_amount=100_000_000, delay_days=700)
    assert r["amount_raw"] == 35_000_000
    assert r["cap"]["applied"] is True
    assert r["amount"] == 30_000_000
    assert any("한도" in w and "35,000,000" in w for w in r["warnings"])


def test_accepted_portion_deducted_from_base():
    """기성·기납 인수분은 기준금액에서 공제(영 제74조②). (10억−4억)×0.0005×30일."""
    r = compute(contract_kind="construction", org_type="national",
                contract_amount=1_000_000_000, delay_days=40, excluded_days=10,
                accepted_portion_amount=400_000_000)
    assert r["base_amount"]["result"] == 600_000_000
    assert r["counted_days"]["result"] == 30
    assert r["amount"] == 9_000_000


def test_undeclared_inputs_warn_instead_of_silently_zero():
    """인수분·면책일수 미선언은 0으로 계산하되 경고로 실토한다."""
    r = compute(contract_kind="construction", org_type="national",
                contract_amount=100_000_000, delay_days=10)
    assert any("인수분" in w for w in r["warnings"])
    assert any("면책일수" in w for w in r["warnings"])
    # 지체일수는 우리가 정하지 않는다는 사실이 응답에 있어야 한다
    assert "사실 판단" in r["counted_days"]["disclaimer"]


def test_design_build_exception_lowers_product_rate():
    """설계·제조 일괄 + 발주기관 승인 물품은 0.75가 아니라 0.5(규칙 제75조 제2호 단서)."""
    plain = compute(contract_kind="product_manufacture", org_type="national",
                    contract_amount=100_000_000, delay_days=10)
    exc = compute(contract_kind="product_manufacture", org_type="national",
                  contract_amount=100_000_000, delay_days=10, design_build_approved=True)
    assert plain["rate"]["value"] == 0.00075
    assert exc["rate"]["value"] == 0.0005


def test_public_corp_uses_national_profile_and_says_so():
    r = compute(contract_kind="construction", org_type="public_corp",
                contract_amount=100_000_000, delay_days=10)
    assert r["profile_applied"] == "national"
    assert any("공기업" in w for w in r["warnings"])


def test_inferred_rate_is_disclosed():
    """법문에 없는 값(해석으로 채운 요율)은 inferred로 실토한다.

    지방 규칙 제75조엔 군용 음·식료품 호가 없어 '물품의 제조·구매'로 해석했다 —
    해석을 법문인 척 내놓으면 그것이 이 저장소가 금지하는 은폐다.
    """
    r = compute(contract_kind="military_food", org_type="local",
                contract_amount=100_000_000, delay_days=10)
    assert r["rate"]["inferred"] is True
    assert any("해석" in w for w in r["warnings"])


@pytest.mark.parametrize("kwargs,code", [
    ({"org_type": "unknown_org"}, "unknown_org_type"),
    ({"contract_kind": "무엇"}, "unknown_contract_kind"),
    ({"contract_amount": 0}, "invalid_contract_amount"),
    ({"delay_days": 5, "excluded_days": 9}, "excluded_exceeds_delay"),
    ({"accepted_portion_amount": 10_000_000_000}, "accepted_exceeds_contract"),
])
def test_rejects_instead_of_guessing(kwargs, code):
    """모르는 입력에 기본값을 채워 계산하지 않는다 — 거부하고 무엇이 필요한지 알린다."""
    base = dict(contract_kind="construction", org_type="national",
                contract_amount=100_000_000, delay_days=10)
    base.update(kwargs)
    with pytest.raises(DelayPenaltyInputError) as e:
        compute(**base)
    assert e.value.code == code
    assert e.value.hint  # 행동지침 없는 거부는 막다른 길이다


def test_no_hardcoded_rates_in_service_module():
    """요율의 진실원은 rules/delay_penalty_rules.json이다(CLAUDE.md 규칙).

    서비스 모듈에 요율 리터럴이 다시 박히면 개정 시 두 곳이 갈린다.
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "backend" / "services"
           / "delay_penalty.py").read_text(encoding="utf-8")
    for literal in ("0.0005", "0.00075", "0.0013", "0.00125", "0.0025", "0.3"):
        assert literal not in src, f"요율·한도 리터럴 {literal}이 코드에 박혔다 — rules로 옮겨라"
