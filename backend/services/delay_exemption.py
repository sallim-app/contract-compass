"""지체일수 불산입(면책) 사유 갈림길 지도 — Phase 2. 순수 조회·조립, 외부 의존 0.

Phase 1(`delay_penalty.py`)이 "계산은 하되 지체일수는 정하지 않는다"고 거부한 자리에
**대신 줄 것**을 놓는다(realty-mcp의 '판정 거부 후 갈림길 지도' 패턴). 판정하지 않고
길을 준다: 어떤 사유가 예규에 있는지, 각 사유가 인정되려면 무엇이 확정돼야 하는지,
선례가 무엇을 말했는지.

이 모듈이 지키는 인식 경계 계약:

1. **판정하지 않는다.** 일반조건 문언 자체가 "계약담당공무원이 인정할 때"를 요건으로
   두므로, 해당 여부는 발주기관의 판단이다. 응답에 그 사실을 매번 싣는다.
2. **자연어를 분류하지 않는다.** "상황을 적으면 사유를 골라주는" 인터페이스는 조용한
   오답 표면을 넓힌다(mcp-tool-design §3) — 사유는 목록에서 id로 고른다.
3. **끊긴 인용을 이어 붙이지 않는다.** 코퍼스 회수분이 중간에서 끊긴 항목은
   quote_truncated로 표시된 채 그대로 나가고, 전문 확인 경로를 함께 준다.
4. **계약유형↔일반조건 매핑은 우리 편의**다 — 계약서에 실제로 편입된 일반조건이
   진실원이라는 경고를 함께 낸다.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

RULES_PATH = Path(__file__).resolve().parents[2] / "rules" / "delay_exemption_map.json"


class DelayExemptionInputError(ValueError):
    def __init__(self, code: str, message: str, hint: str):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


@lru_cache(maxsize=1)
def _map() -> dict:
    with open(RULES_PATH, encoding="utf-8") as f:
        return json.load(f)


def ground_ids() -> list[str]:
    return [g["id"] for g in _map()["grounds"]]


def guide(*, contract_kind: str, ground: str | None = None) -> dict[str, Any]:
    """계약유형별 불산입 사유 지도. ground를 주면 그 사유 하나를 상세히."""
    m = _map()
    family = m["kind_to_family"].get(contract_kind)
    if not family:
        raise DelayExemptionInputError(
            "unknown_contract_kind",
            f"계약유형 '{contract_kind}'을 모른다. 가능한 값: {', '.join(m['kind_to_family'])}",
            "estimate_delay_penalty와 같은 contract_kind를 쓴다 — 어떤 계약인지 사용자에게 확인하라.")

    grounds = [g for g in m["grounds"] if family in g["families"]]
    if ground is not None:
        picked = [g for g in grounds if g["id"] == ground]
        if not picked:
            all_ids = [g["id"] for g in m["grounds"]]
            in_other = ground in all_ids
            raise DelayExemptionInputError(
                "ground_not_applicable" if in_other else "unknown_ground",
                (f"'{ground}'은(는) {m['families'][family]['label']} 계약의 불산입 사유 목록에 없다."
                 if in_other else f"'{ground}'은(는) 없는 사유 id다."),
                (f"이 계약유형에서 가능한 사유: {', '.join(g['id'] for g in grounds)}. "
                 "ground 없이 호출하면 전체 목록이 온다."))
        grounds = picked

    # 선례는 사유에 걸린 것 + 계약유형 무관한 일수 계산 선례 전부
    linked = {p for g in grounds for p in (g.get("precedents") or [])}
    precedents = [p for p in m["precedents"] if ground is None or p["id"] in linked]

    return {
        "contract_kind": contract_kind,
        "family": family,
        "general_conditions_applied": m["families"][family]["general_conditions"],
        "general_conditions_warning": m["kind_mapping_note"],
        "grounds": grounds,
        "grounds_count": len(grounds),
        "day_count_rules": m["day_count_rules"],
        "precedents": precedents,
        "who_decides": m["who_decides"],
        "next_steps": [
            "해당할 만한 사유의 must_establish를 사용자와 하나씩 확인하라 — 답이 안 나오면 "
            "그 사실을 확정하는 것이 다음 할 일이다(추정으로 채우지 말 것).",
            "인용 전에 quote_truncated=true 항목은 search_references로 전문을 확인하라.",
            "불산입 일수가 정해지면 estimate_delay_penalty의 excluded_days에 넣어 다시 계산하라 "
            "— sw_requirement_change는 해당 일수의 1/2만 넣는다.",
        ],
        "sources": m["sources"],
        "uncertainties": m["uncertainties"],
        "rules_updated": m["last_updated"],
    }
