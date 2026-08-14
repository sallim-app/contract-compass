"""'이하' 상한 경계 정확값에서 1순위가 1원 차이로 뒤집히지 않는지 — T-2026W33-99.

배경(2026-08-13 본선 재현): 국가 물품 + 소기업·소상공인 요건 선언 건이
99,999,999원엔 소액수의(PRD_NEGO_SMALLBIZ), 정확히 100,000,000원엔 일반경쟁
(PRD_003B)으로 뒤집혔다. 시행령 제26조①5호가목3) 원문이 '2천만원 초과 1억원
**이하**'라 1억 정확값도 요건 구간에 포함되는데, 경쟁 룰이 같은 지점에서
'이상'(_gte)으로 열리면서 단 한 점의 조건 중복이 생겼고 경쟁 룰의 priority가
앞서 1순위를 가져갔다.

여기서 고정하는 성질은 **스냅샷이 아니라 불변식**이다 — `boundary_inclusive_rank`
룰이 매칭되는 경계 정확값에서, 경계 직전의 1순위 룰이 **거기서도 여전히 매칭된다면**
1순위를 유지해야 한다. 상한이 '미만'이라 경계에서 적법하게 소멸하는 룰(판로지원법
소기업 제한경쟁)까지 붙잡지는 않는다 — 상세는 아래 테스트 독스트링.

개별 기대값은 tests/scenarios_public.json이 따로 못박는다(이 둘은 서로를
대체하지 않는다: 스냅샷은 '무엇이 나오는가', 이 파일은 '왜 뒤집히면 안 되는가').
"""
import itertools
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

from backend.services.rule_engine import RuleEngine  # noqa: E402

# 요건 선언 플래그 — 이게 있어야 요건부 수의 룰이 매칭된다
FLAGS = [
    "small_enterprise_restriction",   # 가목3) 소기업·소상공인
    "is_preferential_enterprise",     # 가목5) 여성·장애인·사회적기업 등
    "is_youth_startup",               # 가목7) 청년창업기업 (5천만원 이하)
    "is_sme_competition_product",     # 중기간 경쟁제품 (경계와 무관하나 조합 노출용)
]
SERVICE_TYPES = [None, "academic", "it_service", "facility", "other"]


@pytest.fixture(scope="module")
def engine():
    return RuleEngine(str(ROOT / "rules" / "contract_rules.json"))


@pytest.fixture(scope="module")
def opted_in_bounds(engine):
    """`boundary_inclusive_rank`를 선언한 룰들의 상한 금액 집합."""
    rules = json.loads((ROOT / "rules" / "contract_rules.json").read_text(encoding="utf-8"))["rules"]
    bounds = {r["conditions"]["estimated_price_lte"]
              for r in rules if r.get("boundary_inclusive_rank")}
    assert bounds, "boundary_inclusive_rank 룰이 하나도 없다 — 경계 보정이 통째로 사라졌다"
    return sorted(bounds)


def _combos():
    yield {}
    for f in FLAGS:
        yield {f: True}
    for a, b in itertools.combinations(FLAGS, 2):
        yield {a: True, b: True}


def _params(ct, price, combo, service_type):
    p = {"contract_type": ct, "estimated_price": price, **combo}
    if ct == "service" and service_type:
        p["service_type"] = service_type
    return p




