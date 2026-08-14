"""물가변동 계약금액 조정 엔드포인트 — 이행단계 축 Phase 3. 무LLM 결정론.

판정·산식은 `backend/services/price_adjustment.py`(순수 함수)가 하고 여기서는 HTTP 계약만.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.services.price_adjustment import PriceAdjustmentInputError, check

router = APIRouter(prefix="/adjustment", tags=["adjustment"])


class PriceAdjustmentRequest(BaseModel):
    org_type: str = Field(..., description="national|local|public_corp — 단품 문턱이 다르므로 필수")
    contract_date: str = Field(..., description="계약체결일 YYYY-MM-DD(장기계속은 제1차계약 체결일)")
    check_date: str = Field(..., description="조정 검토·청구 시점 YYYY-MM-DD")
    last_adjustment_date: str | None = Field(None, description="직전 조정기준일(있으면 기간 기산점)")
    adjustment_rate_pct: float | None = Field(None, description="산정된 품목·지수 조정률(%) — 서버는 산정 불가")
    method_specified_in_contract: str | None = Field(None, description="item|index — 계약서 명시 방식")
    urgent_exception: bool = Field(False, description="천재지변·원자재 급등 예외 검토 여부")
    single_item_rate_pct: float | None = Field(None, description="단품 자재 가격증감률(%)")
    single_item_share_over_5permille: bool | None = Field(
        None, description="해당 자재가 재료비·노무비·경비 합계액의 1천분의 5를 초과하는가")
    is_construction: bool = Field(False, description="공사계약인가(단품 조정은 공사 전용)")
    adjustment_base_amount: int | None = Field(None, description="물가변동적용대가(원)")
    advance_payment_ratio: float | None = Field(None, description="선금급률(비율, 예: 0.3)")


@router.post("/price")
def price_adjustment(req: PriceAdjustmentRequest) -> dict:
    try:
        return check(**req.model_dump())
    except PriceAdjustmentInputError as e:
        raise HTTPException(status_code=400, detail={
            "error": e.code, "message": e.message, "hint": e.hint}) from e
