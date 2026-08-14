"""지체상금(국가·공기업)·지연배상금(지방) 산정 — 순수 계산, 외부 의존 0.

설계·근거는 `docs/DELAY-PENALTY-AXIS.md`. 요율·한도·근거조문의 진실원은
`rules/delay_penalty_rules.json`이며 **이 파일에 수치를 박지 않는다**(CLAUDE.md 규칙).

이 모듈이 지키는 인식 경계 계약 — 이 축에서 도구가 거짓말할 수 있는 자리다:

1. **지체일수를 아는 척하지 않는다.** 준공검사 소요기간·면책 사유는 사실 판단이라
   계산기가 못 한다. 입력은 '선언값'으로 표시해 돌려주고, 무엇을 확정해야 하는지 알린다.
2. **기관유형을 추측하지 않는다.** 국가와 지방은 요율이 다르다(물품 0.75↔0.8,
   용역 1.25↔1.3). 모르면 계산을 거부한다 — 조용한 국가 폴백은 지자체 사용자에게
   틀린 금액을 준다.
3. **30% 한도를 조용히 적용하지 않는다.** clamp 했으면 원금액과 함께 말한다.
4. **인수분 미선언은 0이 아니라 경고다.** 전액 기준으로 계산하되 공제 대상이 있으면
   재계산해야 한다고 알린다.
5. **법문에 없는 값(해석으로 채운 요율)은 inferred로 실토한다.**
6. **용어 비대칭을 병기한다.** 지방 계약서엔 '지체상금'이라는 말이 없다(지연배상금).
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

RULES_PATH = Path(__file__).resolve().parents[2] / "rules" / "delay_penalty_rules.json"

# 계약유형 키 — rules의 rates 키와 1:1. 도구 스키마(Literal)도 이 목록을 쓴다.
CONTRACT_KINDS = (
    "construction",
    "product_manufacture",
    "product_repair",
    "service",
    "military_food",
    "transport_storage",
)
ORG_TYPES = ("national", "local", "public_corp")


class DelayPenaltyInputError(ValueError):
    """계산을 거부해야 하는 입력 — 추측으로 채우지 않고 무엇이 필요한지 알린다."""

    def __init__(self, code: str, message: str, hint: str):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


@lru_cache(maxsize=1)
def _rules() -> dict:
    with open(RULES_PATH, encoding="utf-8") as f:
        return json.load(f)


def _profile(org_type: str) -> tuple[str, dict]:
    rules = _rules()
    key = rules.get("profile_aliases", {}).get(org_type, org_type)
    prof = rules["profiles"].get(key)
    if not prof:
        raise DelayPenaltyInputError(
            "unknown_org_type",
            f"기관유형 '{org_type}'을 모른다. 가능한 값: {', '.join(ORG_TYPES)}",
            "발주기관이 국가기관인지 지방자치단체인지 공기업·준정부기관인지 사용자에게 확인하라 — "
            "국가와 지방은 요율이 달라 추측하면 틀린 금액이 된다.")
    return key, prof


def compute(
    *,
    contract_kind: str,
    org_type: str,
    contract_amount: int,
    delay_days: int,
    excluded_days: int = 0,
    accepted_portion_amount: int = 0,
    design_build_approved: bool = False,
) -> dict[str, Any]:
    """지체상금/지연배상금 산정 결과를 실토 필드까지 포함해 반환.

    금액은 원 단위 정수, 절사 없이 내림(원 미만은 실무상 원 단위로 정리).
    """
    if contract_kind not in CONTRACT_KINDS:
        raise DelayPenaltyInputError(
            "unknown_contract_kind",
            f"계약유형 '{contract_kind}'을 모른다. 가능한 값: {', '.join(CONTRACT_KINDS)}",
            "요율은 계약유형별로 다르다 — 공사/물품 제조·구매/물품 수리·가공·대여/용역/"
            "군용 음식료품/운송·보관 중 무엇인지 확인하라.")
    profile_key, prof = _profile(org_type)

    if contract_amount <= 0:
        raise DelayPenaltyInputError(
            "invalid_contract_amount", "계약금액은 1원 이상이어야 한다.",
            "장기계속계약이면 총액이 아니라 **연차별 계약금액**을 넣어라.")
    if delay_days < 0 or excluded_days < 0 or accepted_portion_amount < 0:
        raise DelayPenaltyInputError(
            "negative_value", "지체일수·면책일수·인수금액은 음수일 수 없다.",
            "입력값을 확인하라.")
    if accepted_portion_amount >= contract_amount:
        raise DelayPenaltyInputError(
            "accepted_exceeds_contract",
            f"인수분({accepted_portion_amount:,}원)이 계약금액({contract_amount:,}원) 이상이다.",
            "인수분은 계약금액에서 공제할 부분이라 계약금액보다 작아야 한다 — "
            "전부 인수했다면 지체 대상 금액이 없다.")
    if excluded_days > delay_days:
        raise DelayPenaltyInputError(
            "excluded_exceeds_delay",
            f"면책일수({excluded_days}일)가 지체일수({delay_days}일)보다 많다.",
            "지체일수는 면책일수를 포함한 총 지체일수를 넣어라 — 이 계산기가 둘을 뺀다.")

    rate = dict(prof["rates"][contract_kind])
    exc = prof.get("design_build_exception") or {}
    if design_build_approved and contract_kind in (exc.get("applies_to") or []):
        rate = {"value": exc["value"], "as_text": exc["as_text"], "ho": exc["basis"],
                "label": exc["condition"]}
    rate["basis"] = f"{prof['rate_article']} {rate.get('ho', '')}".strip()

    base_amount = contract_amount - accepted_portion_amount
    counted_days = delay_days - excluded_days
    amount_raw = int(base_amount * rate["value"] * counted_days)
    cap_amount = int(base_amount * prof["cap_rate"])
    capped = amount_raw > cap_amount
    amount = cap_amount if capped else amount_raw

    warnings: list[str] = []
    if rate.get("inferred"):
        warnings.append(f"⚠ 이 요율은 법문에 명시된 값이 아니라 해석이다 — {rate.get('inference_note')}")
    if capped:
        warnings.append(
            f"한도 적용: 산식대로면 {amount_raw:,}원이지만 {prof['cap_article']}의 "
            f"{int(prof['cap_rate'] * 100)}% 한도로 {amount:,}원이 된다.")
    if accepted_portion_amount == 0:
        warnings.append(
            "기성·기납 인수분을 0으로 계산했다(미선언). 검사를 거쳐 인수한 부분이 있으면 "
            "그 금액을 공제한 뒤 다시 계산해야 한다 — "
            f"{_rules()['deductions']['accepted_portion_article_' + profile_key]}.")
    if excluded_days == 0:
        warnings.append(
            "면책일수를 0으로 계산했다(미선언). 발주기관 귀책·천재지변 등 계약상대자의 책임 없는 "
            "사유로 지체된 일수는 지체일수에서 빠진다 — 해당 여부는 사실 판단이라 이 계산기가 "
            "정하지 않는다.")
    if profile_key == "local" and org_type == "local":
        warnings.append(
            f"지방계약에서 법정 용어는 '{prof['term']}'이다 — 계약서에 '지체상금'이라는 말이 "
            "없어도 없는 제도가 아니다.")
    if org_type == "public_corp":
        warnings.append(
            "공기업·준정부기관은 계약사무규칙이 국가계약법령을 준용해 국가 요율로 계산했다. "
            "기관 자체 계약규정·계약서 특약이 다르게 정할 수 있으니 확인하라.")

    return {
        "term": prof["term"],
        "counterpart_term": prof["counterpart_term"],
        "org_type": org_type,
        "profile_applied": profile_key,
        "contract_kind": contract_kind,
        "rate": rate,
        "base_amount": {
            "contract_amount": contract_amount,
            "accepted_portion_deducted": accepted_portion_amount,
            "result": base_amount,
            "basis": _rules()["deductions"][f"accepted_portion_article_{profile_key}"],
            "note": _rules()["long_term_contract_note"],
        },
        "counted_days": {
            "declared_delay_days": delay_days,
            "declared_excluded_days": excluded_days,
            "result": counted_days,
            "basis": _rules()["deductions"][f"excluded_days_article_{profile_key}"],
            # 이 축에서 가장 중요한 실토 — 우리는 계산만 하고 확정하지 않는다.
            "disclaimer": "지체일수·면책일수는 사용자가 선언한 값이다. 준공검사 소요기간·"
                          "검사 불합격 재검사 기간·분할납품의 납기 분리 등 지체일수 산정 자체는 "
                          "사실 판단이라 이 도구가 정하지 않는다.",
        },
        "amount_raw": amount_raw,
        "cap": {
            "limit_rate": prof["cap_rate"],
            "limit_amount": cap_amount,
            "applied": capped,
            "basis": prof["cap_article"],
        },
        "amount": amount,
        "formula": (f"{base_amount:,}원 × {rate['as_text']} × {counted_days}일"
                    + (f" → 한도 {int(prof['cap_rate'] * 100)}% 적용" if capped else "")),
        "legal_basis": [prof["act_article"], prof["base_article"], prof["rate_article"]],
        "joint_contract_rule": prof.get("joint_contract_rule"),
        "warnings": warnings,
        "uncertainties": _rules()["uncertainties"],
        "rules_updated": _rules()["last_updated"],
        "rate_article_amended": prof["rate_article_amended"],
    }