@pytest.mark.parametrize("org_type", ["national", "public_corp", "local"])
@pytest.mark.parametrize("contract_type", ["product", "service"])
def test_matching_rule_is_never_demoted_at_boundary(engine, opted_in_bounds, org_type, contract_type):
    """경계 직전의 1순위 룰이 경계에서도 **여전히 매칭되면** 1순위를 유지해야 한다.

    1순위가 바뀌는 것 자체는 정상일 수 있다 — 판로지원법 시행령 제2조의2의
    소기업·소상공인 제한경쟁(PRD_004·SVC_SMALL_BIZ_001)은 '추정가격 1억원
    **미만**'이라 1억 정확값에서 적법하게 매칭이 끝난다. 그건 법령이 만든
    절벽이지 룰 인코딩의 결함이 아니다. 결함은 **아직 적용되는 룰이 그 지점에서
    비로소 열린 룰에게 1순위를 빼앗기는 것**이고, 여기서 잡는 건 그쪽이다.
    """
    cliffs = []
    for bound in opted_in_bounds:
        for service_type in (SERVICE_TYPES if contract_type == "service" else [None]):
            for combo in _combos():
                at = _params(contract_type, bound, combo, service_type)
                matched_at = engine.match(dict(at), org_type=org_type)
                # 이 입력에서 실제로 옵트인 룰이 경계 상한으로 걸릴 때만 검사 대상
                if not any(r.get("boundary_inclusive_rank")
                           and r.get("conditions", {}).get("estimated_price_lte") == bound
                           for r in matched_at):
                    continue
                before = engine.match(
                    _params(contract_type, bound - 1, combo, service_type), org_type=org_type)
                if not before:
                    continue
                top_before = before[0]["rule_id"]
                ids_at = [r["rule_id"] for r in matched_at]
                # 경계에서 매칭이 끝난 룰(상한이 '미만')은 적법한 소멸 — 검사 제외
                if top_before not in ids_at:
                    continue
                if ids_at[0] != top_before:
                    cliffs.append(
                        f"{org_type}/{contract_type}/{service_type}/{sorted(combo)} "
                        f"{bound - 1:,}원 1순위={top_before} → {bound:,}원 1순위={ids_at[0]} "
                        f"(밀려난 {top_before}는 {bound:,}원에도 여전히 매칭)")
    assert not cliffs, "경계 정확값에서 아직 적용되는 룰이 1순위를 빼앗긴다:\n  " + "\n  ".join(cliffs)


def test_reported_case_is_negotiated_at_exactly_100m(engine):
    """제보 재현 그대로 — 국가 물품 + 소기업 요건, 정확히 1억원."""
    matched = engine.match(
        {"contract_type": "product", "estimated_price": 100_000_000,
         "small_enterprise_restriction": True}, org_type="national")
    assert matched, "후보가 0건"
    assert matched[0]["rule_id"] == "PRD_NEGO_SMALLBIZ"
    assert matched[0]["result"]["method"] == "소액수의계약"
    # 일반경쟁은 사라지지 않고 2순위 대안으로 남아야 한다(요건 활용은 임의)
    assert "PRD_003B" in [r["rule_id"] for r in matched], "일반경쟁 후보가 통째로 빠졌다"


def test_boundary_fix_does_not_leak_outside_the_boundary(engine):
    """요건 미선언·경계 밖은 종전 판정 그대로 — 보정이 구간 전체로 새지 않는다."""
    # 요건 미선언 1억 정확값: 종전대로 일반경쟁
    no_flag = engine.match(
        {"contract_type": "product", "estimated_price": 100_000_000}, org_type="national")
    assert no_flag[0]["rule_id"] == "PRD_003B"

    # 구간 내부(경계 아님)는 기존 우선순위 그대로 — 정보화사업 5천만~2.3억 룰이 유지된다
    interior = engine.match(
        {"contract_type": "service", "estimated_price": 70_000_000,
         "service_type": "it_service", "small_enterprise_restriction": True},
        org_type="national")
    assert interior[0]["rule_id"] == "SVC_IT_002"

    # 경계를 1원이라도 넘으면 수의 구간이 끝난다
    over = engine.match(
        {"contract_type": "product", "estimated_price": 100_000_001,
         "small_enterprise_restriction": True}, org_type="national")
    assert "PRD_NEGO_SMALLBIZ" not in [r["rule_id"] for r in over]
    assert over[0]["rule_id"] == "PRD_003B"


def test_effective_priority_is_not_written_back_to_shared_rules(engine):
    """보정은 얕은 사본에만 — 원본 룰 딕셔너리가 오염되면 다음 호출로 새어나간다."""
    engine.match({"contract_type": "product", "estimated_price": 100_000_000,
                  "small_enterprise_restriction": True}, org_type="national")
    assert not any("_effective_priority" in r for r in engine._data["rules"]), \
        "룰엔진이 공유 룰 딕셔너리에 보정치를 써넣었다"
    # 경계가 아닌 호출은 보정치가 붙지 않아야 한다
    plain = engine.match({"contract_type": "product", "estimated_price": 80_000_000,
                          "small_enterprise_restriction": True}, org_type="national")
    assert not any("_effective_priority" in r for r in plain)
