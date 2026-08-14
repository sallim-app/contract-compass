"""contract_rules.json 기반 결정론적 계약방법 필터."""
import json
from pathlib import Path
from typing import Any


def rule_method(rule: dict, price: int = 0) -> str:
    """규칙 result에서 금액에 맞는 계약방법 문자열 추출. method_by_amount 지원.

    여기 있는 이유(2026-08-06): 원래 `backend/api/v1/filter.py`의 `_rule_method`였는데,
    그 모듈을 import하면 chromadb·numpy까지 끌려온다(2,333 모듈). 룰 결과에서 값을
    끌어내는 순수 함수를 무거운 API 모듈에 두면 **룰만 필요한 소비자**(정적 페이지
    생성기, 경량 CI 테스트)가 그 값을 재구현하게 되고, 그 사본이 다음 개정에서
    본선과 갈린다 — `pass_score_ref` 단일화(8151f15)와 같은 이유로 사본을 만들지
    않는다. filter.py는 이 함수를 alias로 그대로 쓴다(호출부 무변경).
    """
    result = rule.get("result", {})
    if "method" in result:
        return result["method"]
    method_map = result.get("method_by_amount", {})
    if method_map:
        tiers = sorted(
            ((int(k.split("_", 1)[1]), v) for k, v in method_map.items()),
            reverse=True,
        )
        for threshold, method in tiers:
            if price >= threshold:
                return method
        return tiers[-1][1] if tiers else "미확정"
    return "미확정"


