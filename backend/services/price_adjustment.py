"""물가변동으로 인한 계약금액 조정 — 이행단계 축 Phase 3. 순수 계산, 외부 의존 0.

설계 갈래(왜 이 모양인가): 이 축은 **결정론인 부분과 아닌 부분이 섞여 있다.**
  - 결정론: 90일 경과·3% 문턱·단품 문턱(국가 15% / 지방 10%)·조정 방식 결정 규칙·
    조정금액과 선금 공제 산식.
  - 결정론 아님: **조정률 그 자체**(품목·지수 — 지수·단가 원천을 우리가 안 가졌다),
    조정기준일 확정, 물가변동적용대가 산출.
그래서 이 모듈은 앞의 것만 계산하고, 뒤의 것은 입력으로 받아 '선언값'으로 표시하며,
못 하는 이유를 cannot_do로 매번 실토한다(기치 ② — 못 봄 ≠ 없음).

수치·근거의 진실원은 `rules/price_adjustment_rules.json`이다(코드에 문턱을 박지 않는다).
"""
from __future__ import annotations

import json
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

RULES_PATH = Path(__file__).resolve().parents[2] / "rules" / "price_adjustment_rules.json"

ORG_TYPES = ("national", "local", "public_corp")
METHODS = ("item", "index")


class PriceAdjustmentInputError(ValueError):
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
    r = _rules()
    key = r["profile_aliases"].get(org_type, org_type)
    prof = r["profiles"].get(key)
    if not prof:
        raise PriceAdjustmentInputError(
            "unknown_org_type",
            f"기관유형 '{org_type}'을 모른다. 가능한 값: {', '.join(ORG_TYPES)}",
            "국가/지방/공기업 중 무엇인지 사용자에게 확인하라 — **단품 조정 문턱이 국가 15%, "
            "지방 10%로 달라** 추측하면 판정이 뒤집힌다.")
    return key, prof


def _parse_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as e:
        raise PriceAdjustmentInputError(
            "invalid_date", f"{field}='{value}'을(를) 날짜로 읽지 못했다.",
            "YYYY-MM-DD 형식으로 넣어라(예: 2026-03-15).") from e


