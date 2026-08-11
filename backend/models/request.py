from pydantic import BaseModel, Field
from typing import Literal


class Step1Request(BaseModel):
    contract_type: Literal["service", "product", "construction"]
    # 발주 기관 유형 — 룰 라우팅(org_type 지정 룰)과 안내 문구에 사용.
    # national=국가기관 / local=지방자치단체 / public_corp=공기업·준정부기관
    org_type: Literal["national", "local", "public_corp"] = "public_corp"
    estimated_price: int = Field(..., gt=0, description="추정가격 (원)")
    service_type: Literal["technical", "academic", "facility", "it_service", "other"] | None = None
    is_sme_competition_product: bool = False
    project_name: str = ""
    description: str = ""
    # 수의계약 사유 (시행령 제26조 각호) — 입력 시 해당 사유 규칙 매칭
    # rebid_failure: 재공고 유찰 (1항 2호)
    # urgent: 긴급 필요 (1항 3호)
    # technical_difficulty: 기술 곤란·불가분 (1항 4호)
    # patent_new_tech: 특허·신기술 (1항 5호)
    # specific_person: 특정인 (1항 6호)
    # small_repeat: 소액 — 경쟁 비효율 (현행 1항 5호 각 목. 2026-07-13 정정:
    #   과거 "소액·반복" 표기의 '반복적·정형적 계약'은 법령에 근거 없음.
    #   동일업체 반복 수의는 사유가 아니라 통제 대상. 이 블록의 호 번호들은
    #   구 시행령 나열 기준이라 현행 각호와 다를 수 있음 — 권위 원본 확인 필요)
    # other_justified: 기타 정당화 (1항 8호)
    negotiation_reason: Literal[
        "rebid_failure", "urgent", "technical_difficulty", "patent_new_tech",
        "specific_person", "small_repeat", "other_justified"
    ] | None = None
    prior_bid_count: int = 0  # 재공고 유찰 시 직전 공고 횟수
    # 우대기업 — 지방계약법 시행령 제25조제1항제5호바목(2천만원 초과 1억원 이하
    # 물품·용역 수의계약). 셋을 한 목으로 묶어 규정하므로 룰은 is_preferential_enterprise
    # 조건으로 OR 매칭한다. 2단계(additional_conditions)에만 있어 MCP 단발 판정
    # 경로로는 표현할 수 없었고, 그래서 "여성기업"이라고 말해도 수의계약 후보가
    # 통째로 누락됐다(2026-07-30 제보 → 07-31 수리).
    is_women_enterprise: bool = False      # 여성기업지원법 제2조제1호
    is_disabled_enterprise: bool = False   # 장애인기업활동법 제2조제2호
    is_social_enterprise: bool = False     # 사회적기업 육성법 제2조제1호 등
    # 청년창업기업 — 같은 조 제5호 다목(2천만원 초과 5천만원 이하). 다목이 신설되며
    # 이후 목이 밀렸는데 정작 다목을 다루는 룰이 없었다(2026-07-31 신설).
    is_youth_startup: bool = False         # 중소기업창업 지원법 제2조제11호
    # 소기업·소상공인 상대방 — 국가 시행령 제26조①5호가목3)·지방 시행령 제25조①5호라목
    # (2천만원 초과 1억원 이하 수의). 종전엔 2단계 additional_conditions에만 있어
    # MCP 단발 판정 경로로 표현할 수 없었다(우대기업 07-31 수리와 같은 구멍 —
    # 2026-08-12 R23에서 국가축 요건별 수의 룰 신설과 함께 1단계로 승격).
    small_enterprise_restriction: bool = False

    # 공사 전문분야 (미입력 시 일반건설공사로 처리)
    # F20-C1 (2026-06-10): 건설산업기본법 시행령 별표1 전문공사 14개 + 기존 6개 = 20개 enum
    # group: 종합(general) / 법령공사(electrical·ict·fire_safety·cultural_heritage·other)
    #      / 전문 14개 (건설산업기본법 시행령 별표1 기준 2억 임계값)
    construction_specialty: Literal[
        # 종합
        "general",
        # 법령공사 (전기·정보통신·소방·문화재 등 별도 법령 적용)
        "electrical", "ict", "fire_safety", "cultural_heritage", "other",
        # 전문공사 14개 (건설산업기본법 시행령 별표1)
        "ground_paving",            # 1. 지반조성·포장공사업
        "interior",                 # 2. 실내건축공사업
        "metal_window_roof",        # 3. 금속창호·지붕건축물조립공사업
        "painting_waterproof",      # 4. 도장·습식·방수·석공사업
        "landscape",                # 5. 조경식재·시설물공사업
        "steel_structure",          # 6. 철강구조물공사업
        "underwater_dredging",      # 7. 수중·준설공사업
        "elevator",                 # 8. 승강기·삭도공사업
        "mechanical",               # 9. 기계가스설비공사업
        "gas_heating",              # 10. 가스난방공사업
        "water_sewer",              # 11. 상·하수도설비공사업
        "boring_grouting",          # 12. 보링·그라우팅·파일공사업
        "railway",                  # 13. 철도·궤도공사업
        "facility_maintenance",     # 14. 시설물유지관리업
    ] | None = None
    construction_specialty_other: str | None = None  # 'other' 선택 시 자유 텍스트
    # F13-4 (2026-06-09): 물품 종류 분기 — 전기·정보통신 등
    product_category: Literal[
        "general",                # 일반 물품
        "electrical",             # 전기 자재 (전기공사업법 적용)
        "ict",                    # 정보통신 자재 (정보통신공사업법)
        "construction_material",  # 공사용 자재
        "office",                 # 사무용품
        "other",                  # 기타 (자유 텍스트)
    ] | None = None
    product_category_other: str | None = None  # 'other' 선택 시 자유 텍스트
    # F13-6 (2026-06-09): 중기간 경쟁제품 다중 선택 — 코드·이름 배열
    sme_product_codes: list[str] = []
    sme_product_names: list[str] = []
    # 6/20 의견: 복수 품목 결합 조건 (or=하나라도 중기간이면 적용, and=모두 중기간일 때만)
    sme_combine_mode: Literal["or", "and"] | None = None
    pq_required: bool = False  # PQ 사전심사 여부 (금액 기반으로 프론트엔드에서 자동 판단)
    # 단순노무용역 여부 (시행규칙 제23조의3 — 경비·청소·시설물관리 등).
    # 소액수의 낙찰하한율이 일반 용역(87.995%)과 다른 89.995% 적용.
    is_simple_labor: bool = False
    # 2026-07-30: LLM 보조설명 생략 요청 — MCP 등 에이전트 클라이언트는 자체 LLM으로
    # 설명을 합성하므로 백엔드 OpenAI 호출(일일 캡 차감)이 불필요. 판정은 동일(결정론).
    skip_llm: bool = False


class Step2Request(BaseModel):
    session_id: str
    additional_conditions: dict = Field(default_factory=dict)
    selected_rule_id: str | None = None  # 사용자가 2단계에서 선택한 후보의 rule_id
    # 사용자가 practice_alternatives에서 직접 선택한 실무 옵션 (2026-05-31, Phase 2)
    # kind: negotiated/small_negotiated_electronic/designated_competitive/general_competitive 등
    selected_alternative_kind: str | None = None
    # 주요 키: regional_restriction, negotiation_contract, pq_required,
    #          is_technical_service, is_sme_mandatory, is_women_enterprise,
    #          is_social_enterprise, is_tech_developed_product


class DocSearchRequest(BaseModel):
    query: str
    contract_type: Literal["service", "product", "construction", "public_procurement"]
    top_k: int = Field(default=5, ge=1, le=20)


class IngestRequest(BaseModel):
    file_path: str
    contract_type: Literal["service", "product", "construction", "public_procurement"]
