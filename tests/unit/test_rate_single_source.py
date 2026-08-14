"""낙찰하한율의 진실원이 하나임을 못 박는다 (2026-08-05 P0 회귀).

계기: 추정가격 70억 종합공사 한 번의 질의에 **같은 응답 안에서** 두 값이 나왔다.

    CST_001 (50억↑, 금액 구간별 5단 표)      → 87.495%
    CST_007 (4억~100억, 단일 상수 하나)      → 89.745%

구간마다 바뀌는 값을 평률로 덮었으니 구조적으로 틀릴 수밖에 없었다. 2.25%p면 70억
공사의 투찰 하한이 **1.58억** 어긋나고, 투찰은 되돌릴 수 없다. 발주자가 이 값을 쓰면
적법한 입찰을 무효 처리하고, 입찰자가 쓰면 하한 미달로 실격되거나 낙찰가를 과다 산정한다.

**이 결함을 22문항 평가셋·MCP 회귀 20건·public 시나리오 41건이 전부 놓쳤다** —
공사 낙찰하한율을 채점하는 항목이 하나도 없었기 때문이다(요율 항목은 labor-rate 하나).
그래서 "값이 맞나"가 아니라 **"두 경로가 같은 말을 하나"**를 불변식으로 박는다.
값의 정오는 법령 원문 대조의 몫이고, 이 테스트는 그와 무관하게 항상 유효하다.
"""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.services.rule_engine import RuleEngine  # noqa: E402

RULES = str(ROOT / "rules" / "contract_rules.json")

# 구간 경계와 그 사이를 함께 훑는다 — 경계에서만 갈라지는 결함이 흔하다.
SWEEP = [
    100_000_000, 199_999_999, 200_000_000, 299_999_999, 300_000_000,
    400_000_000, 999_999_999, 1_000_000_000, 4_999_999_999, 5_000_000_000,
    7_000_000_000, 9_999_999_999, 10_000_000_000, 30_000_000_000,
]


@pytest.fixture(scope="module")
def engine():
    return RuleEngine(RULES)


def _rates(engine, price, specialty="general"):
    """이 금액에 매칭되는 공사 룰들이 내놓는 (rule_id, 하한율) 목록."""
    out = []
    for rule in engine.match({
        "estimated_price": price,
        "contract_type": "construction",
        "construction_specialty": specialty,
    }):
        info = engine.get_pass_score(rule, price)
        rate = info.get("lower_limit_rate")
        if rate is not None:
            out.append((rule["rule_id"], rate))
    return out


@pytest.mark.parametrize("price", SWEEP)
def test_같은_금액에_매칭되는_룰들은_같은_하한율을_말한다(engine, price):
    """두 경로가 다른 값을 주면 사용자는 어느 쪽이 맞는지 알 방법이 없다."""
    rates = _rates(engine, price)
    distinct = {r for _, r in rates}
    assert len(distinct) <= 1, (
        f"추정가격 {price:,}원에서 하한율이 갈렸다: "
        + ", ".join(f"{rid}={rate * 100:.3f}%" for rid, rate in rates)
    )


def test_70억_종합공사는_87_495퍼센트다(engine):
    """P0 재현 지점 자체를 못 박는다.

    값의 근거: 조달청 시설공사 적격심사세부기준 별표(50억~100억 미만) — 통과 95점,
    비가격 만점 50점이라 가격 45점 필요, 산식 `50-2×|90-x|`에서 `|90-x|=2.5` → 87.495%.
    개정으로 값이 바뀌면 이 테스트가 먼저 깨지는 것이 맞다(조용히 바뀌는 것보다 낫다).
    """
    rates = _rates(engine, 7_000_000_000)
    assert rates, "70억 종합공사에 매칭되는 공사 룰이 없다"
    for rid, rate in rates:
        assert rate == pytest.approx(0.87495), f"{rid}가 {rate * 100:.3f}%를 반환"


