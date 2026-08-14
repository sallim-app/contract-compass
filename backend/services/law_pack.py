"""결정론 자료 팩 빌더 — 매칭된 룰의 적용 법령 조문을 결정론적으로 패키징.

설계 원칙:
- RAG 검색은 변동 가능 (같은 쿼리에 다른 청크가 올 수 있음 → 결정론 위배).
- 룰엔진은 케이스(계약유형 + 금액 + 조건)별로 적용 자료를 결정론적으로 lookup.
- 매칭된 룰 → legal_basis → law_registry.json에서 조문 본문
- 한 번의 LLM 호출 컨텍스트에 모두 동행.

결과: 동일 입력에 동일 컨텍스트 → 동일 출력 (결정론 보장).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from backend.services.rule_engine import rule_method

_RULES_DIR = Path(__file__).resolve().parent.parent.parent / "rules"

# 국제입찰 임계값(7.1억/265억)은 **공기업·준정부 고시금액**이다. 국가기관·지자체 요청에
# INTL 계열 룰이 후보로 뜨면 기관유형별로 다르다는 안내를 병기해야 한다
# (2026-07-29 codex 적대검증에서 나온 규칙).
# 여기 있는 이유(2026-08-06): 이 문구가 `filter.py` 안에만 있어서 같은 룰을 소비하는
# 정적 가이드 페이지 생성기가 경고 없이 국가기관 페이지에 공기업 기준을 실었다.
# 룰을 소비하는 모든 경로가 같은 문구를 쓰도록 진실원을 하나로 둔다.
INTL_BID_CAVEAT = (
    "국제입찰 고시금액은 기관유형별로 다릅니다(본 안내는 공기업·준정부 기준). "
    "국가기관·지자체는 기획재정부 고시금액을 확인하세요."
)


def intl_bid_caveat(org_type: str, rule_ids) -> str:
    """국제입찰 기관유형 주의문구. 해당 없으면 빈 문자열."""
    if org_type == "public_corp":
        return ""
    if any("INTL" in rid or rid == "PRD_006" for rid in rule_ids):
        return INTL_BID_CAVEAT
    return ""

# Load once at module import (즉시 결정론적, 매 호출에 디스크 IO 없음)
with open(_RULES_DIR / "law_registry.json", encoding="utf-8") as _f:
    _LAW = json.load(_f)
    _LAW_REG: dict[str, dict] = _LAW["registry"]
    _METHOD_LAW: dict[str, list[str]] = _LAW["method_law_keys"]


# 레지스트리 키는 단축형("시행령 제26조"), 룰 legal_basis는 정식형
# ("국가계약법 시행령 제26조 제1항 …")이라 등호/startswith 매칭이 항상 실패했음
# (2026-07-16 점검: 수의계약 전 계열 등 34개 룰·method 조합 laws_applied=0).
# 포함 매칭 + 긴 키 우선(지방계약법 시행령 제26조 > 시행령 제26조) +
# 경계 가드(제26조가 제26조의2에 오매칭 방지)로 해석한다.
_REG_KEYS_BY_LEN = sorted(_LAW_REG.keys(), key=len, reverse=True)
_METHOD_KEYS_BY_LEN = sorted(_METHOD_LAW.keys(), key=len, reverse=True)

# 포함 매칭만으로는 **키의 법령명 표기가 인용문과 다르면** 여전히 못 잡는다.
# (2026-07-28 점검: 키 "공기업계약사무규칙 제7조의2" ↔ 인용 "공기업·준정부기관 계약사무규칙
#  제7조의2" — 조문은 레지스트리에 있는데 미해석. 그러면 아래 method 폴백이 대신
#  일반경쟁 조문을 붙여, '비어 보이지 않는' 오첨부가 됐다.)
# 키 자체를 바꾸면 method_law_keys·Step4 시드가 깨지므로 별칭으로 흡수한다.
_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "공기업계약사무규칙": ("공기업·준정부기관 계약사무규칙", "공기업ㆍ준정부기관 계약사무규칙"),
    "중소기업제품구매촉진법": ("중소기업제품 구매촉진 및 판로지원에 관한 법률",),
    "공공기관운영법": ("공공기관의 운영에 관한 법률",),
    "국가계약법": ("국가를 당사자로 하는 계약에 관한 법률",),
    "지방계약법": ("지방자치단체를 당사자로 하는 계약에 관한 법률",),
}


def _key_variants(key: str) -> tuple[str, ...]:
    """레지스트리 키 → 인용문에서 쓰일 수 있는 표기 변형들(자기 자신 포함)."""
    out = [key]
    for short, longs in _KEY_ALIASES.items():
        if key.startswith(short):
            rest = key[len(short):]
            out.extend(f"{lg}{rest}" for lg in longs)
    return tuple(out)


_REG_KEY_VARIANTS: dict[str, tuple[str, ...]] = {k: _key_variants(k) for k in _LAW_REG}


def _key_in(text: str, key: str) -> bool:
    for variant in _REG_KEY_VARIANTS.get(key, (key,)):
        pos = text.find(variant)
        if pos < 0:
            continue
        tail = text[pos + len(variant):]
        if not tail or tail[0] not in "의0123456789":
            return True
    return False


def resolve_registry_keys(legal_basis: list[str] | None, method: str,
                          *, include_method_defaults: bool = True) -> list[str]:
    """룰의 legal_basis 문자열들 + method → law_registry 키 목록.

    basis 문자열마다 가장 긴 포함 키 1개만 취하고(한 인용은 한 조문),
    method는 정확 일치 → 괄호 앞 base 일치 → base 접두 일치 순으로 푼다.
    include_method_defaults=False면 method별 기본 법령 키를 얹지 않는다 —
    기본 키가 국가계약법 기준이라 지자체(LOCAL_*) 룰에 혼입되던 결함 방지(2026-07-30 R8).
    """
    out: list[str] = []

    def _add(k: str) -> None:
        if k not in out:
            out.append(k)

    for b in legal_basis or []:
        for k in _REG_KEYS_BY_LEN:
            if _key_in(b, k):
                _add(k)
                break
    if not include_method_defaults:
        return out
    base = method.split("(")[0].strip()
    for m in (method, base):
        if m in _METHOD_LAW:
            for k in _METHOD_LAW[m]:
                _add(k)
            break
    else:
        for mk in _METHOD_KEYS_BY_LEN:
            if base and (base.startswith(mk) or mk.startswith(base)):
                for k in _METHOD_LAW[mk]:
                    _add(k)
                break
    return out


def _law_text(key: str) -> dict[str, Any] | None:
    """법령 key → 본문 dict (전체 조문 포함)."""
    entry = _LAW_REG.get(key)
    if not entry:
        return None
    return {
        "key": key,
        "law_name": entry.get("law_name", ""),
        "articles": [
            {"title": a.get("title", ""), "body": a.get("body", "")}
            for a in entry.get("articles", [])
        ],
    }


# 의견 본문 환각 조문 정정 — legal_basis에 있는 조문만 인용 허용,
# 그 외 "제N조" 인용은 generic 표현으로 대체 (구 form_generator에서 이관).
import re as _re2

_CITATION_RE = _re2.compile(
    r"(?:국가계약법|지방계약법|공공기관운영법|중소기업제품구매촉진법|법|시행령|시행규칙|규칙|규정)\s*"
    r"제\s*\d+\s*조"
    r"(?:\s*제\s*\d+\s*항)?(?:\s*제\s*\d+\s*호)?",
)

# 레지스트리 전체 노출 (filter._expand_law 등에서 조문 본문 첨부용)
LAW_REGISTRY: dict[str, dict] = _LAW_REG


def strip_unverified_citations(text: str, legal_basis: list[str]) -> str:
    """본문에서 legal_basis에 없는 조문 인용을 generic 표현으로 대체.

    매칭 기준: '제N조' 부분이 legal_basis 어느 항목에든 나타나면 통과.
    """
    if not text:
        return text
    haystack = _re2.sub(r"\s+", "", " ".join(legal_basis))

    def _repl(m):
        cite = m.group()
        art = _re2.search(r"제\s*\d+\s*조", cite)
        if not art:
            return cite
        art_norm = _re2.sub(r"\s+", "", art.group())
        if art_norm in haystack:
            return cite  # legal_basis에 있음 — 그대로
        return "관련 법령"  # 환각 의심 → generic

    return _CITATION_RE.sub(_repl, text)


def build_decision_pack(
    rule: dict,
    contract_type: str,
    estimated_price: int,
    additional_conditions: dict | None = None,
) -> dict:
    """케이스에 적용되는 정확한 자료를 결정론적으로 패키징.

    출력 구조 — LLM에 통째로 전달:
        {
          "rule": { rule_id, method, conditions, ... },
          "laws_applied": [ { key, law_name, articles: [{title, body}] }, ... ],
          "additional_conditions": {...}  # 사용자 추가 선택
        }
    """
    # 1. 매칭된 룰의 적용 법령 (legal_basis + 방법별 default 법령)
    #    조문 본문은 registry 키로 해석해 붙이고, 사람용 근거 문구는 원문 인용 유지
    # 금액구간별로 방법이 갈리는 룰(`method_by_amount`)은 `result.method`가 없다.
    # 그걸 그대로 쓰면 method=""가 되어 설명이 "→ **** 1순위 추천"으로 깨지고,
    # 적용 법령도 방법 기반 default 조문을 못 받는다. 실측(2026-08-06): CST_001은
    # 50억 이상/100억 이상 두 구간을 method_by_amount로 갖고 있어 70억 종합공사
    # 판정의 human_explanation에서 계약방법이 통째로 사라졌다.
    method = (rule.get("result", {}) or {}).get("method") or rule_method(rule, estimated_price)
    _is_local = rule.get("org_type") == "local"
    law_keys = resolve_registry_keys(rule.get("legal_basis"), method,
                                     include_method_defaults=not _is_local)
    laws_applied = [t for t in (_law_text(k) for k in law_keys) if t]

    # 2. 룰엔진 자연어 설명 — 금액·계약유형 외에 매칭된 모든 조건
    #    (전문분야·중기간·지역·실적·사유)을 자연어로 풀어 설명.
    ct_label = {"construction": "공사", "service": "용역", "product": "물품"}.get(contract_type, contract_type)
    # 2026-07-29 (Codex 적대검증): 정수 절삭으로 2.3억→"2억원" 표기되던 것 정정 —
    # 경계값 검증을 방해하지 않도록 소수 첫째 자리까지 보존.
    if estimated_price >= 100_000_000:
        _eok = estimated_price / 100_000_000
        price_text = f"{_eok:g}억원" if _eok != int(_eok) else f"{int(_eok)}억원"
    elif estimated_price >= 10_000_000:
        # 2026-07-30 R8: 20,000,001원이 "2,000만원"으로 반올림돼 경계값 초과 사실이
        # 가려지던 결함 정정 — 만원 단위로 떨어지지 않으면 원 단위 전액 표기.
        if estimated_price % 10_000:
            price_text = f"{estimated_price:,}원"
        else:
            price_text = f"{estimated_price // 10_000:,}만원"
    else:
        price_text = f"{estimated_price:,}원"
    # 매칭된 조건 자연어로 — rule.conditions + additional_conditions 모두 반영
    rule_conds = rule.get("conditions", {})
    extra_facts: list[str] = []
    SPEC_LABEL = {
        "general": "종합공사", "professional_generic": "전문공사(건산법 14종)",
        "electrical": "전기공사", "ict": "정보통신공사",
        "fire_safety": "소방시설공사", "cultural_heritage": "문화재수리", "other": "기타 법령공사",
        "ground_paving": "지반조성·포장", "interior": "실내건축", "metal_window_roof": "금속창호·지붕",
        "painting_waterproof": "도장·습식·방수", "landscape": "조경식재", "steel_structure": "철강구조물",
        "underwater_dredging": "수중·준설", "elevator": "승강기·삭도", "mechanical": "기계가스설비",
        "gas_heating": "가스난방", "water_sewer": "상·하수도설비", "boring_grouting": "보링·그라우팅",
        "railway": "철도·궤도", "facility_maintenance": "시설물유지관리",
    }
    if rule_conds.get("construction_specialty"):
        spec = rule_conds["construction_specialty"]
        extra_facts.append(SPEC_LABEL.get(spec, spec))
    if rule_conds.get("is_sme_competition_product"):
        extra_facts.append("중기간 경쟁제품")
    if rule_conds.get("negotiation_reason"):
        REASON_LABEL = {
            "rebid_failure": "재공고 유찰", "urgent": "긴급", "technical_difficulty": "기술 곤란",
            "patent_new_tech": "특허·신기술", "specific_person": "특정인", "small_repeat": "소액(경쟁 비효율)",
            "other_justified": "기타 정당화",
        }
        extra_facts.append(f"수의 사유: {REASON_LABEL.get(rule_conds['negotiation_reason'], rule_conds['negotiation_reason'])}")
    ac = additional_conditions or {}
    if ac.get("regional_restriction"):
        extra_facts.append("지역제한 적용")
    if ac.get("performance_restriction"):
        extra_facts.append("실적제한 적용")
    if ac.get("construction_capability_restriction"):
        extra_facts.append("시공능력 제한 적용")
    if ac.get("sme_restriction"):
        extra_facts.append("중소기업자간 제한")
    if ac.get("small_enterprise_restriction"):
        extra_facts.append("소기업·소상공인 제한")
    if ac.get("joint_contract"):
        extra_facts.append(f"공동도급({ac.get('joint_contract_kind','자율')})")

    cond_text = " · ".join([ct_label] + extra_facts) if extra_facts else ct_label
    alts = rule.get("result", {}).get("alternatives", []) or []
    alt_methods = [a.get("method", "") for a in alts if isinstance(a, dict) and a.get("method")]
    # 사람용 근거 문구는 룰 원문 인용(항·호까지)이 더 정확 — registry 키는 fallback
    _basis_texts = (rule.get("legal_basis") or [])[:2] or law_keys[:2]
    law_text = f"근거: {', '.join(_basis_texts)}" if _basis_texts else ""

    # "왜 이 방법인가" — 매칭된 룰 자신의 금액 조건에서 도출 (2026-07-30 R8).
    # 종전에는 룰과 무관한 국가 기준 금액대 일반론을 붙여 "일반경쟁입찰 적용인데
    # 왜? 소액수의 가능"류 자기모순·기관유형 불일치가 났다. 룰 조건→설명이면
    # 모순이 구조적으로 불가능하다.
    def _fmt_won(v: int) -> str:
        if v >= 100_000_000:
            _e = v / 100_000_000
            return f"{_e:g}억원"
        if v >= 10_000_000 and v % 10_000_000 == 0:
            return f"{v // 10_000_000}천만원"
        if v % 10_000 == 0:
            return f"{v // 10_000:,}만원"
        return f"{v:,}원"

    rationale: list[str] = []
    _gte = rule_conds.get("estimated_price_gte")
    _lte = rule_conds.get("estimated_price_lte")
    _lt = rule_conds.get("estimated_price_lt")
    if _gte and (_lte or _lt):
        _hi, _hi_word = (_lte, "이하") if _lte else (_lt, "미만")
        rationale.append(f"추정가격 {_fmt_won(_gte)} 이상 {_fmt_won(_hi)} {_hi_word} 구간")
    elif _gte:
        rationale.append(f"추정가격 {_fmt_won(_gte)} 이상")
    elif _lte or _lt:
        _hi, _hi_word = (_lte, "이하") if _lte else (_lt, "미만")
        rationale.append(f"추정가격 {_fmt_won(_hi)} {_hi_word}")
    # 적격심사 별표 인용 (룰에 별표 정보가 있으면)
    pass_info = (rule.get("result", {}) or {}).get("pass_score_by_amount") or {}
    if pass_info:
        # 금액에 해당하는 별표 찾기
        for band_key in sorted(pass_info.keys(), key=lambda k: int(k.replace("gte_", "")), reverse=True):
            threshold = int(band_key.replace("gte_", ""))
            if estimated_price >= threshold:
                band = pass_info[band_key] or {}
                byeolpyo = band.get("byeolpyo")
                pass_score = band.get("pass_score")
                if byeolpyo and pass_score:
                    rationale.append(f"{byeolpyo} 적용 (적격심사 {pass_score}점)")
                break

    reason_text = " · ".join(rationale[:2]) if rationale else ""

    if alt_methods:
        human_explanation = (
            f"추정가격 {price_text} · {cond_text} → **{method}** 1순위 추천. "
            + (f"**왜?** {reason_text}. " if reason_text else "")
            + f"실무 재량 선택: {' · '.join(alt_methods[:3])}. {law_text}"
        ).strip()
    else:
        # 수의계약 등은 임의규정("할 수 있다")이라 "선택지가 없다"고 단정하면 오도 —
        # 일반경쟁입찰은 법령상 언제나 선택 가능함을 병기한다. (Codex 적대검증 반영)
        _always_open = (
            "" if method.startswith("일반경쟁")
            else " (일반경쟁입찰은 법령상 언제나 선택 가능)"
        )
        human_explanation = (
            f"추정가격 {price_text} · {cond_text} → **{method}** 적용{_always_open}. "
            + (f"**왜?** {reason_text}. " if reason_text else "")
            + law_text
        ).strip()

    return {
        "rule": {
            "rule_id": rule.get("rule_id"),
            "name": rule.get("name"),
            "method": method,
            "conditions": rule.get("conditions", {}),
            "legal_basis_keys": law_keys,
            "result": rule.get("result", {}),
            # 2026-08-12 R23(T-2026W33-58): 룰 notes(요건 경고)가 decision_pack에 실리지
            # 않아 LLM·에이전트가 "2천만 초과~1억 요건부" 경고를 받지 못했다 — 배달한다.
            "notes": rule.get("notes"),
        },
        "human_explanation": human_explanation,
        "laws_applied": laws_applied,
        "additional_conditions": additional_conditions or {},
        "_summary": {
            "law_count": len(laws_applied),
            "total_chars": sum(len(a.get("body", "")) for law in laws_applied for a in law.get("articles", [])),
        },
    }
