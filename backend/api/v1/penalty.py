"""지체상금·지연배상금 산정 엔드포인트 — 무LLM 결정론.

계산은 `backend/services/delay_penalty.py`(순수 함수)가 하고 여기서는 HTTP 계약만 맡는다.
정적 SEO 페이지 생성기·CI 테스트도 같은 함수를 쓰게 해 사본이 갈리지 않게 한다
(rule_engine.rule_method를 서비스 계층으로 내린 것과 같은 이유).
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.services.delay_exemption import (DelayExemptionInputError, ground_ids,
                                              guide)
from backend.services.delay_penalty import (CONTRACT_KINDS, ORG_TYPES,
                                            DelayPenaltyInputError, compute)

router = APIRouter(prefix="/penalty", tags=["penalty"])


class DelayPenaltyRequest(BaseModel):
    contract_kind: str = Field(..., description=f"계약유형 — {', '.join(CONTRACT_KINDS)}")
    org_type: str = Field(..., description=f"기관유형 — {', '.join(ORG_TYPES)}. 요율이 다르므로 필수")
    contract_amount: int = Field(..., description="계약금액(원). 장기계속계약은 연차별 계약금액")
    delay_days: int = Field(..., description="지체일수(선언값)")
    excluded_days: int = Field(0, description="계약상대자 책임 없는 사유 일수(선언값)")
    accepted_portion_amount: int = Field(0, description="검사·인수된 기성·기납 부분 금액(원)")
    design_build_approved: bool = Field(
        False, description="설계·제조 일괄 + 발주기관 승인 물품(요율 예외 적용 대상)")


@router.post("/delay")
def delay_penalty(req: DelayPenaltyRequest) -> dict:
    try:
        return compute(
            contract_kind=req.contract_kind,
            org_type=req.org_type,
            contract_amount=req.contract_amount,
            delay_days=req.delay_days,
            excluded_days=req.excluded_days,
            accepted_portion_amount=req.accepted_portion_amount,
            design_build_approved=req.design_build_approved,
        )
    except DelayPenaltyInputError as e:
        # 구조화 실패 — 에이전트가 hint의 행동지침을 따를 수 있게 한다(이 저장소 규약).
        raise HTTPException(status_code=400, detail={
            "error": e.code, "message": e.message, "hint": e.hint}) from e


@router.get("/delay/exemptions")
def delay_exemptions(contract_kind: str, ground: str | None = None) -> dict:
    """지체일수 불산입(면책) 사유 지도 — Phase 2. 판정하지 않고 갈림길만 편다."""
    try:
        return guide(contract_kind=contract_kind, ground=ground)
    except DelayExemptionInputError as e:
        raise HTTPException(status_code=400, detail={
            "error": e.code, "message": e.message, "hint": e.hint,
            "available_grounds": ground_ids()}) from e