def check(
    *,
    org_type: str,
    contract_date: str,
    check_date: str,
    last_adjustment_date: str | None = None,
    adjustment_rate_pct: float | None = None,
    method_specified_in_contract: str | None = None,
    urgent_exception: bool = False,
    single_item_rate_pct: float | None = None,
    single_item_share_over_5permille: bool | None = None,
    is_construction: bool = False,
    adjustment_base_amount: int | None = None,
    advance_payment_ratio: float | None = None,
) -> dict[str, Any]:
    """요건 판정 + (값이 주어지면) 조정금액·선금공제 산식 적용."""
    r = _rules()
    profile_key, prof = _profile(org_type)
    if method_specified_in_contract not in (None, *METHODS):
        raise PriceAdjustmentInputError(
            "unknown_method",
            f"조정 방식 '{method_specified_in_contract}'을 모른다. 가능한 값: item, index",
            "계약서에 '지수조정률'이 명시돼 있으면 index, 아니면 item(또는 미상이면 생략)이다.")

    d_contract = _parse_date(contract_date, "contract_date")
    d_check = _parse_date(check_date, "check_date")
    if d_check < d_contract:
        raise PriceAdjustmentInputError(
            "check_before_contract",
            f"검토일({check_date})이 계약체결일({contract_date})보다 빠르다.",
            "장기계속계약이면 **제1차계약 체결일**을 contract_date에 넣어라.")

    # ── 요건 ①: 기간 ──────────────────────────────────────────────
    base_label, d_base = "계약체결일", d_contract
    if last_adjustment_date:
        d_last = _parse_date(last_adjustment_date, "last_adjustment_date")
        if d_last > d_check:
            raise PriceAdjustmentInputError(
                "adjustment_after_check",
                f"직전 조정기준일({last_adjustment_date})이 검토일({check_date})보다 늦다.",
                "직전 조정이 없었다면 last_adjustment_date를 비워라.")
        base_label, d_base = "직전 조정기준일", d_last
    elapsed = (d_check - d_base).days
    need = prof["elapsed_days_min"]
    period_ok = elapsed >= need
    period = {
        "basis_date_label": base_label,
        "basis_date": d_base.isoformat(),
        "elapsed_days": elapsed,
        "required_days": need,
        "met": period_ok,
        "boundary_note": f"{need}일 '이상'이므로 정확히 {need}일 경과한 날도 요건을 채운다.",
        "legal_basis": prof["elapsed_basis"] if base_label == "계약체결일" else prof["recheck_basis"],
    }
    if not period_ok and urgent_exception:
        period["exception"] = {
            "applies": True,
            "reason": "천재·지변 또는 원자재 가격급등으로 조정제한기간 내 조정 없이는 이행이 곤란하다고 "
                      "**발주기관이 인정하는 경우** 90일 이내에도 조정할 수 있다.",
            "legal_basis": prof["urgent_exception_basis"],
            "caution": "이 예외의 인정 주체는 발주기관이다 — 이 도구가 인정 여부를 판정하지 않는다.",
        }

    # ── 요건 ②: 등락률 ────────────────────────────────────────────
    threshold = prof["rate_threshold_pct"]
    rate_block: dict[str, Any] = {
        "threshold_pct": threshold,
        "legal_basis": prof["rate_basis"],
        "note": "증감 어느 쪽이든 절대값이 문턱 이상이면 요건을 채운다(감액 조정도 대상이다).",
    }
    if adjustment_rate_pct is None:
        rate_ok = None
        rate_block["declared_rate_pct"] = None
        rate_block["met"] = None
        rate_block["why_unknown"] = ("조정률을 주지 않았다. 이 서버는 품목·지수 조정률을 "
                                     "산정하지 못하므로(원천 지수·단가 미보유) 요건 충족 여부를 "
                                     "판정할 수 없다 — 산정된 값을 받아야 한다.")
    else:
        rate_ok = abs(adjustment_rate_pct) >= threshold
        rate_block["declared_rate_pct"] = adjustment_rate_pct
        rate_block["met"] = rate_ok
        rate_block["direction"] = "증액" if adjustment_rate_pct > 0 else (
            "감액" if adjustment_rate_pct < 0 else "변동 없음")

    # ── 조정 방식 ────────────────────────────────────────────────
    mr = r["method_rule"]
    applied_method = method_specified_in_contract or mr["default"]
    method = {
        "applied": applied_method,
        "applied_label": mr["labels"][applied_method],
        "declared_in_contract": method_specified_in_contract,
        "rule": mr["rule"],
        "legal_basis": mr["basis"],
        "caution": mr["caution"],
    }
    if method_specified_in_contract is None:
        method["assumption"] = ("계약서 명시 여부를 주지 않아 **기본값(품목조정률)**으로 안내했다. "
                                "계약상대자가 지수조정률을 원해 계약서에 명시했다면 지수조정률이다 — "
                                "계약서를 확인하라.")

    # ── 단품 조정(공사) ───────────────────────────────────────────
    single: dict[str, Any] | None = None
    if single_item_rate_pct is not None or single_item_share_over_5permille is not None:
        s_thr = prof["single_item_threshold_pct"]
        single = {
            "threshold_pct": s_thr,
            "legal_basis": prof["single_item_basis"],
            "declared_rate_pct": single_item_rate_pct,
            "share_over_5permille": single_item_share_over_5permille,
            "construction_only": True,
            "cross_profile_warning": (
                f"단품 조정 문턱은 기관유형에 따라 다르다 — 국가·공기업 "
                f"{r['profiles']['national']['single_item_threshold_pct']}%, 지방 "
                f"{r['profiles']['local']['single_item_threshold_pct']}%. 지금은 "
                f"{prof['label']} 기준 {s_thr}%로 판정했다."),
        }
        checks = []
        if not is_construction:
            checks.append(False)
            single["not_construction"] = "단품 조정은 **공사계약**에만 있는 제도다(is_construction=false)."
        if single_item_share_over_5permille is False:
            checks.append(False)
            single["share_note"] = ("해당 자재가 재료비·노무비·경비 합계액의 1천분의 5를 초과하지 "
                                    "않으면 단품 조정 대상이 아니다.")
        elif single_item_share_over_5permille is None:
            single["share_note"] = ("자재 비중(1천분의 5 초과 여부)을 주지 않았다 — 이 조건은 "
                                    "산출내역서로 확인해야 하며 우리가 계산하지 못한다.")
        if single_item_rate_pct is None:
            single["met"] = None
            single["why_unknown"] = "자재 가격증감률을 주지 않아 판정할 수 없다."
        else:
            rate_hit = abs(single_item_rate_pct) >= s_thr
            checks.append(rate_hit)
            single["met"] = (all(checks) and rate_hit
                             if single_item_share_over_5permille is True else None)
            single["rate_met"] = rate_hit
            if single["met"] is None and rate_hit:
                single["met_pending"] = "가격증감률 요건은 충족했다 — 나머지 조건(공사 여부·자재 비중) 확인 필요."

    # ── 금액 계산(값이 다 있을 때만) ───────────────────────────────
    computed: dict[str, Any] | None = None
    if adjustment_base_amount is not None and adjustment_rate_pct is not None:
        if adjustment_base_amount <= 0:
            raise PriceAdjustmentInputError(
                "invalid_base_amount", "물가변동적용대가는 1원 이상이어야 한다.",
                "조정기준일 이후에 이행되는 부분의 대가를 넣어라(산출내역서 기준).")
        rate = adjustment_rate_pct / 100.0
        gross = int(adjustment_base_amount * rate)
        f = r["formulas"]
        arts = prof["formula_articles"]   # 근거 조문은 기관유형별로 다르다(국가 74조 / 지방 72조)
        computed = {
            "adjustment_base_amount": adjustment_base_amount,
            "rate_applied_pct": adjustment_rate_pct,
            "adjustment_amount": gross,
            "formula": f["adjustment_amount"]["expr"],
            "legal_basis": arts["adjustment_amount"],
            "base_amount_note": f["adjustment_amount"]["note"],
        }
        # 선금 공제는 **증액 조정에만** 적용된다 — 법문이 "산출한 증가액에서 공제한다"이다
        # (국가 영 제64조③ / 지방 영 제73조③). 2026-08-14 codex 탐침이 잡은 결함: 감액
        # 조정(-3%)에 공제를 적용해 감액액이 -30,000원에서 -21,000원으로 줄었다 —
        # 계약상대자에게 유리한 쪽으로 틀려서 발주기관이 손해를 본다.
        if advance_payment_ratio and gross <= 0:
            computed["advance_deduction_skipped"] = {
                "reason": "감액(또는 변동 없음) 조정이라 선금 공제를 적용하지 않았다.",
                "legal_basis": f["advance_deduction"]["increase_only"],
                "declared_advance_payment_ratio": advance_payment_ratio,
            }
        elif advance_payment_ratio:
            if not 0 < advance_payment_ratio <= 1:
                raise PriceAdjustmentInputError(
                    "invalid_advance_ratio",
                    f"선금급률 {advance_payment_ratio}는 0 초과 1 이하의 비율이어야 한다(예: 0.3).",
                    "퍼센트가 아니라 비율로 넣어라 — 30%면 0.3.")
            deduction = int(adjustment_base_amount * rate * advance_payment_ratio)
            computed["advance_deduction"] = {
                "advance_payment_ratio": advance_payment_ratio,
                "amount": deduction,
                "formula": f["advance_deduction"]["expr"],
                "legal_basis": arts["advance_deduction"],
                "note": f["advance_deduction"]["note"],
            }
            computed["net_amount"] = gross - deduction
        elif not advance_payment_ratio:
            computed["advance_deduction_note"] = (
                "선금급률을 주지 않아 공제를 반영하지 않았다. 선금을 받은 계약이면 "
                "증가액에서 공제해야 한다 — " + f["advance_deduction"]["expr"])

    # ── 종합 ────────────────────────────────────────────────────
    if rate_ok is None:
        verdict = "undetermined"
        verdict_reason = "조정률이 없어 판정 불가 — 요건 ②를 확인할 수 없다."
    elif period_ok and rate_ok:
        verdict = "requirements_met"
        verdict_reason = "선언된 값 기준으로 기간·등락률 요건을 모두 채웠다."
    elif not period_ok and urgent_exception:
        verdict = "exception_path"
        verdict_reason = ("기간 요건은 미충족이나 천재지변·원자재 급등 예외(제5항) 검토 대상이다 "
                          "— 인정 주체는 발주기관이다.")
    else:
        verdict = "requirements_not_met"
        unmet = []
        if not period_ok:
            unmet.append(f"기간({elapsed}일 < {need}일)")
        if rate_ok is False:
            unmet.append(f"등락률(|{adjustment_rate_pct}%| < {threshold}%)")
        verdict_reason = "미충족: " + ", ".join(unmet)

    warnings = [
        "이 판정은 **사용자가 선언한 값**(조정률·날짜·비중)에 대한 것이다. 값이 틀리면 판정도 틀린다.",
        method["caution"],
    ]
    if single and single.get("cross_profile_warning"):
        warnings.append(single["cross_profile_warning"])
    if prof["response_deadline_days"]:
        warnings.append(
            f"증액 조정은 청구를 받은 날부터 {prof['response_deadline_days']}일 이내에 처리해야 한다 "
            f"({prof['response_deadline_basis']}).")
    else:
        warnings.append(prof["response_deadline_basis"])

    return {
        "org_type": org_type,
        "profile_applied": profile_key,
        "profile_label": prof["label"],
        "verdict": verdict,
        "verdict_reason": verdict_reason,
        "period": period,
        "rate": rate_block,
        "method": method,
        "single_item": single,
        "computed": computed,
        "other_triggers": r["other_triggers"],
        "cannot_do": r["cannot_do"],
        "price_index_sources": r["price_index_sources"],
        "legal_basis": [prof["act_article"], prof["decree_article"], prof["rule_article"]],
        "warnings": warnings,
        "uncertainties": r["uncertainties"],
        "rules_updated": r["last_updated"],
    }