class RuleEngine:
    def __init__(self, rules_path: str):
        self._path = Path(rules_path)
        self._data: dict = {}
        self.reload()

    def reload(self):
        with open(self._path, encoding="utf-8") as f:
            self._data = json.load(f)

    @property
    def thresholds(self) -> dict:
        return self._data.get("thresholds", {})

    # 공용(센티널) 룰이 대신 받는 전문분야 집합 — _check_conditions와 아래 판별기가 공유한다.
    _PRO_GROUP = {"ground_paving", "interior", "metal_window_roof", "painting_waterproof",
                  "landscape", "steel_structure", "underwater_dredging", "elevator",
                  "mechanical", "gas_heating", "water_sewer", "boring_grouting",
                  "railway", "facility_maintenance"}
    _LEGAL_GROUP = {"fire_safety", "cultural_heritage", "other"}

    def _specialty_match_kind(self, rule: dict, params: dict) -> str | None:
        """이 룰이 전문분야를 **정확히** 물었나, 공용(센티널) 룰로 대신 받았나.

        2026-08-14 T-2026W33-148: `fire_safety` 룰이 '그 밖의 공사(법령공사)' 금액 기준
        공용 룰을 겸하는데, 그 때문에 문화재수리공사 질의가 CST_HERITAGE_002(정확 일치)와
        CST_FIRE_002(센티널)에 **동시에** 매칭됐다. 둘은 priority가 같아(115) 파일 순서로
        소방 룰이 1순위를 가져갔고, 실무자는 **소방시설공사업 요건**을 문화재수리공사의
        요건으로 읽게 됐다(기치 ① 도메인 사실 정확성 위반, 라이브 실측).
        금액 구간 판정에는 공용 룰이 맞으므로 후보에서 지우지는 않고, **정확 일치 룰보다
        뒤로** 보낸다(아래 _sort_key).
        """
        val = (rule.get("conditions") or {}).get("construction_specialty")
        if val is None:
            return None
        p_spec = params.get("construction_specialty")
        return "exact" if p_spec == val else "sentinel"

    def match(self, params: dict, org_type: str = "public_corp") -> list[dict]:
        """입력 파라미터에 맞는 규칙을 우선순위 순으로 반환.

        org_type: 시행 주체(public_corp/national/local). 규칙에 선택적 `org_type` 필드가
        있으면 해당 기관에만 매칭, 없으면(현행 룰) 전 기관 공통 → **후방호환**(기본 public_corp).
        """
        matched = []
        input_ct = params.get("contract_type", "")
        for rule in self._data.get("rules", []):
            # 기관유형 필터: 규칙 org_type 지정 시 해당 기관만(미지정=공통).
            # 문자열(단일) 또는 리스트(복수 기관) 허용 — 2026-07-30 R8: 국가 소액수의
            # 기본 룰(SVC_002·PRD_005)이 지자체 판정에 국가 근거로 혼입되던 결함 수리.
            rule_org = rule.get("org_type")
            if rule_org:
                allowed = rule_org if isinstance(rule_org, list) else [rule_org]
                if org_type not in allowed:
                    continue
            # 계약유형이 다른 규칙은 제외 (public_procurement는 모든 유형에 적용 가능)
            rule_ct = rule.get("contract_type", "")
            if rule_ct != "public_procurement" and input_ct and rule_ct != input_ct:
                continue
            if self._check_conditions(rule.get("conditions", {}), params):
                kind = self._specialty_match_kind(rule, params)
                matched.append({**rule, "_specialty_match": kind} if kind else rule)
        ordered = self._order_matched(matched, params.get("estimated_price", 0))
        # 최종 순서를 값으로 박아 하위 소비자(api/v1/filter.py)가 재정렬해도 유지되게 한다 —
        # priority만 다시 읽으면 여기서 한 보정(경계·센티널)이 되돌아간다.
        return [{**r, "_order_rank": i} for i, r in enumerate(ordered)]

    # 경계에서 새로 열리는 룰을 '이하' 룰 바로 뒤로 보낼 때 쓰는 증분 —
    # 정수 priority 사이에 끼우기만 하면 되므로 값 자체엔 의미가 없다.
    _BOUNDARY_EPSILON = 0.5

    def _sort_key(self, rule: dict) -> tuple:
        """정렬 우선순위: ①전문분야 정확 일치 먼저 ②경계 보정 priority ③원 priority.

        센티널(공용 룰) 강등이 ①이다 — 전용 룰이 있는 전문분야에서 공용 룰이 1순위를
        가져가면 잘못된 업종 요건을 제시한다(T-2026W33-148). 전용 룰이 없는 전문분야
        (other 등)는 전부 센티널이라 상대 순서가 그대로다.
        """
        return (1 if rule.get("_specialty_match") == "sentinel" else 0,
                rule.get("_effective_priority", rule.get("priority", 999)),
                rule.get("priority", 999))

    def _order_matched(self, matched: list[dict], price: int) -> list[dict]:
        """priority 순 정렬 — 단, `boundary_inclusive_rank` 룰의 상한 경계에서 순위 역전을 막는다.

        법문에서 수의계약 상한은 '…이하'라 **경계 정확값도 그 목의 구간**이다
        (국가계약법 시행령 제26조①5호가목3)~5) 2천만원 초과 **1억원 이하**,
        가목7) 5천만원 이하). 그런데 경쟁 룰은 같은 경계에서 '이상'(_gte)으로 열리므로
        **정확히 그 한 점에서만** 두 룰이 겹치고, 경쟁 룰의 priority가 더 앞서면
        1순위가 뒤집힌다 — 소기업·소상공인 요건을 선언한 국가 물품 건이
        99,999,999원엔 소액수의(PRD_NEGO_SMALLBIZ), 100,000,000원엔 일반경쟁
        (PRD_003B)이 되는 1원짜리 불연속(2026-08-13 T-2026W33-99 본선 재현).

        경계 직전에는 존재하지도 않던(그 점에서 비로소 열리는) 룰이, 그 점까지
        이어져 온 '이하' 룰을 1순위에서 밀어내지 못하게 한다. 즉 **경계 정확값의
        순위 = 경계 직전의 순위 + 새로 열린 룰들(뒤에 이어붙임)**. 구간을 걸쳐
        있는(경계에서 열리지 않는) 룰은 손대지 않으므로, 5천만원처럼 진짜 구간이
        시작하는 경계에서 SVC_IT_002(정보화사업 5천만~2.3억) 같은 룰이 밀리는
        부작용은 없다.

        **옵트인인 이유**: 전역 규칙으로 두면 "경계에서 새로 열리는 룰"이 아닌
        구간 룰까지 순서가 흔들린다(5천만원처럼 진짜 구간이 시작하는 경계에서
        SVC_IT_002 같은 룰이 밀리는 부작용). 그래서 룰이
        `boundary_inclusive_rank: true`로 선언한 경우에만 적용한다.

        **적용 범위(2026-08-14 갱신, T-2026W33-145)**: 물품·용역 요건부 수의
        (가목3)~5)7))와 **공사 소액수의(CST_005·CST_ELEC_003·CST_ICT_003·
        CST_PRO_003·CST_FIRE_003·CST_HERITAGE_003)**, 지방 대응 룰까지 전부 옵트인이다.
        법문이 같은 '이하'이므로 4분면(국가·지방 × 물품·용역·공사)에 같은 규약을 쓴다.
        종전 이 주석은 "공사는 경계에서 경쟁을 1순위로 두는 규약이 고정돼 있다"고
        적혀 있었는데 **지금 코드·룰과 맞지 않는 stale 설명**이었다(라이브 실측:
        전기 1.6억 정확값 1순위 = CST_ELEC_003, 즉 경계 포함이 이미 적용 중).
        종합공사 3억~4억 구간은 경쟁 룰(CST_003)이 겹쳐 그쪽이 1순위인데, 그건
        경계 문제가 아니라 구간 구성이다 — 3.99억과 4억의 1순위가 같아 불연속이 없다.

        보정된 룰에는 `_effective_priority`를 붙인 얕은 사본을 반환한다(원본 룰
        딕셔너리는 self._data 공유물이라 변형 금지). 하위에서 priority로 재정렬하는
        소비자(`api/v1/filter.py`)는 이 키를 우선 읽어야 순서가 유지된다.
        """
        # 상한이 경계 정확값인 옵트인 룰 — 없으면 경계가 아니므로 아무것도 안 바뀐다
        closing = [r for r in matched
                   if r.get("boundary_inclusive_rank")
                   and r.get("conditions", {}).get("estimated_price_lte") == price]
        if not closing:
            return sorted(matched, key=self._sort_key)

        floor = min(r.get("priority", 999) for r in closing)
        adjusted = []
        for rule in matched:
            cond = rule.get("conditions", {})
            prio = rule.get("priority", 999)
            opens_here = (cond.get("estimated_price_gte") == price
                          and cond.get("estimated_price_lte") != price)
            if opens_here and prio <= floor:
                rule = {**rule, "_effective_priority": floor + self._BOUNDARY_EPSILON}
            adjusted.append(rule)
        # 2차 키(원 priority)로 보정된 룰들끼리의 상대 순서를 유지
        adjusted.sort(key=self._sort_key)
        return adjusted

    def _check_conditions(self, conditions: dict, params: dict) -> bool:
        price = params.get("estimated_price", 0)
        ct = params.get("contract_type", "")

        for key, val in conditions.items():
            # '초과'(>)와 '이상'(≥)은 법문에서 구별된다 — 지방계약법 시행령 제25조제1항
            # 제5호 라·마·바목은 "2천만원 초과"라 정확히 2천만원이면 그 목이 아니라
            # 나목(2천만원 이하 소액수의)이 근거다. _gte만 있던 시절엔 경계에서 근거
            # 조문이 틀리게 붙었다(2026-07-31 수리). 경쟁입찰 룰은 금액과 무관하게
            # 항상 가능하므로 _gte를 유지한다.
            if key == "estimated_price_gt" and price <= val:
                return False
            elif key == "estimated_price_gte" and price < val:
                return False
            elif key == "estimated_price_lt" and price >= val:
                return False
            # '이하' 경계 (2026-07-16): 수의계약 상한은 법령상
            # '이하'(lte)인데 lt만 지원해 경계 정확값이 배제되던 문제 정정
            elif key == "estimated_price_lte" and price > val:
                return False
            elif key == "contract_type" and ct != val:
                return False
            elif key == "is_sme_competition_product":
                if params.get("is_sme_competition_product", False) != val:
                    return False
            elif key == "is_sme_mandatory":
                if params.get("is_sme_mandatory", False) != val:
                    return False
            elif key == "regional_restriction":
                if params.get("regional_restriction", False) != val:
                    return False
            elif key == "performance_restriction":
                if params.get("performance_restriction", False) != val:
                    return False
            elif key == "sme_restriction":
                if params.get("sme_restriction", False) != val:
                    return False
            elif key == "small_enterprise_restriction":
                if params.get("small_enterprise_restriction", False) != val:
                    return False
            elif key == "is_youth_startup":
                # 지방계약법 시행령 제25조제1항제5호다목 — 「중소기업창업 지원법」
                # 제2조제11호 청년창업기업, 2천만원 초과 5천만원 이하 물품·용역.
                # 이 목이 신설되며 이후 목(라·마·바)이 밀렸는데 정작 다목 자체를
                # 다루는 룰이 없어 청년창업기업 수의계약이 후보에 오르지 않았다.
                if params.get("is_youth_startup", False) != val:
                    return False
            elif key == "is_simple_labor":
                if params.get("is_simple_labor", False) != val:
                    return False
            elif key == "negotiation_contract":
                if params.get("negotiation_contract", False) != val:
                    return False
            elif key == "pq_required":
                if params.get("pq_required", False) != val:
                    return False
            elif key == "is_technical_service":
                if params.get("is_technical_service", False) != val:
                    return False
            elif key == "is_sme_product":
                if params.get("is_sme_product", False) != val:
                    return False
            elif key == "is_women_enterprise":
                if params.get("is_women_enterprise", False) != val:
                    return False
            elif key == "is_social_enterprise":
                if params.get("is_social_enterprise", False) != val:
                    return False
            elif key == "is_preferential_enterprise":
                # 지방계약법 시행령 제25조제1항제5호바목은 여성기업·장애인기업·사회적기업·
                # 사회적협동조합·자활기업·마을기업을 **하나의 목**으로 묶는다. 예전에는
                # is_social_enterprise 하나만 봐서, 클라이언트가 '여성기업'이라고 말하며
                # is_women_enterprise만 세우면 수의계약 후보가 통째로 누락됐다
                # (2026-07-30 제보 mcp: "여성기업 특례를 판정하지 못했습니다").
                got = any(params.get(k, False) for k in (
                    "is_preferential_enterprise", "is_women_enterprise",
                    "is_disabled_enterprise", "is_social_enterprise",
                ))
                if got != val:
                    return False
            elif key == "is_tech_developed_product":
                if params.get("is_tech_developed_product", False) != val:
                    return False
            elif key == "service_type":
                if params.get("service_type") != val:
                    return False
            elif key == "negotiation_reason":
                if params.get("negotiation_reason") != val:
                    return False
            elif key == "construction_specialty":
                # F20-C1 (2026-06-10) → 2026-07-30 P0 수리: 전문공사(건산법 별표1 14종)는
                # "professional_generic" 센티널 룰(CST_PRO_*·LOCAL_CST_*_PRO)에 매칭한다.
                # 전기·정보통신은 건산법 전문공사가 아니라 각자 법령(전기공사업법·정보통신공사업법,
                # 소액수의 1.6억)이 적용되는 '그 밖의 공사' — electrical 겸용 시절엔
                # 전문 14종이 1.6억 룰(CST_ELEC_*)에, 전기공사가 2억 룰(LOCAL_*_PRO)에
                # 양방향 오판정됐다. 법령공사 그룹(other 포함)은 fire_safety 룰 재사용.
                p_spec = params.get("construction_specialty")
                PRO_GROUP = self._PRO_GROUP
                LEGAL_GROUP = self._LEGAL_GROUP
                if p_spec == val:
                    pass  # 정확 일치
                elif val == "professional_generic" and p_spec in PRO_GROUP:
                    pass  # 건산법 전문공사 14종 → 전문공사 공용 룰
                elif val == "fire_safety" and p_spec in LEGAL_GROUP:
                    pass  # 법령공사는 fire_safety 룰에 매칭
                else:
                    return False
            elif key == "product_category":  # F13-4
                if params.get("product_category") != val:
                    return False
            elif key == "joint_contract_kind":  # F13-2
                if params.get("joint_contract_kind") != val:
                    return False
            elif key == "prior_bid_count_gte":
                if params.get("prior_bid_count", 0) < val:
                    return False
        return True

    def _resolve_pass_score_ref(self, result: dict) -> dict | None:
        """`pass_score_ref`(다른 룰의 rule_id)가 있으면 그 룰의 구간표를 빌려 온다.

        왜 참조인가 (2026-08-05 P0 수리): 같은 낙찰하한율을 **두 룰이 각자 들고 있었다.**
        CST_001은 금액 구간별 5단 표(50억↑ 87.495% / 10억↑ 88.745% / 3억↑ 89.745%)를
        갖는데, CST_007은 같은 4억~100억 구간을 **단일 상수 89.745%** 하나로 덮었다.
        구간마다 바뀌는 값을 평률로 덮었으니 구조적으로 틀릴 수밖에 없다 — 70억 종합공사
        한 번의 질의에 87.495%(CST_001)와 89.745%(CST_007)가 **같은 응답 안에** 함께 나왔고,
        2.25%p면 투찰 하한이 1.58억 어긋난다. 투찰은 되돌릴 수 없다.

        값을 복사해 맞추면 지금은 같아지지만 다음 개정에서 또 갈린다. 사본을 늘리는 대신
        **진실원을 하나로 만든다** — 요율이 바뀌면 고칠 곳이 한 군데다.
        """
        ref = result.get("pass_score_ref")
        if not ref:
            return None
        for r in self._data.get("rules", []):
            if r.get("rule_id") == ref:
                return (r.get("result", {}) or {}).get("pass_score_by_amount")
        return None

    def get_pass_score(self, rule: dict, price: int) -> dict[str, Any]:
        """규칙에서 금액별 적격심사 점수 및 낙찰하한율 계산.

        pass_score_by_amount 키 형식: "gte_<정수>" — 값이 큰 구간부터 매칭.
        `pass_score_ref`가 있으면 참조 룰의 구간표를 쓴다(요율 진실원 단일화).
        """
        result = rule.get("result", {})
        pass_score_map = result.get("pass_score_by_amount") or self._resolve_pass_score_ref(result)

        if pass_score_map:
            # 키("gte_N")를 숫자로 파싱, 내림차순 정렬
            tiers = sorted(
                ((int(k.split("_", 1)[1]), v) for k, v in pass_score_map.items()),
                reverse=True,
            )
            for threshold, score_info in tiers:
                if price >= threshold:
                    return score_info
            # 모든 구간 미만이면 마지막(최소) 구간 반환
            return tiers[-1][1] if tiers else {}

        return {
            "pass_score": result.get("pass_score"),
            "lower_limit_rate": result.get("lower_limit_rate"),
        }

    def get_public_procurement_obligations(self, contract_type: str) -> list[dict]:
        """해당 계약유형의 공공구매 의무 목록 반환."""
        obligations_path = self._path.parent / "public_procurement_obligations.json"
        if not obligations_path.exists():
            return []
        with open(obligations_path, encoding="utf-8") as f:
            data = json.load(f)
        result = []
        for ob in data.get("obligations", []):
            scope = ob.get("scope", [])
            if contract_type in scope or not scope:
                result.append(ob)
        return result

    def restriction_options(self, contract_type: str, price: int,
                            construction_specialty: str | None = None,
                            org_type: str = "public_corp") -> list[dict]:
        """제한경쟁 가능 항목을 금액 기준으로 결정론적 판정.
        각 항목에 정확한 법령 근거 부여(LLM 환각 방지). NextStepQuestion 형식 dict 반환.
        근거: 국가계약법 시행령 제21조·판로지원법 시행령·공기업준정부기관 계약사무규칙.
        ※ 지방자치단체(org_type="local")는 지방계약법령·행안부 예규 기준이 일부 달라
        해당 항목에 확인 안내를 덧붙인다(미확인 값 단정 금지 원칙)."""
        local_note = (" ※ 지방자치단체는 지방계약법령 기준(한도·절차)이 다를 수 있으니 "
                      "소속 지자체 계약 부서 기준을 확인하세요." if org_type == "local" else "")
        # 2026-07-30 R8: 지자체 요청에 국가 시행령 제21조가 그대로 인용되던 혼재 수리 —
        # 지자체 제한입찰 근거는 지방계약법 시행령 제20조(호 단위는 검증 전이라 조 단위 인용).
        _is_local = org_type == "local"
        perf_basis = ("지방계약법 시행령 제20조(제한입찰)" if _is_local
                      else "국가계약법 시행령 제21조 제1항 제1호")
        region_basis_generic = ("지방계약법 시행령 제20조(제한입찰)" if _is_local
                                else "국가계약법 시행령 제21조 제1항 제6호")
        th = self.thresholds
        notice = th.get("announcement_limit", 230_000_000)        # 고시금액 2.3억
        small = th.get("sme_small_enterprise_upper", 100_000_000)  # 1억
        opts: list[dict] = []

        def q(qid, text, desc, if_yes, sub_options=None):
            d = {"id": qid, "text": text, "description": desc, "if_yes": if_yes, "type": "boolean"}
            if sub_options:
                d["sub_options"] = sub_options
            return d

        if contract_type == "construction":
            # general=종합공사로 간주(전기·정보통신·소방 등은 전문). #21에서 정식 분리 예정
            is_general = construction_specialty in (None, "", "general")
            kind = "종합공사" if is_general else "전문공사"
            region_limit = 15_000_000_000 if is_general else 1_000_000_000   # 150억 / 10억
            perf_limit = 3_000_000_000 if is_general else 300_000_000        # 30억 / 3억
            if _is_local:
                region_basis = "지방계약법 시행규칙 제24조(제한입찰의 제한기준)"
            else:
                region_basis = ("공기업·준정부기관 계약사무규칙 제6조 제4항 (150억원 미만 건설공사)"
                                if is_general else "국가계약법 시행령 제21조 제1항 제6호")
            # [선택] 지역제한 — 종합 150억·전문 10억 미만. 법령상 선택 사항이나
            # 기관 내규로 의무화한 곳이 많아 내규 확인 안내를 덧붙인다.
            if price < region_limit:
                opts.append(q(
                    "regional_restriction",
                    "우리 지역(시·도) 업체로 제한하시겠습니까? [선택]",
                    f"추정가격 {region_limit // 100_000_000}억원 미만 {kind}는 지역제한 경쟁 가능. "
                    f"근거: {region_basis}. 법령상 선택 사항이나 기관 내규로 의무 적용인 경우가 "
                    f"있으니 소속 기관 계약 규정을 확인하세요.{local_note}",
                    "지역제한 경쟁입찰로 전환됩니다.",
                ))
            # [선택] 실적제한
            if price >= perf_limit:
                opts.append(q(
                    "performance_restriction",
                    f"시공 실적 보유 업체로 제한하시겠습니까? [선택]",
                    f"추정가격 {perf_limit // 100_000_000}억원 이상 {kind}는 실적제한 가능. "
                    f"근거: {perf_basis}{local_note}",
                    "실적제한 경쟁입찰이 적용됩니다.",
                ))
                # F20-C2 (2026-06-10): 공사 시공능력 제한 (사용자 의견 — 실적제한과 별개 옵션)
                opts.append(q(
                    "construction_capability_restriction",
                    f"시공능력평가액으로 제한하시겠습니까? [선택]",
                    f"직전 사업년도 시공능력평가액(추정가의 1배 이상)을 기준으로 입찰참가 제한. "
                    f"실적제한과 다른 제한 방식 — "
                    f"{'지방계약법 시행령 제20조(제한입찰)' if _is_local else '국가계약법 시행령 제21조(제한경쟁입찰)'} "
                    f"참조. 자유 텍스트 입력 가능.",
                    "시공능력 제한이 적용됩니다 (자격요건에 명시 — 추천 문구 3종 선택 가능).",
                ))
        else:
            kind = "용역" if contract_type == "service" else "물품"
            # [필수] 중소기업자간 경쟁 / 소기업·소상공인
            if price < notice:
                opts.append(q(
                    "sme_restriction",
                    "중소기업만 입찰하도록 제한하시겠습니까? [필수 검토]",
                    f"추정가격이 고시금액(2.3억원) 미만인 {kind}은 중소기업자간 경쟁이 원칙입니다. "
                    f"근거: 판로지원법 시행령 제2조의2 (단, 기술용역·폐기물용역 등은 예외 — 시행령 제2조의3)",
                    "중소기업자간 경쟁입찰로 제한됩니다.",
                ))
            if price < small:
                opts.append(q(
                    "small_enterprise_restriction",
                    "소기업·소상공인으로 제한하시겠습니까? [필수 검토]",
                    "추정가격 1억원 미만은 소기업·소상공인 제한 대상입니다. 근거: 판로지원법 시행령 제2조의2",
                    "소기업·소상공인 제한경쟁이 적용됩니다.",
                ))
            # [선택] 실적제한(고시금액 이상) / 지역제한(고시금액 미만)
            if price >= notice:
                opts.append(q(
                    "performance_restriction",
                    "실적 보유 업체로 제한하시겠습니까? [선택]",
                    f"추정가격 고시금액(2.3억원) 이상 {kind}은 실적제한 가능. 근거: {perf_basis}{local_note}",
                    "실적제한 경쟁입찰이 적용됩니다.",
                ))
            else:
                opts.append(q(
                    "regional_restriction",
                    "우리 지역(시·도) 업체로 제한하시겠습니까? [선택]",
                    f"추정가격 고시금액(2.3억원) 미만 {kind}은 지역제한 가능. 근거: {region_basis_generic}{local_note}",
                    "지역제한 경쟁입찰로 전환됩니다.",
                ))
        # 2026-06-05 F9-3 / 2026-06-09 F13-2: 공동도급 4가지 세부 옵션 + 법령 클릭
        # F28-B (2026-06-10): 공사계약에 지역의무공동도급 옵션 추가 (사용자 의견 반영).
        # 근거: 시행령 제72조 제3~4항.
        # 적용 범위: 종합공사 88억 ≤ price < 265억 / 전문공사 10억 ≤ price < 265억
        joint_subopts = [
            {"value": "co_exec_only", "label": "공동이행만 허용",
             "desc": "공동수급체 구성원이 공동으로 시공·인력 투입 — 공동연대책임",
             "law": "공동계약운영요령 제7조"},
            {"value": "div_exec_only", "label": "분담이행만 허용",
             "desc": "공동수급체 구성원이 분담된 공종을 각자 시공 — 분담연대책임",
             "law": "공동계약운영요령 제11조"},
            {"value": "both_allowed", "label": "공동·분담 자율 선택",
             "desc": "입찰자가 공동이행 또는 분담이행 자율 선택 가능 (가장 흔함)",
             "law": "공동계약운영요령 제5조"},
            {"value": "none", "label": "공동도급 미허용 (단독)",
             "desc": "단독 입찰자만 가능 — 사유 명시 필요 (발주기관이 공동계약 허용 여부 결정)",
             "law": "계약예규 「공동계약운영요령」"},
        ]
        if contract_type == "construction":
            is_general = construction_specialty in (None, "", "general")
            regional_lo = 8_800_000_000 if is_general else 1_000_000_000   # 종합 88억 / 전문 10억
            regional_hi = 26_500_000_000                                    # 고시금액② 265억
            if regional_lo <= price < regional_hi:
                joint_subopts.insert(0, {
                    "value": "regional_mandatory",
                    "label": "🏢 지역의무공동도급 (지역업체 1인 이상 의무)",
                    "desc": (
                        f"추정가격 {regional_lo // 100_000_000}억~{regional_hi // 100_000_000}억 미만 "
                        f"{'종합' if is_general else '전문/법령'}공사는 지역의무공동도급 적용 — "
                        "해당 지역업체 1인 이상이 공동수급체 구성원이어야 함. "
                        "단, 자격보유자가 10인 미만이면 예외."
                    ),
                    "law": "국가계약법 시행령 제72조 제3~4항",
                })
        opts.append(q(
            "joint_contract",
            "공동도급으로 발주하시겠습니까? [선택]",
            "공동도급은 2개 이상 업체가 공동수급체를 구성해 입찰. 근거: 계약예규 「공동계약운영요령」 (기획재정부 계약예규)",
            "공동계약이 적용됩니다.",
            sub_options=joint_subopts,
        ))
        return opts
