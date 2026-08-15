export type ContractType = 'service' | 'product' | 'construction'
export type ServiceType = 'technical' | 'academic' | 'facility' | 'it_service' | 'other'
// general=종합공사(토목·건축·토목건축·산업환경설비·조경),
// 전기·정보통신·소방·문화재는 기타 법령에 따른 전문 (제한경쟁 금액기준은 전문공사와 동일)
// other=기타 (조경·환경·해체 등 자유 텍스트, F10-1 2026-06-07)
// F20-C1 (2026-06-10): 종합 1 + 법령 5 + 전문 14 = 20개 (건설산업기본법 시행령 별표1)
export type ConstructionSpecialty =
  | 'general'
  | 'electrical' | 'ict' | 'fire_safety' | 'cultural_heritage' | 'other'
  | 'ground_paving' | 'interior' | 'metal_window_roof' | 'painting_waterproof'
  | 'landscape' | 'steel_structure' | 'underwater_dredging' | 'elevator'
  | 'mechanical' | 'gas_heating' | 'water_sewer' | 'boring_grouting'
  | 'railway' | 'facility_maintenance'
export type NegotiationReason =
  | 'rebid_failure'
  | 'urgent'
  | 'technical_difficulty'
  | 'patent_new_tech'
  | 'specific_person'
  | 'small_repeat'
  | 'other_justified'

// 발주 기관유형 — 국가기관 / 지방자치단체 / 공기업·준정부기관 (적용 법령·기준 분기)
export type OrgType = 'national' | 'local' | 'public_corp'

export interface Step1Input {
  contract_type: ContractType
  estimated_price: number
  org_type: OrgType
  service_type?: ServiceType
  construction_specialty?: ConstructionSpecialty
  construction_specialty_other?: string  // 'other' 선택 시 자유 텍스트 (F10-1)
  pq_required?: boolean
  // 단순노무용역 여부 (시행규칙 제23조의3 — 경비·청소·시설물관리 등). 소액수의 낙찰하한율 89.995%.
  is_simple_labor?: boolean
  is_sme_competition_product: boolean
  project_name: string
  description: string
  negotiation_reason?: NegotiationReason
  prior_bid_count?: number
  // 2026-05-20 요구사항 #1: 중기간 경쟁제품 적용 심사기준 (분류번호 카드 승인 결과)
  sme_product_code?: string
  sme_product_name?: string
  sme_applicable_standard?: '조달청' | '중기부' | '직접발주' | null
  // F13-6 (2026-06-09): 중기간 다중 선택 — 코드·이름 배열
  sme_product_codes?: string[]
  sme_product_names?: string[]
  // 6/20 의견: 복수 품목 결합 조건 (OR=하나라도 중기간이면 적용, AND=모두 중기간일 때만 적용)
  sme_combine_mode?: 'or' | 'and'
  // F13-4 (2026-06-09): 물품 종류 분기
  product_category?: 'general' | 'electrical' | 'ict' | 'construction_material' | 'office' | 'other'
  product_category_other?: string
  // 2026-05-27 #29: 계약유형 AI 추천 메타 (Step2에서 확정/변경)
  suggested_contract_type?: ContractType
  suggested_confidence?: number
  suggested_reason?: string
}

export interface Candidate {
  rank: number
  method: string
  rule_id: string
  confidence: number
  summary: string
  key_params: Record<string, unknown>
  clarifying_questions: string[]
  // 2026-06-02 F7-1: 낙찰자결정방법 (시행령 제42조) — 계약방법과 별개
  bidder_selection?: string | null
  bidder_options?: { bidder: string; reason: string; kind: string; law_basis?: string }[]
  // High #1: 공공계약 실무 사례의 method 분포 (참고 신호)
  community_prior?: {
    top_method: string
    top_ratio: number
    n: number
    message: string
    distribution: { method: string; count: number; ratio: number }[]
  } | null
}

export interface NextStepQuestion {
  id: string
  text: string
  description: string
  if_yes: string
  type: string
  options: string[]
  // F13-2 (2026-06-09): YES 선택 시 노출되는 sub-options (예: 공동도급 4종)
  sub_options?: { value: string; label: string; desc: string; law: string }[]
}

export interface RagSource {
  chunk_id: string
  section_title: string
  excerpt: string
  content?: string  // 청크 전체 본문 (SourceDrawer highlight용, 2026-05-31)
  relevance_score: number
  source_type?: 'textbook' | 'guide' | 'law'
  document_id?: string
}

export interface KnowledgeWebSources {
  textbook: RagSource[]
  guide: RagSource[]
  law: RagSource[]
}

export interface PracticeAlternative {
  method: string
  reason: string
  kind: string
}

export interface Step1Response {
  session_id: string
  step: number
  candidates: Candidate[]
  practice_alternatives?: PracticeAlternative[]
  rag_sources: RagSource[]
  knowledge_web?: KnowledgeWebSources
  next_step_questions: NextStepQuestion[]
  // F28 (2026-06-10): 1순위 룰의 결정론 자료 팩 — Step2/Step3 일관성 (laws_applied만 제공)
  decision_pack?: {
    rule?: { rule_id?: string; name?: string; method?: string }
    human_explanation?: string
    laws_applied?: Array<{ key: string; law_name: string; articles: Array<{ title: string; body: string }> }>
    _summary?: { law_count?: number; total_chars?: number }
  }
}

export interface FinalRecommendation {
  method: string
  rule_id: string
  details: Record<string, unknown>
  public_procurement_obligations: Array<Record<string, unknown>>
  legal_basis: string[]
  ai_rationale: string
  confidence: number
  form_prefill: Record<string, string>
  // F13-5 (2026-06-09): step2에서 적용된 사용자 선택 + 무시된 사유
  applied_conditions?: Record<string, unknown>
  selection_ignored_reason?: string | null
  // Q15 (2026-06-13): _LAW_REGISTRY 키 배열 (Step4 체크박스·docx 법령 본문 필터용)
  legal_basis_keys?: string[]
  // F19 (2026-06-09): LLM에 전달된 결정론 자료 팩 — 사용자 투명성 (laws_applied만 제공)
  decision_pack?: {
    rule?: { rule_id?: string; name?: string; method?: string }
    human_explanation?: string
    laws_applied?: Array<{ key: string; law_name: string; articles: Array<{ title: string; body: string }> }>
    _summary?: { law_count?: number; total_chars?: number }
  }
}

export interface Step2Response {
  session_id: string
  step: number
  final_recommendation: FinalRecommendation
  rag_sources: RagSource[]
  knowledge_web?: KnowledgeWebSources
}

