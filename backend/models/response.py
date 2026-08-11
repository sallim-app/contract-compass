from pydantic import BaseModel
from typing import Any


class RagSource(BaseModel):
    chunk_id: str
    section_title: str
    excerpt: str           # 200자 미리보기 (검색 매칭 강조용 — SourceDrawer highlight 인자로 사용)
    content: str = ""      # 청크 전체 본문 (2026-05-31 추가) — Drawer에 원문 표시
    relevance_score: float
    source_type: str = "guide"
    document_id: str = ""


class KnowledgeWebSources(BaseModel):
    textbook: list[RagSource] = []
    guide: list[RagSource] = []
    law: list[RagSource] = []


class Candidate(BaseModel):
    rank: int
    method: str
    rule_id: str
    confidence: float
    summary: str
    key_params: dict[str, Any]
    clarifying_questions: list[str] = []
    # 2026-06-02 F7-1: 낙찰자결정방법(시행령 제42조) 추가 후보 — 계약방법과 별개
    bidder_options: list[dict[str, Any]] = []
    bidder_selection: str | None = None
    # F4-1 e2e: 매칭된 룰의 legal_basis 노출 (대기업 참여 제한·SW진흥법 등 검증용)
    legal_basis: list[str] = []
    # 2026-08-12 R23(T-2026W33-58): 룰 notes(요건 경고 — "2천만 초과~1억은 상대방 요건
    # 충족 시만 수의")가 룰 파일에만 있고 응답에 실리지 않아 정적 SEO 페이지만 경고를
    # 노출하는 계층 간 자기모순이 있었다. 후보마다 notes를 배달한다.
    notes: str | None = None


class NextStepQuestion(BaseModel):
    id: str
    text: str
    description: str = ""   # 이 조건이 무엇인지, 왜 묻는지 쉬운 말로 설명
    if_yes: str = ""         # "예" 선택 시 어떤 계약방법/조건이 적용되는지
    type: str  # "boolean" | "select"
    options: list[str] = []
    # F13-2 (2026-06-09): YES 선택 시 노출되는 sub-options (예: 공동도급 4종 — 공동이행만/분담이행만/둘다/미허용)
    # 각 option: {value: str, label: str, desc: str, law: str}
    sub_options: list[dict] = []


class PracticeAlternative(BaseModel):
    """실무 옵션 — 1순위 추천 외에도 사정·사유에 따라 선택 가능한 계약방법.

    rule.result.alternatives 필드에서 가져옴. 사용자 선택 UI는 Phase 2.
    """
    method: str        # 예: '수의계약(시담)'
    reason: str        # 어떤 사유 시 선택 가능한지 (예: '정비·보수·감리 등 사유 시')
    kind: str          # negotiated/small_negotiated_electronic/designated_competitive 등


class Step1Response(BaseModel):
    session_id: str
    step: int = 1
    candidates: list[Candidate]
    practice_alternatives: list[PracticeAlternative] = []  # 실무 다양성 옵션
    rag_sources: list[RagSource]
    knowledge_web: KnowledgeWebSources = KnowledgeWebSources()
    next_step_questions: list[NextStepQuestion]
    # F28 (2026-06-10): Step2 화면에서도 Step3와 동일한 결정론 자료 팩 노출.
    # 사용자 의견 "2단계 참조근거가 3단계와 다른데" — 1순위 룰의 decision_pack을 미리 전달.
    decision_pack: dict = {}


class FinalRecommendation(BaseModel):
    method: str
    rule_id: str
    details: dict[str, Any]
    public_procurement_obligations: list[dict]
    legal_basis: list[str]
    ai_rationale: str
    confidence: float
    form_prefill: dict[str, str]
    # F13-5 (2026-06-09): step2에서 실제 적용된 additional_conditions 노출 (사용자 선택 무시 방지)
    applied_conditions: dict = {}
    selection_ignored_reason: str | None = None
    # Q15 (2026-06-13): _LAW_REGISTRY 키 배열 (legal_basis는 자연어라 Step4 체크박스·docx 필터와
    # 형식 불일치 → 법령 본문 누락). decision_pack.laws_applied[].key에서 결정론적으로 채움.
    legal_basis_keys: list[str] = []
    # F19 (2026-06-09): LLM에 전달된 결정론 자료 팩을 사용자에게도 노출 (투명성).
    # {rule, laws_applied[{key, law_name, articles[{title, body}]}]}
    decision_pack: dict = {}


class Step2Response(BaseModel):
    session_id: str
    step: int = 2
    final_recommendation: FinalRecommendation
    rag_sources: list[RagSource]
    knowledge_web: KnowledgeWebSources = KnowledgeWebSources()


class FormPreview(BaseModel):
    fields: dict[str, str]


class FormGenerateResponse(BaseModel):
    form_id: str
    download_url: str
    preview: FormPreview


class DocSearchResponse(BaseModel):
    results: list[RagSource]
