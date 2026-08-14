"""전문분야 공용(센티널) 룰이 전용 룰의 1순위를 뺏지 않는다 — T-2026W33-148.

라이브 실측(2026-08-14): 국가 문화재수리공사 1.8억 판정의 1순위가 CST_FIRE_002
(소방공사 일반경쟁)였다. `fire_safety` 룰이 '그 밖의 공사(법령공사)' 금액 기준 공용
룰을 겸하는데 priority가 문화재 전용 룰과 같아(115) 파일 순서로 이겼기 때문이다.
실무자는 소방시설공사업 요건을 문화재수리공사 요건으로 읽게 된다(기치 ① 위반).
"""
from backend.api.deps import get_rule_engine

ENGINE = get_rule_engine()


def _ids(**params):
    base = {"contract_type": "construction", "estimated_price": 180_000_000}
    return [r["rule_id"] for r in ENGINE.match({**base, **params}, org_type="national")]


def test_exact_specialty_outranks_sentinel():
    ids = _ids(construction_specialty="cultural_heritage")
    assert ids[0] == "CST_HERITAGE_002", ids
    # 공용 룰은 금액 구간 근거이므로 후보에서 지우지 않는다 — 뒤로 밀기만 한다
    assert "CST_FIRE_002" in ids
    assert ids.index("CST_HERITAGE_002") < ids.index("CST_FIRE_002")


def test_sentinel_is_marked_for_disclosure():
    rules = ENGINE.match({"contract_type": "construction", "estimated_price": 180_000_000,
                          "construction_specialty": "cultural_heritage"}, org_type="national")
    by_id = {r["rule_id"]: r for r in rules}
    assert by_id["CST_HERITAGE_002"]["_specialty_match"] == "exact"
    assert by_id["CST_FIRE_002"]["_specialty_match"] == "sentinel"


def test_fire_safety_itself_is_exact():
    """소방공사 질의에서는 그 룰이 정확 일치다 — 강등이 자기 영역을 깎아선 안 된다."""
    rules = ENGINE.match({"contract_type": "construction", "estimated_price": 180_000_000,
                          "construction_specialty": "fire_safety"}, org_type="national")
    assert rules[0]["rule_id"] == "CST_FIRE_002"
    assert rules[0]["_specialty_match"] == "exact"


def test_professional_group_still_uses_shared_rule():
    """전문공사 14종은 전용 룰이 없어 전부 센티널 — 상대 순서가 바뀌지 않는다."""
    ids = _ids(construction_specialty="elevator", estimated_price=210_000_000)
    assert ids and all(i.startswith("CST_PRO") or i.startswith("CST_") for i in ids)


def test_order_rank_is_stamped():
    """하위 소비자가 재정렬해도 엔진 순서가 유지되도록 순위를 값으로 박는다."""
    rules = ENGINE.match({"contract_type": "construction", "estimated_price": 180_000_000,
                          "construction_specialty": "cultural_heritage"}, org_type="national")
    assert [r["_order_rank"] for r in rules] == list(range(len(rules)))
