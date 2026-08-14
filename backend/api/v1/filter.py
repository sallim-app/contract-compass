import asyncio
import hashlib
import json
import time
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Request
from backend.services.usage_logger import extract_client_meta
from backend.models.request import Step1Request, Step2Request
from backend.models.response import (
    Step1Response, Step2Response, Candidate, RagSource, KnowledgeWebSources,
    NextStepQuestion, FinalRecommendation,
)
from backend.api.deps import get_rule_engine, get_rag_service, get_llm, get_session_store, get_usage_logger
from backend.services.rate_limiter import rate_limit_llm_soft, record_llm_call


class LLMBudgetExhausted(RuntimeError):
    """일일 LLM 캡 소진 — LLM 보조설명만 생략하고 룰엔진 폴백으로 응답하기 위한 신호."""
from backend.services.thresholds import ANNOUNCEMENT_LIMIT, SME_SMALL_ENTERPRISE_UPPER

router = APIRouter(prefix="/filter", tags=["filter"])

# LLM 응답 캐시: 동일 입력에 대한 반복 API 호출 제거 (TTL 1시간)
_LLM_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 3600  # seconds

def _cache_key(data: dict) -> str:
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]

def _cache_get(key: str) -> dict | None:
    if key in _LLM_CACHE:
        ts, val = _LLM_CACHE[key]
        if time.time() - ts < _CACHE_TTL:
            return val
        del _LLM_CACHE[key]
    return None

def _cache_set(key: str, val: dict) -> None:
    _LLM_CACHE[key] = (time.time(), val)
    # 캐시 크기 제한 (최대 200개 항목)
    if len(_LLM_CACHE) > 200:
        oldest = min(_LLM_CACHE, key=lambda k: _LLM_CACHE[k][0])
        del _LLM_CACHE[oldest]

PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts"


# 진실원은 rule_engine — 룰만 필요한 경량 소비자(정적 페이지 생성기·CI 테스트)가
# 이 무거운 모듈을 import하지 않고 같은 함수를 쓰게 하려고 옮겼다(2026-08-06).
# 호출부는 그대로 `_rule_method`를 쓴다.
from backend.services.rule_engine import rule_method as _rule_method  # noqa: E402


def _load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


# F17-E (2026-06-09): LLM 컨텍스트 강화 — 매칭된 룰의 legal_basis 키를
# rules/law_registry.json에서 조문 본문까지 펼쳐서 LLM에 전달.
# 기존: rule_matches에는 legal_basis 키 목록만 — LLM이 "그 법령이 뭔지" 추측 필요.
# 이후: 정확한 본문이 항상 동행 → 환각 차단 + 동일 입력 → 동일 컨텍스트.
from backend.services.law_pack import LAW_REGISTRY as _LAW_REG

def _expand_law(rule: dict) -> dict:
    """룰 객체에 적용 법령의 정확한 조문 본문(law_texts)을 첨부해 반환."""
    out = dict(rule)
    basis_texts = []
    for key in rule.get("legal_basis", []) or []:
        entry = _LAW_REG.get(key)
        if not entry:
            continue
        for art in entry.get("articles", []):
            basis_texts.append({
                "key": key,
                "law_name": entry.get("law_name", ""),
                "article": art.get("title", ""),
                "body": art.get("body", ""),
            })
    if basis_texts:
        out["law_texts"] = basis_texts
    return out


def _safe_format(template: str, **kwargs) -> str:
    """프롬프트 파일 내 JSON 예시의 {} 중괄호를 보호하면서 지정된 키만 치환."""
    escaped = template.replace('{', '{{').replace('}', '}}')
    for key in kwargs:
        escaped = escaped.replace('{{' + key + '}}', '{' + key + '}')
    return escaped.format(**kwargs)