def test_평률_상수가_요율_경계를_가로지르지_않는다(engine):
    """평률 상수 자체는 죄가 아니다 — **자기 구간이 요율 경계를 가로지르는데** 평률이면 죄다.

    CST_002(10억~50억)는 그 구간 내내 88.745% 하나라 상수로 두어도 맞다.
    CST_007은 4억~100억을 덮으면서 상수 하나였는데, 그 안에서 정본 구간표는 요율이
    세 번 바뀐다(89.745 → 88.745 → 87.495). 그래서 반드시 어딘가는 틀렸다.
    이 테스트는 그 '가로지름'만 잡는다 — 좁은 구간의 정당한 상수를 오탐하지 않는다.
    """
    canon = None
    for r in engine._data["rules"]:
        if r.get("rule_id") == "CST_001":
            canon = (r.get("result", {}) or {}).get("pass_score_by_amount")
    assert canon, "정본 구간표(CST_001)를 찾을 수 없다"
    tiers = sorted(((int(k.split("_", 1)[1]), v) for k, v in canon.items()), reverse=True)

    def canon_rate(price):
        for threshold, info in tiers:
            if price >= threshold:
                return info.get("lower_limit_rate")
        return tiers[-1][1].get("lower_limit_rate")

    offenders = []
    for rule in engine._data["rules"]:
        if rule.get("contract_type") != "construction":
            continue
        res = rule.get("result", {}) or {}
        if res.get("lower_limit_rate") is None:
            continue                       # 구간표·참조를 쓰는 룰은 대상 아님
        cond = rule.get("conditions", {}) or {}
        lo = cond.get("estimated_price_gte", cond.get("estimated_price_gt", 0))
        hi = cond.get("estimated_price_lt", cond.get("estimated_price_lte"))
        # 상한이 없으면 정본 표의 최상단 구간까지 본다
        probes = [lo] + [t for t, _ in tiers if t > lo and (hi is None or t < hi)]
        seen = {canon_rate(p) for p in probes}
        seen.discard(None)
        if len(seen) > 1:
            offenders.append(
                f"{rule['rule_id']}({lo:,}~{hi and f'{hi:,}' or '∞'}: "
                + "/".join(f"{r * 100:.3f}%" for r in sorted(seen)) + ")")
    assert not offenders, (
        "자기 구간이 요율 경계를 가로지르는데 평률 상수를 쓴 공사 룰: " + ", ".join(offenders)
        + " — 구간표(pass_score_by_amount)나 참조(pass_score_ref)로 바꿔야 한다."
    )


def test_참조가_실재하는_룰을_가리킨다(engine):
    """참조가 깨지면 하한율이 조용히 None이 되어 '값 없음'으로 새어나간다."""
    ids = {r.get("rule_id") for r in engine._data["rules"]}
    for rule in engine._data["rules"]:
        ref = (rule.get("result", {}) or {}).get("pass_score_ref")
        if ref:
            assert ref in ids, f"{rule['rule_id']}의 pass_score_ref={ref}가 존재하지 않는다"
            assert engine._resolve_pass_score_ref(rule["result"]), \
                f"{rule['rule_id']}의 참조 {ref}에 pass_score_by_amount가 없다"


# ── /ask 결정론 주입 (2026-08-05 P0 후반부) ──────────────────────────────────
# 룰엔진을 고쳐도 챗봇은 여전히 RAG로 답한다. 코퍼스의 `(감사원)공공계약 실무가이드.pdf`는
# **개정 전 세대**라 85.495%가 14회 나오고 현행 값(87.495/88.745/89.745)은 0회다.
# 청크에 발간연도·시행일 메타가 없어 신선도로 거를 수도 없어서, 어느 청크가 rerank 1위를
# 먹느냐에 답이 달렸다 — 같은 질문에 회차마다 답이 바뀐 이유가 이것이다.
# 요율은 검색할 것이 아니라 금액에서 계산되는 값이므로 룰엔진 답을 [확정 사실]로 주입한다.

from backend.api.v1.ask import (  # noqa: E402
    _deterministic_rate_block,
    _parse_amount,
)


@pytest.mark.parametrize("q,expected", [
    ("추정가격 70억원 종합공사의 적격심사 낙찰하한율은?", "87.495%"),
    ("30억 공사 낙찰하한율 알려줘", "88.745%"),
    ("5억원 공사 낙찰하한율은?", "89.745%"),
    ("7,000,000,000원 공사의 낙찰하한율은?", "87.495%"),
])
def test_공사_요율_질문엔_결정론_값이_주입된다(q, expected):
    block = _deterministic_rate_block(q)
    assert "[확정 사실" in block, f"주입되지 않았다: {q}"
    assert expected in block, f"{q} → {block!r}"
    # 구 자료의 값이 확정 사실로 새어나가면 안 된다
    assert "85.495" not in block


@pytest.mark.parametrize("q", [
    "공사 낙찰하한율이 뭐야?",              # 금액 없음
    "70억이랑 30억 공사 낙찰하한율 차이는?",  # 금액 2개 — 어느 쪽인지 모른다
    "단순노무용역 소액수의 낙찰하한율은?",     # 공사 아님
    "70억 공사 계약방법 알려줘",             # 요율 질문 아님
])
def test_애매하면_주입하지_않는다(q):
    """틀린 주입은 지금보다 나쁘다 — 확정이 아닌 것을 확정이라 부르지 않는다."""
    assert _deterministic_rate_block(q) == "", q


def test_금액_파싱은_하나일_때만_확정한다():
    assert _parse_amount("70억원") == 7_000_000_000
    assert _parse_amount("1.5억") == 150_000_000
    assert _parse_amount("7,000,000,000원") == 7_000_000_000
    assert _parse_amount("금액 없음") is None
    assert _parse_amount("70억과 30억") is None