@router.post("/step1", response_model=Step1Response)
async def step1(
    req: Step1Request,
    request: Request,
    rule_engine=Depends(get_rule_engine),
    rag=Depends(get_rag_service),
    llm=Depends(get_llm),
    sessions=Depends(get_session_store),
    usage_logger=Depends(get_usage_logger),
    llm_quota: tuple = Depends(rate_limit_llm_soft),
):
    # 캡 소진이어도 결정론 판정은 계속 — llm_allowed=False면 LLM 설명만 생략(룰 폴백).
    # skip_llm(2026-07-30): MCP 등 에이전트 클라이언트는 자체 LLM으로 설명을 합성하므로
    # 백엔드 LLM 보조설명을 명시적으로 생략(예산 0 소모)할 수 있다.
    client_ip, llm_allowed = llm_quota
    llm_allowed = llm_allowed and not req.skip_llm
    _t0 = time.monotonic()
    params = req.model_dump(exclude={"skip_llm"})
    # 공사 전문분야 미입력 시 일반건설로 기본값 설정 (general CST 규칙이 construction_specialty="general" 조건을 가짐)
    # F15 (2026-06-09): 'other' (기타공사)도 general로 fallback — 룰 매칭 보장 (specialty_other 사용자 입력은 보존)
    if params.get("contract_type") == "construction":
        spec = params.get("construction_specialty")
        if not spec or spec == "other":
            params["construction_specialty"] = "general"
    query = f"{req.contract_type} {req.estimated_price}원 {req.project_name} 계약방법"

    # 1. 규칙 매칭 + RAG 검색 병렬 실행
    matched_rules, knowledge_web = await asyncio.gather(
        asyncio.to_thread(lambda: rule_engine.match(params, org_type=req.org_type)),
        asyncio.to_thread(rag.search_knowledge_web, query, req.contract_type, 5),
    )
    rag_context = rag.build_knowledge_web_context(knowledge_web)

    matched_rules_for_llm = [_expand_law(r) for r in matched_rules[:3]]

    # 결정론 자료 팩 — RAG 의존 제거, 케이스별 정확한 법령 조문 lookup.
    from backend.services.law_pack import build_decision_pack
    decision_pack = build_decision_pack(
        matched_rules[0] if matched_rules else {},
        req.contract_type,
        req.estimated_price,
    ) if matched_rules else None

    # 3. LLM으로 후보 생성 (캐시 적용: 동일 입력 반복 시 LLM 호출 생략)
    _cache_lookup = {
        "contract_type": req.contract_type,
        "estimated_price": req.estimated_price,
        "service_type": req.service_type,
        "is_sme": req.is_sme_competition_product,
        "pq_required": req.pq_required,
        "construction_specialty": params.get("construction_specialty"),
        "negotiation_reason": req.negotiation_reason,
        "top_rules": [r["rule_id"] for r in matched_rules[:3]],
    }
    _ck = _cache_key(_cache_lookup)
    _cached = _cache_get(_ck)

    if _cached is not None:
        parsed = _cached
    else:
        # F17-F: decision_pack을 시스템 프롬프트에 동행 (RAG는 보조 검색 컨텍스트로 유지)
        pack_text = json.dumps(decision_pack, ensure_ascii=False, indent=2) if decision_pack else "(결정론 자료 없음)"
        system_prompt = _safe_format(
            _load_prompt("step1_system.txt"),
            rag_context=rag_context or "관련 규정 없음",
            rule_matches=json.dumps(matched_rules_for_llm, ensure_ascii=False, indent=2),
        ) + (
            "\n\n[결정론 자료 팩 — 케이스별 정확한 적용 문서]\n"
            "※ 아래는 룰엔진이 이 케이스에 대해 lookup한 **확정 자료**입니다.\n"
            "※ RAG 검색 결과보다 우선합니다. laws_applied(법령·시행령·시행규칙 본문)가 "
            "한 번에 전달됩니다.\n"
            f"{pack_text}"
        )
        user_msg = (
            f"계약유형: {req.contract_type}, 추정가격: {req.estimated_price:,}원, "
            f"용역구분: {req.service_type or '해당없음'}, "
            f"중소기업경쟁제품: {'예' if req.is_sme_competition_product else '아니오'}, "
            f"사업명: {req.project_name}"
        )

    # decision_channel: 최종 method는 항상 룰 lookup이므로 "rule". LLM 보조설명 실패 시만 "llm_fallback".
    step1_decision_channel = "rule"
    step1_fallback_reason = None
    try:
        if _cached is None:
            if not llm_allowed:
                raise LLMBudgetExhausted(
                    "skip_llm 요청 — 룰엔진 단독 응답" if req.skip_llm
                    else "일일 LLM 상한 도달 — 룰엔진 단독 응답")
            record_llm_call(client_ip)  # 실제 LLM 호출(캐시 미스)만 카운트 — IP별 + 스코프별 일일 상한
            raw = await llm.complete(system_prompt, user_msg, json_mode=True)
            parsed = json.loads(raw)
            _cache_set(_ck, parsed)
        usage_logger.record_llm_success()
    except Exception as _llm_err:
        usage_logger.log_llm_failure(event="step1", error=str(_llm_err))
        step1_decision_channel = "llm_fallback"
        step1_fallback_reason = type(_llm_err).__name__
        # LLM 실패 시 규칙 엔진 결과만으로 후보 구성
        parsed = {
            "candidates": [
                {
                    "rank": i + 1,
                    "method": _rule_method(r, req.estimated_price),
                    "rule_id": r["rule_id"],
                    "confidence": 0.9,
                    "summary": r.get("name", _rule_method(r, req.estimated_price)),
                    "key_params": {},
                    "clarifying_questions": [],
                }
                for i, r in enumerate(matched_rules[:3])
            ],
            "next_step_questions": [],
        }

    # 4. 세션 저장
    session_id = sessions.create()
    sessions.set(session_id, "step1_input", params)
    sessions.set(session_id, "matched_rules", matched_rules)
    sessions.set(session_id, "candidates", parsed.get("candidates", []))

    # 규칙엔진으로 점수 계산 후 LLM key_params를 보정 (LLM 환산 오류 방지)
    rule_by_id = {r["rule_id"]: r for r in matched_rules}
    candidates = []
    seen_rule_ids: set[str] = set()
    for i, c in enumerate(parsed.get("candidates", [])):
        key_params = dict(c.get("key_params", {}))
        rule = rule_by_id.get(c.get("rule_id", ""))
        # 2026-07-30 R9: 지자체(local) 판정에 국가 적격심사 수치가 새는 마지막 경로 차단.
        # org_type 미지정 룰(=전 기관 공통 — 판로지원법 중기간 경쟁제품·SW진흥법 등)은
        # '의무' 자체가 지자체에도 적용되므로 후보에서 빼면 안 되지만, result에 박힌
        # 통과점수·낙찰하한율은 국가(조달청) 적격심사 세부기준 값이다. 지자체는 행안부
        # 「지방자치단체 입찰시 낙찰자 결정기준」이 적용돼 수치가 다르므로 노출하지 않고
        # 확인처만 안내한다 — 지자체 수치를 임의로 대체하지도 않는다(미확인 값 단정 금지).
        _local_common = (req.org_type == "local" and rule is not None
                         and not rule.get("org_type"))
        if _local_common:
            key_params.pop("pass_score", None)
            key_params.pop("lower_limit_rate", None)
        elif rule:
            score_info = rule_engine.get_pass_score(rule, req.estimated_price)
            if score_info.get("pass_score"):
                key_params["pass_score"] = score_info["pass_score"]
            if score_info.get("lower_limit_rate"):
                key_params["lower_limit_rate"] = f"{score_info['lower_limit_rate'] * 100:.3f}%"
        # 2026-07-30 R9: 지자체 판정은 수치를 제공하지 않는다(LOCAL_* 룰에 값 미인코딩,
        # 공통 룰은 위에서 국가 수치를 제거). 빈 채로 두면 클라이언트 LLM이 국가 수치를
        # 자체 지식으로 메우는 환각 경로가 열리므로, 확인처를 명시적으로 돌려준다.
        if req.org_type == "local" and not (
                key_params.get("pass_score") or key_params.get("lower_limit_rate")):
            key_params["적격심사_기준"] = (
                "지방자치단체는 행정안전부 「지방자치단체 입찰시 낙찰자 결정기준」이 "
                "적용됩니다 — 통과점수·낙찰하한율은 소속 지자체 기준과 입찰공고문을 "
                "확인하세요(국가·공기업 수치를 그대로 적용하지 마십시오)."
            )
        # 2026-06-02 F7-1: 룰의 bidder_options·bidder_selection을 candidate에 전달
        bidder_opts = (rule.get("result", {}).get("bidder_options", []) if rule else [])
        bidder_sel = (rule.get("result", {}).get("bidder_selection") if rule else None)
        # F4-1 e2e: 룰의 legal_basis도 후속 검증·UI용으로 전달
        rule_legal_basis = list(rule.get("legal_basis", []) if rule else [])
        # 2026-07-30 R9: 공통 룰이 국가계약법 조문을 근거로 달고 있으면, 지자체 판정에서는
        # 그 조문이 그대로 인용돼 오인용이 된다(지자체 근거는 지방계약법령). 조문을 임의로
        # 치환하면 호 단위가 검증 전이라 또 다른 오인용이 되므로, 인용은 남기되 적용 한계를
        # 명시한다 — restriction_options()의 기존 처리(조 단위 인용 + 확인 안내)와 같은 원칙.
        if _local_common and any("국가계약법" in str(b) for b in rule_legal_basis):
            rule_legal_basis.append(
                "※ 위 국가계약법령 인용은 국가기관·공기업 기준입니다. 지방자치단체는 "
                "지방계약법령(시행령 제25조·제42조 등)이 적용되므로 소속 지자체 계약 부서 "
                "기준을 확인하세요."
            )
        # F8-1 재정정: LLM이 method를 자체 결정하지 못하게 — 룰의 method 우선 (실무 표현 일관 유지)
        rule_method = _rule_method(rule, req.estimated_price) if rule else c.get("method", "")
        # 2026-07-29 (Codex 적대검증): LLM이 같은 rule_id로 '대안'(일반경쟁·지명경쟁 등)을
        # 2·3순위 후보로 내는 경우, 무조건 룰 method로 덮으면 요약("일반경쟁 선택 가능")과
        # method("소액수의계약")가 모순된다. 룰의 alternatives에 등록된 method와 일치하는
        # LLM method는 그대로 인정한다(환각 차단 원칙 유지 — 등록 외 method는 여전히 강제).
        if rule and c.get("rule_id", "") in seen_rule_ids:
            llm_method = (c.get("method") or "").strip()
            alt_methods = {
                (a.get("method") or "").strip()
                for a in (rule.get("result", {}).get("alternatives") or [])
                if isinstance(a, dict)
            }
            if llm_method and any(llm_method.split("(")[0].strip() == m.split("(")[0].strip()
                                  for m in alt_methods if m):
                rule_method = llm_method
        seen_rule_ids.add(c.get("rule_id", ""))
        candidates.append(Candidate(
            rank=c.get("rank", i + 1),
            method=rule_method,
            rule_id=c.get("rule_id", ""),
            confidence=c.get("confidence", 0.5),
            summary=c.get("summary", ""),
            key_params=key_params,
            clarifying_questions=c.get("clarifying_questions", []),
            bidder_options=bidder_opts,
            bidder_selection=bidder_sel,
            legal_basis=rule_legal_basis,
            notes=rule.get("notes") if rule else None,
        ))

    # 결정론적 보정: LLM이 최상위 매칭 규칙을 누락하면 강제 주입
    # 특히 수의계약 사유(negotiation_reason)처럼 명확한 매칭이 있는 경우 LLM이 일반 규칙을 우선시할 수 있음
    # 2026-07-16 확장: 최상위 1개만이 아니라 '서로 다른 계약방법'은 전부 후보 보장 —
    # 경계 정확값(예: 1억=소액수의 상한 '이하')에서 적법한 수의 후보가 LLM 선택에
    # 따라 비노출되던 문제 정정. 노출 상한 3개는 아래 기존 cap이 유지.
    candidate_rule_ids = {c.rule_id for c in candidates}
    candidate_methods = {_rule_method(rule_by_id[c.rule_id], req.estimated_price)
                         for c in candidates if c.rule_id in rule_by_id}
    rules_to_ensure = list(matched_rules[:1])
    for r in matched_rules[1:]:
        if _rule_method(r, req.estimated_price) not in candidate_methods | {
            _rule_method(x, req.estimated_price) for x in rules_to_ensure
        }:
            rules_to_ensure.append(r)
    for top_rule in rules_to_ensure:
        if top_rule["rule_id"] not in candidate_rule_ids:
            score_info = rule_engine.get_pass_score(top_rule, req.estimated_price)
            injected_key_params: dict = {}
            if score_info.get("pass_score"):
                injected_key_params["pass_score"] = score_info["pass_score"]
            if score_info.get("lower_limit_rate"):
                injected_key_params["lower_limit_rate"] = f"{score_info['lower_limit_rate'] * 100:.3f}%"
            candidates.insert(0, Candidate(
                rank=0,
                method=_rule_method(top_rule, req.estimated_price),
                rule_id=top_rule["rule_id"],
                confidence=0.95,
                summary=top_rule.get("name", ""),
                key_params=injected_key_params,
                clarifying_questions=[],
                notes=top_rule.get("notes"),
            ))

    # 결정론적 재정렬: 규칙 엔진의 priority(낮을수록 더 구체적) 순서를 강제
    # LLM이 임의로 순서를 바꾸지 못하도록 함
    # `_effective_priority`는 룰엔진이 '이하' 경계 정확값에서 붙이는 보정치 —
    # 이 키를 무시하고 raw priority로 재정렬하면 엔진의 경계 보정이 여기서 되돌아간다
    # (2026-08-13 T-2026W33-99: 1억원 정확값 1순위 뒤집힘의 두 번째 코드 위치).
    rule_priority = {r["rule_id"]: r.get("_effective_priority", r.get("priority", 999))
                     for r in matched_rules}
    candidates.sort(key=lambda c: rule_priority.get(c.rule_id, 999))
    # F31 (2026-06-10): candidates max 3개 — UI 노출은 1순위+2~3 보조만, INTL 같은 보조 룰이 4번째로 끼면 제외
    #
    # 2026-08-14 T-2026W33-158: 이 상한이 **서로 다른 계약방법 보장(위 rules_to_ensure)을
    # 무력화**하고 있었다. 국가기관 종합공사 4억원 실측: 매칭 4건 중 CST_003·CST_004·
    # CST_007이 전부 같은 '일반경쟁입찰'(수치도 동일)이라 3슬롯을 중복이 채우고,
    # 유일하게 **다른** 방법인 CST_005(공사 소액수의 — 시행령 제26조①5호가목1, 종합공사
    # 4억원 **이하**는 금액만으로 수의 가능)가 priority 200이라 잘려나갔다. 즉 적법한
    # 계약방법 하나가 응답에서 통째로 사라지고, 잘렸다는 사실조차 공시되지 않았다.
    # → ①방법이 겹치지 않는 후보에 슬롯을 먼저 준다 ②그래도 잘린 것은 실토한다.
    # 순위(priority) 자체는 손대지 않는다 — 경계 정확값 규약(_effective_priority)과 직교.
    omitted_candidates: list[dict] = []
    if len(candidates) > 3:
        kept: list = []
        seen_methods: set[str] = set()
        for c in candidates:                      # 1차: 서로 다른 계약방법 우선
            if len(kept) < 3 and c.method not in seen_methods:
                kept.append(c)
                seen_methods.add(c.method)
        for c in candidates:                      # 2차: 남은 슬롯을 우선순위대로
            if len(kept) < 3 and c not in kept:
                kept.append(c)
        omitted_candidates = [
            {"rule_id": c.rule_id, "method": c.method, "summary": c.summary}
            for c in candidates if c not in kept
        ]
        candidates = sorted(kept, key=lambda c: rule_priority.get(c.rule_id, 999))
    for new_rank, c in enumerate(candidates, start=1):
        c.rank = new_rank

    # F20-B2 + F36-2 (2026-06-11): 중기간 경쟁제품 선택 시 계약방법 자동 고정.
    # 1억 미만 → 중소기업자간 강제 / 1억~2.3억 → 중기간 (조달위탁 선택) / 2.3억 이상 → 조달청 위탁
    # F36-2 보강: SME 룰이 priority 200으로 후순위라 SVC_001(100)에 가려 candidates 누락 → matched_rules에서 강제 주입
    if req.is_sme_competition_product:
        price = req.estimated_price
        sme_pref_ids = []
        if price < SME_SMALL_ENTERPRISE_UPPER:
            sme_pref_ids = ["PRD_SME_PRODUCT_UNDER_100M", "SVC_SME_PRODUCT_UNDER_100M"]
        elif price < ANNOUNCEMENT_LIMIT:
            sme_pref_ids = ["PRD_SME_PRODUCT_MID", "SVC_SME_PRODUCT_MID", "PRD_003"]
        else:
            sme_pref_ids = ["PRD_002", "SVC_SME_PRODUCT_OVER_230M"]
        # 우선 candidates에서 찾기
        preferred = [c for c in candidates if c.rule_id in sme_pref_ids]
        # F36-2: candidates에 없으면 matched_rules에서 강제 주입 (priority 가려진 경우)
        if not preferred:
            from backend.models.response import Candidate as _Cand  # noqa: PLC0415
            for top_rule in matched_rules:
                if top_rule.get("rule_id") in sme_pref_ids:
                    score_info = rule_engine.get_pass_score(top_rule, price)
                    injected_kp: dict = {}
                    if score_info.get("pass_score"):
                        injected_kp["pass_score"] = score_info["pass_score"]
                    if score_info.get("lower_limit_rate"):
                        injected_kp["lower_limit_rate"] = f"{score_info['lower_limit_rate'] * 100:.3f}%"
                    preferred = [_Cand(
                        rank=1,
                        method=_rule_method(top_rule, price),
                        rule_id=top_rule["rule_id"],
                        confidence=0.95,
                        summary=top_rule.get("name", "") + " [중기간 경쟁제품 — F36-2 주입]",
                        key_params=injected_kp,
                        clarifying_questions=[],
                        bidder_options=(top_rule.get("result", {}) or {}).get("bidder_options", []) or [],
                        bidder_selection=(top_rule.get("result", {}) or {}).get("bidder_selection"),
                        legal_basis=top_rule.get("legal_basis", []) or [],
                        notes=top_rule.get("notes"),
                    )]
                    break
        if preferred:
            top = preferred[0]
            if "자동 고정" not in (top.summary or "") and "주입" not in (top.summary or ""):
                top.summary = (top.summary or "") + " [중기간 경쟁제품 자동 고정]"
            # 2026-08-14: 자동 고정으로 화면에서 사라지는 후보도 실토한다 — 의도된 고정이지만
            # 읽는 쪽(에이전트·사용자)에겐 "이것뿐"으로 보이는 것은 같다.
            omitted_candidates += [
                {"rule_id": c.rule_id, "method": c.method, "summary": c.summary}
                for c in candidates if c.rule_id != top.rule_id
            ]
            candidates = [top]
            for nc in candidates:
                nc.rank = 1

    def _to_rag_source(c: dict) -> RagSource:
        content = c["content"]
        return RagSource(
            chunk_id=c["chunk_id"],
            section_title=c["section_title"],
            excerpt=content[:200],
            content=content,  # 2026-05-31: SourceDrawer highlight용 전체 청크
            relevance_score=c["relevance_score"],
            source_type=c.get("source_type", "textbook"),
            document_id=c.get("document_id", ""),
        )

    kw_model = KnowledgeWebSources(
        textbook=[_to_rag_source(c) for c in knowledge_web.get("textbook", [])[:3]],
        guide=[_to_rag_source(c) for c in knowledge_web.get("guide", [])[:3]],
        law=[_to_rag_source(c) for c in knowledge_web.get("law", [])[:3]],
    )
    sources = kw_model.textbook + kw_model.guide + kw_model.law

    # #26/#31: 제한경쟁 질문은 rule_engine이 결정론적으로 생성(정확한 법령근거).
    # LLM 생성 질문 중 제한경쟁 관련(id 중복)은 rule 우선으로 대체.
    rest_opts = rule_engine.restriction_options(
        req.contract_type, req.estimated_price, req.construction_specialty,
        org_type=req.org_type,
    )
    rest_ids = {ro["id"] for ro in rest_opts}
    rest_questions = [
        NextStepQuestion(id=ro["id"], text=ro["text"], description=ro["description"],
                         if_yes=ro["if_yes"], type=ro["type"], options=[],
                         # F13-2 (2026-06-09): 공동도급 4옵션 등 sub_options 전달
                         sub_options=ro.get("sub_options", []))
        for ro in rest_opts
    ]
    llm_questions = [
        NextStepQuestion(
            id=q.get("id", f"q_{i}"),
            text=q.get("text", ""),
            description=q.get("description", ""),
            if_yes=q.get("if_yes", ""),
            type=q.get("type", "boolean"),
            options=q.get("options", []),
        )
        for i, q in enumerate(parsed.get("next_step_questions", []))
        if q.get("id") not in rest_ids  # 제한경쟁은 rule이 담당 — LLM 중복 제거
    ]
    questions = rest_questions + llm_questions

    # 실무 옵션 — 1순위 룰의 alternatives 필드를 노출 (사용자 참고용)
    # 2026-05-31: alt-aware 메트릭 100% 달성한 옵션들을 UI에서도 보이게.
    practice_alts: list = []
    if matched_rules:
        top_rule_alts = matched_rules[0].get("result", {}).get("alternatives", []) or []
        from backend.models.response import PracticeAlternative  # noqa: PLC0415
        for a in top_rule_alts:
            if isinstance(a, dict) and a.get("method") and a.get("kind"):
                practice_alts.append(PracticeAlternative(
                    method=a["method"], reason=a.get("reason", ""), kind=a["kind"]
                ))

    # F28 (2026-06-10): 1순위 룰의 decision_pack 미리 빌드 — Step2 화면에서 Step3와 동일 노출.
    # 사용자 의견 "참조근거가 2단계·3단계 다른데" — 동일한 룰엔진 자료를 두 단계 모두 메인 sources로.
    step1_decision_pack: dict = {}
    if matched_rules:
        try:
            from backend.services.law_pack import build_decision_pack  # noqa: PLC0415
            step1_decision_pack = build_decision_pack(
                matched_rules[0], req.contract_type, req.estimated_price, additional_conditions=None,
            )
        except Exception:
            step1_decision_pack = {}

    # 2026-07-29 (Codex 적대검증): 국제입찰 임계값(7.1억/265억)은 공기업·준정부 고시금액 —
    # 국가기관·지자체 요청에 INTL 계열 룰이 후보로 뜨면 기관유형별 상이 안내를 병기한다.
    if req.org_type != "public_corp" and step1_decision_pack:
        _top3_ids = {r["rule_id"] for r in matched_rules[:3]}
        if any("INTL" in rid or rid == "PRD_006" for rid in _top3_ids):
            step1_decision_pack["human_explanation"] = (
                step1_decision_pack.get("human_explanation", "")
                + " ※ 국제입찰 고시금액은 기관유형별로 다릅니다(본 안내는 공기업·준정부 기준). "
                  "국가기관·지자체는 기획재정부 고시금액을 확인하세요."
            ).strip()

    response = Step1Response(
        session_id=session_id,
        candidates=candidates,
        practice_alternatives=practice_alts,
        rag_sources=sources,
        knowledge_web=kw_model,
        next_step_questions=questions,
        decision_pack=step1_decision_pack,
        omitted_candidates=omitted_candidates,
    )

    try:
        top = candidates[0] if candidates else None
        _ip, _ua = extract_client_meta(request)
        usage_logger.log_step1(
            session_id=session_id,
            contract_type=req.contract_type,
            estimated_price=req.estimated_price,
            service_type=req.service_type,
            is_sme=req.is_sme_competition_product,
            top_rule_id=top.rule_id if top else "",
            pass_score=top.key_params.get("pass_score") if top else None,
            lower_limit_rate=top.key_params.get("lower_limit_rate") if top else None,
            duration_ms=int((time.monotonic() - _t0) * 1000),
            client_ip=_ip,
            user_agent=_ua,
            decision_channel=step1_decision_channel,
            llm_fallback_reason=step1_fallback_reason,
        )
    except Exception:
        pass

    return response


@router.post("/step2", response_model=Step2Response)
async def step2(
    req: Step2Request,
    request: Request,
    rule_engine=Depends(get_rule_engine),
    rag=Depends(get_rag_service),
    llm=Depends(get_llm),
    sessions=Depends(get_session_store),
    usage_logger=Depends(get_usage_logger),
    llm_quota: tuple = Depends(rate_limit_llm_soft),
):
    # 캡 소진이어도 결정론 판정은 계속 — llm_allowed=False면 LLM 설명만 생략(룰 폴백).
    client_ip, llm_allowed = llm_quota
    _t0 = time.monotonic()
    session = sessions.get(req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="세션이 만료되었습니다.")

    step1_input = session.get("step1_input", {})
    candidates = session.get("candidates", [])
    merged_params = {**step1_input, **req.additional_conditions}
    conditions_str = " ".join(str(v) for v in req.additional_conditions.values())
    query = f"{step1_input.get('contract_type', '')} {conditions_str} 계약방법 적격심사"
    contract_type_str = step1_input.get("contract_type", "service")

    # 규칙 재매칭 + RAG 검색 + 공공구매 의무 병렬 실행
    _org_type = step1_input.get("org_type", "public_corp")
    matched_rules, knowledge_web2, obligations = await asyncio.gather(
        asyncio.to_thread(lambda: rule_engine.match(merged_params, org_type=_org_type)),
        asyncio.to_thread(rag.search_knowledge_web, query, contract_type_str, 5),
        asyncio.to_thread(rule_engine.get_public_procurement_obligations, step1_input.get("contract_type", "")),
    )
    rag_context = rag.build_knowledge_web_context(knowledge_web2)

    # 사용자가 특정 후보를 선택한 경우 해당 규칙을 맨 앞으로 이동
    # Q14 (2026-06-13): 선택한 rule_id가 매칭 후보에 없으면 silent 무시되던 버그 정정.
    # 무시 여부를 포착해 final.selection_ignored_reason으로 사용자에게 명시.
    rule_selection_ignored = False
    matched_rule_ids_before = [r["rule_id"] for r in matched_rules[:3]]
    if req.selected_rule_id:
        selected = [r for r in matched_rules if r["rule_id"] == req.selected_rule_id]
        others = [r for r in matched_rules if r["rule_id"] != req.selected_rule_id]
        matched_rules = selected + others
        rule_selection_ignored = not selected

    # Step2 캐시 키: 상위 규칙 + 추가 조건 조합
    _s2_lookup = {
        "top_rules": [r["rule_id"] for r in matched_rules[:2]],
        "additional_conditions": req.additional_conditions,
        "selected_rule_id": req.selected_rule_id,
    }
    _s2_ck = _cache_key(_s2_lookup)
    _s2_cached = _cache_get(_s2_ck)

    # F19 (2026-06-09): decision_pack은 캐시 분기와 무관하게 항상 빌드 — 사용자 응답에 첨부
    from backend.services.law_pack import build_decision_pack as _build_pack
    step2_pack = _build_pack(
        matched_rules[0],
        merged_params.get("contract_type", "service"),
        merged_params.get("estimated_price", 0),
        req.additional_conditions or {},
    ) if matched_rules else None

    if _s2_cached is not None:
        rec_data = _s2_cached
    else:
        system_prompt = _safe_format(
            _load_prompt("step2_system.txt"),
            rag_context=rag_context or "관련 규정 없음",
            candidates=json.dumps(candidates[:3], ensure_ascii=False, indent=2),
            additional_conditions=json.dumps(req.additional_conditions, ensure_ascii=False),
        )
        # 매칭된 룰의 법령 본문 + 결정론 자료 팩(법령·시행령·규칙 한번에)
        matched_rules_with_text = [_expand_law(r) for r in matched_rules[:2]]
        user_msg = (
            f"추가 조건 적용 후 최종 계약방법을 확정해주세요.\n"
            f"규칙 매칭 결과(법령 본문 포함): {json.dumps(matched_rules_with_text, ensure_ascii=False)}\n\n"
            f"[F17-F 결정론 자료 팩 — 이 케이스에 적용되는 정확한 문서]\n"
            f"{json.dumps(step2_pack, ensure_ascii=False, indent=2) if step2_pack else '(없음)'}"
        )

    # decision_channel: 최종 method는 룰 lookup. LLM 보조설명 실패 시만 "llm_fallback".
    step2_decision_channel = "rule"
    step2_fallback_reason = None
    try:
        if _s2_cached is None:
            if not llm_allowed:
                raise LLMBudgetExhausted("일일 LLM 상한 도달 — 룰엔진 단독 응답")
            record_llm_call(client_ip)  # 실제 LLM 호출(캐시 미스)만 카운트 — IP별 + 스코프별 일일 상한
            raw = await llm.complete(system_prompt, user_msg, json_mode=True)
            parsed = json.loads(raw)
            rec_data = parsed.get("final_recommendation", {})
            _cache_set(_s2_ck, rec_data)
        usage_logger.record_llm_success()
    except Exception as _llm_err:
        usage_logger.log_llm_failure(event="step2", error=str(_llm_err))
        step2_decision_channel = "llm_fallback"
        step2_fallback_reason = type(_llm_err).__name__
        # LLM 실패 시 규칙 엔진 최상위 결과로 대체
        top = matched_rules[0] if matched_rules else {}
        rec_data = {
            "method": _rule_method(top, merged_params.get("estimated_price", 0)) if matched_rules else "미확정",
            "rule_id": top.get("rule_id", ""),
            "legal_basis": top.get("legal_basis", []),
            "ai_rationale": "(AI 분석 일시 불가 — 규칙 엔진 결과)",
            "confidence": 0.85,
            "details": {},
            "form_prefill": {},
        }

    # 점수/하한율은 규칙 엔진에서 보정
    if matched_rules:
        score_info = rule_engine.get_pass_score(matched_rules[0], merged_params.get("estimated_price", 0))
        if score_info.get("pass_score") and "details" in rec_data:
            rec_data["details"]["qualification_score"] = score_info["pass_score"]
            rec_data["details"]["lower_limit_rate"] = f"{score_info['lower_limit_rate'] * 100:.3f}%" if score_info.get("lower_limit_rate") else "해당없음"
            # F14 (2026-06-09): 별표 번호·schedule 전달 (계대결 Section 5에 명시)
            if score_info.get("byeolpyo"):
                rec_data["details"]["byeolpyo"] = score_info["byeolpyo"]
            if score_info.get("schedule"):
                rec_data["details"]["schedule"] = score_info["schedule"]
        # 전문공사 면허·주의사항 및 PQ 정보는 규칙에서 결정론적으로 추출
        top_result = matched_rules[0].get("result", {})
        if top_result.get("license_required"):
            rec_data.setdefault("details", {})["license_required"] = top_result["license_required"]
        if top_result.get("special_notes"):
            rec_data.setdefault("details", {})["special_notes"] = top_result["special_notes"]
        if top_result.get("applicable_criteria"):
            rec_data.setdefault("details", {})["applicable_criteria"] = top_result["applicable_criteria"]
        # PQ 관련 정보 추출
        price = merged_params.get("estimated_price", 0)
        pq_gte = top_result.get("pq_required_gte")
        if pq_gte and price >= pq_gte:
            rec_data.setdefault("details", {})["pq_required"] = True
            rec_data["details"].setdefault("special_notes", [])
            pq_notes = [
                f"추정가격 {price // 100_000_000}억원 — PQ 사전심사 의무 대상 (국가계약법 시행령 제21조)",
                "PQ 공고는 입찰공고와 별도로 최소 30일 이상 사전 게재 필요",
                "PQ 통과 업체에 한해 입찰 참가 자격 부여",
            ]
            if isinstance(rec_data["details"]["special_notes"], list):
                rec_data["details"]["special_notes"] = pq_notes + rec_data["details"]["special_notes"]
            else:
                rec_data["details"]["special_notes"] = pq_notes
        elif top_result.get("pq_evaluation_items"):
            rec_data.setdefault("details", {})["pq_required"] = True
            rec_data["details"]["pq_evaluation_items"] = top_result["pq_evaluation_items"]
            if top_result.get("special_notes"):
                rec_data["details"].setdefault("special_notes", top_result["special_notes"])

    # legal_basis는 항상 규칙 엔진에서 가져옴 (LLM 환각 방지)
    # LLM이 생성한 legal_basis는 ai_rationale 안에서만 활용하도록 의도적으로 무시
    deterministic_legal_basis = matched_rules[0].get("legal_basis", []) if matched_rules else []

    # 2026-06-05 F9-1: 제한경쟁 조건(performance_restriction·regional_restriction 등)이 conditions에 적용되면
    # method 라벨을 동적으로 "제한경쟁입찰 (실적제한)" 등으로 변환. legal_basis 보강.
    # F14 (2026-06-09): 결정론 룰엔진 우선 — LLM의 method가 룰과 다르면 룰을 따름 (환각 방지).
    # 사용자가 발견한 큰 버그: 100억 공사가 "일반경쟁(적격심사)"로 나옴 — LLM이 잘못 추정.
    # 룰엔진 method_by_amount는 100억+ = "종합심사낙찰제 (PQ 대상)"로 정확.
    base_method = (
        _rule_method(matched_rules[0], merged_params.get("estimated_price", 0))
        if matched_rules else rec_data.get("method", "미확정")
    )
    final_method = base_method
    deterministic_legal_basis = list(deterministic_legal_basis or [])
    ac = req.additional_conditions or {}
    restriction_label = ""
    # 2026-07-30 R8: 지자체는 제한입찰 근거가 지방계약법 시행령 제20조 — 국가 조문 혼입 방지
    _is_local_org = merged_params.get("org_type") == "local"
    if ac.get("performance_restriction"):
        restriction_label = "실적제한"
        if not any("21조" in s or "20조" in s for s in deterministic_legal_basis):
            deterministic_legal_basis.append(
                "지방계약법 시행령 제20조 (제한입찰 — 실적제한)" if _is_local_org
                else "국가계약법 시행령 제21조 제1항 제1호 (실적제한)")
    elif ac.get("regional_restriction"):
        restriction_label = "지역제한"
        if not any("21조 제1항 제6호" in s or "21조1항6호" in s or "20조" in s
                   for s in deterministic_legal_basis):
            deterministic_legal_basis.append(
                "지방계약법 시행령 제20조 (제한입찰 — 지역제한)" if _is_local_org
                else "국가계약법 시행령 제21조 제1항 제6호 (지역제한)")
    elif ac.get("sme_restriction"):
        restriction_label = "중소기업자간"
        if not any("판로지원법" in s for s in deterministic_legal_basis):
            deterministic_legal_basis.append("판로지원법 시행령 제2조의2")
    elif ac.get("small_enterprise_restriction"):
        restriction_label = "소기업·소상공인"

    # F9-3: 공동도급 — 별도 라벨 (제한경쟁 라벨과 독립)
    if ac.get("joint_contract"):
        if not any("공동계약운영요령" in s for s in deterministic_legal_basis):
            deterministic_legal_basis.append("계약예규 공동계약운영요령 (기획재정부)")

    if restriction_label and restriction_label not in final_method:
        # 라벨이 이미 포함되어 있지 않으면 추가 — "일반경쟁입찰" → "제한경쟁입찰", 그 다음 (라벨)
        if "일반경쟁입찰" in final_method:
            final_method = final_method.replace("일반경쟁입찰", "제한경쟁입찰") + f" ({restriction_label})"
        elif "제한경쟁입찰" in final_method:
            final_method = final_method + f" ({restriction_label})"
        else:
            final_method = f"제한경쟁입찰 ({restriction_label}) — {final_method}"

    # F13-5 (2026-06-09): 사용자가 step2에서 선택한 모든 additional_conditions를
    # 응답에 명시 노출 → 사용자가 "내 선택이 반영됐는지" 즉시 확인 가능
    applied_conditions = dict(req.additional_conditions or {})
    selection_ignored_reason = None

    # F25-A (2026-06-10): ai_rationale 환각 차단 — 입력에 없는 contract_type 단어·법령 인용 제거.
    # 사용자 의견: "사업개요에 용역 없는데 용역 문구가 있다고 거짓말"
    from backend.services.law_pack import strip_unverified_citations
    raw_rationale = rec_data.get("ai_rationale", "")
    safe_rationale = strip_unverified_citations(raw_rationale, deterministic_legal_basis)
    # 추가: 사용자 입력에 없는 contract_type 단어가 ai_rationale에 들어가면 generic 표현으로
    user_desc = req.additional_conditions.get("description") if isinstance(req.additional_conditions, dict) else ""
    user_text = (str(user_desc) + " " + (merged_params.get("project_name","") or "")).strip()
    actual_ct = merged_params.get("contract_type")
    ct_map = {"service": "용역", "construction": "공사", "product": "물품"}
    for ct_key, ct_word in ct_map.items():
        if ct_key != actual_ct and ct_word in safe_rationale and ct_word not in user_text:
            safe_rationale = safe_rationale.replace(ct_word, "사업")

    final = FinalRecommendation(
        method=final_method,
        rule_id=rec_data.get("rule_id", matched_rules[0]["rule_id"] if matched_rules else ""),
        details=rec_data.get("details", {}),
        public_procurement_obligations=obligations,
        legal_basis=deterministic_legal_basis,
        ai_rationale=safe_rationale,
        confidence=rec_data.get("confidence", 0.8),
        form_prefill=rec_data.get("form_prefill", {}),
        applied_conditions=applied_conditions,
        # Q15 (2026-06-13): registry 키 배열 — Step4 체크박스·docx 법령 본문 필터가 키 형식을 기대.
        # laws_applied[].key는 _law_text()로 registry 검증된 키만 포함 (결정론적).
        legal_basis_keys=[l["key"] for l in (step2_pack or {}).get("laws_applied", [])],
        # F19 (2026-06-09) + F25-D: LLM에 전달된 결정론 자료 팩 — 항상 노출 (빈 dict도 OK)
        decision_pack=step2_pack or {},
    )

    # Q14 (2026-06-13): 선택한 rule_id가 매칭 후보에 없어 무시된 경우 사유 명시
    if rule_selection_ignored:
        final.selection_ignored_reason = (
            f"선택한 룰 '{req.selected_rule_id}'이 현재 조건의 매칭 후보"
            f"({matched_rule_ids_before})에 없어 1순위 룰 {final.rule_id}로 진행"
        )

    # Phase 2 (2026-05-31): 사용자가 practice_alternative 직접 선택 시 method 오버라이드
    # rule 매칭은 1순위 그대로 (legal_basis 보존), 실무 선택만 final.method/details 반영
    if req.selected_alternative_kind and matched_rules:
        top_alts = matched_rules[0].get("result", {}).get("alternatives", []) or []
        chosen = next((a for a in top_alts if isinstance(a, dict) and a.get("kind") == req.selected_alternative_kind), None)
        if not chosen:
            # F13-5: 선택이 무시된 사유 응답 노출
            final.selection_ignored_reason = (
                f"선택한 옵션 '{req.selected_alternative_kind}' 이 1순위 룰 "
                f"{matched_rules[0]['rule_id']} 의 alternatives 목록에 없음"
            )
        if chosen:
            final.method = chosen["method"]
            final.ai_rationale = (
                f"[사용자 실무 옵션 선택] {chosen['method']} — {chosen.get('reason', '')}\n"
                f"(시스템 1순위 추천은 {rec_data.get('method', '')}이나 사용자가 실무 사정으로 변경)"
            )
            # 수의·소액·지명 등은 pass_score/lower_limit_rate 비활성 (적격심사 외)
            if chosen["kind"] in ("negotiated", "small_negotiated_electronic", "designated_competitive"):
                final.details["qualification_score"] = "해당없음 (수의·견적 등)"
                final.details["lower_limit_rate"] = "해당없음"
            final.details["user_alternative_selected"] = chosen["kind"]
            final.confidence = max(final.confidence, 0.9)  # 사용자 명시 선택이므로 신뢰도 보장

    sessions.set(req.session_id, "final_recommendation", final.model_dump())
    # F13 (2026-06-09): step2_conditions를 form.generate에서 활용 가능하도록 보존
    # (joint_contract_kind, regional_restriction_region 등)
    sessions.set(req.session_id, "step2_conditions", dict(req.additional_conditions or {}))

    def _to_rs(c: dict) -> RagSource:
        content = c["content"]
        return RagSource(
            chunk_id=c["chunk_id"],
            section_title=c["section_title"],
            excerpt=content[:200],
            content=content,
            relevance_score=c["relevance_score"],
            source_type=c.get("source_type", "textbook"),
            document_id=c.get("document_id", ""),
        )

    kw2_model = KnowledgeWebSources(
        textbook=[_to_rs(c) for c in knowledge_web2.get("textbook", [])[:3]],
        guide=[_to_rs(c) for c in knowledge_web2.get("guide", [])[:3]],
        law=[_to_rs(c) for c in knowledge_web2.get("law", [])[:3]],
    )
    sources2 = kw2_model.textbook + kw2_model.guide + kw2_model.law

    response2 = Step2Response(
        session_id=req.session_id,
        final_recommendation=final,
        rag_sources=sources2,
        knowledge_web=kw2_model,
    )

    try:
        _ip, _ua = extract_client_meta(request)
        usage_logger.log_step2(
            session_id=req.session_id,
            rule_id=final.rule_id,
            method=final.method,
            confidence=final.confidence,
            duration_ms=int((time.monotonic() - _t0) * 1000),
            client_ip=_ip,
            user_agent=_ua,
            decision_channel=step2_decision_channel,
            llm_fallback_reason=step2_fallback_reason,
        )
    except Exception:
        pass

    return response2