def test_시스템_프롬프트가_확정사실_우선을_지시한다():
    """주입만 하고 프롬프트가 침묵하면 LLM이 검색 문서를 골라도 막을 근거가 없다."""
    src = (ROOT / "backend" / "api" / "v1" / "ask.py").read_text(encoding="utf-8")
    prompt = src.split('_SYSTEM_PROMPT = """')[1].split('"""')[0]
    assert "[확정 사실]" in prompt, "확정 사실 우선 규칙이 프롬프트에 없다"


# ── 별표 번호는 원문 매핑을 따른다 (2026-08-05 F3 수리) ──────────────────────
# 계기: 70억 응답 하나에 `method: "…(적격심사 별표4)"`와 `byeolpyo: "별표3"`이 공존했다.
# 원문 대조 결과 우리 표기가 **정확히 +2 밀려** 있었고, 그래서 평가기준 자리에서
# **경영상태 평가기준 표**를 가리키고 있었다(별표6~9가 경영상태다).
#
# 정본: 조달청 시설공사 적격심사세부기준 제2조제1항 + 별표 실제 표제
#   (tools/law_tables/byl_ppa_cst_qual.pdf p1 제2조 / p5·8·10·12·13 표제)
#   [별표1] 100억 미만 50억 이상        [별표6] 〃 — 경영상태
#   [별표2] 50억 미만 10억 이상(전기등 3억↑)  [별표7] 〃 — 경영상태
#   [별표3] 10억 미만 3억 이상          [별표8] 〃 등 — 경영상태
#   [별표4] 3억 미만 2억 이상(전기등 8천만↑)  [별표9] 〃 — 경영상태
#   [별표5] 2억 미만(전기등 8천만 미만)
#
# 값(요율)이 아니라 **근거 표기**라 조용히 틀려도 아무 게이트가 안 걸렸다.
# 사용자가 "별표6"을 믿고 기준을 펴면 무관한 표가 나온다.

# 종합·전문(건설산업기본법 건설공사)
_GEN_BYEOLPYO = [
    (7_000_000_000, "별표1"), (5_000_000_000, "별표1"),
    (3_000_000_000, "별표2"), (1_000_000_000, "별표2"),
    (500_000_000, "별표3"), (300_000_000, "별표3"),
    (250_000_000, "별표4"), (200_000_000, "별표4"),
    (100_000_000, "별표5"), (0, "별표5"),
]
_SPECIALTY = {"CST_ELEC_001", "CST_ICT_001", "CST_FIRE_001", "CST_HERITAGE_001"}
# 경영상태 평가기준 — 평가기준 자리에 오면 안 되는 번호들
_MGMT_ONLY = {"별표6", "별표7", "별표8", "별표9", "별표10"}


@pytest.mark.parametrize("price,expected", _GEN_BYEOLPYO)
def test_종합공사_별표는_원문_매핑을_따른다(engine, price, expected):
    for rule in engine.match({
        "estimated_price": price, "contract_type": "construction",
        "construction_specialty": "general",
    }):
        bp = engine.get_pass_score(rule, price).get("byeolpyo")
        if not bp:
            continue
        assert expected in bp, (
            f"{rule['rule_id']} @ {price:,}원 → {bp} (원문 매핑은 {expected})")


def test_평가기준_자리에_경영상태_별표가_오지_않는다(engine):
    """별표6~10은 경영상태 평가기준표다 — 평가기준(별표1~5) 자리에 오면 다른 표를 편다."""
    bad = []
    for rule in engine._data["rules"]:
        if rule.get("contract_type") != "construction":
            continue
        if rule["rule_id"].startswith(("LOCAL_", "CST_PQ")):
            continue        # 행안부 기준·사전심사 기준은 별표 체계가 다르다
        for key, info in ((rule.get("result", {}) or {}).get("pass_score_by_amount") or {}).items():
            bp = info.get("byeolpyo") or ""
            for m in _MGMT_ONLY:
                if m in bp:
                    bad.append(f"{rule['rule_id']}.{key}={bp}")
    assert not bad, "평가기준 자리에 경영상태 별표: " + ", ".join(bad)


def test_한_룰_안에서_별표_표기가_어긋나지_않는다(engine):
    """P0의 자기모순 지점 — 같은 금액 구간에 method는 별표4, byeolpyo는 별표3이었다."""
    for rule in engine._data["rules"]:
        res = rule.get("result", {}) or {}
        psba = res.get("pass_score_by_amount") or {}
        for field in ("method_by_amount", "bidder_selection_by_amount"):
            for key, text in (res.get(field) or {}).items():
                nums = re.findall(r"별표\s*(\d+)", text or "")
                ref = (psba.get(key) or {}).get("byeolpyo") or ""
                ref_nums = re.findall(r"별표\s*(\d+)", ref)
                if nums and ref_nums:
                    assert set(nums) & set(ref_nums), (
                        f"{rule['rule_id']}.{field}.{key}: {text!r} vs byeolpyo {ref!r}")
