"""프로젝트 현황 공개 엔드포인트 — 서비스 메타정보."""
import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

import chromadb
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.api.deps import require_admin
from backend.config import BASE_DIR, get_settings

router = APIRouter(prefix="/status", tags=["status"])

# 2026-06-02: 사용자가 룰 갭 발견하도록 공개 — /rules-public
_rules_router = APIRouter(prefix="/rules-public", tags=["rules-public"])

_ROOT = BASE_DIR
_STATUS_PATH = _ROOT / "data" / "project_status.json"
_GLOSSARY_PATH = _ROOT / "data" / "glossary.json"
_TESTS_RESULTS_DIR = _ROOT / "tests" / "results"
_FEEDBACK_LOG = _ROOT / "logs" / "feedback.jsonl"
_CHROMA_PATH = Path(get_settings().chroma_path)


class TrackItem(BaseModel):
    name: str
    progress: int  # 0~100
    status: Literal["완료", "운영중", "진행", "보류", "예정"]
    note: str = ""


class FeatureItem(BaseModel):
    id: str
    name: str
    status: Literal["운영중", "베타", "계획"]
    desc: str


class ChangelogItem(BaseModel):
    date: str
    items: list[str]


class RoadmapItem(BaseModel):
    label: str
    priority: Literal["high", "medium", "low"]


class TestCase(BaseModel):
    case_id: str
    passed: bool
    duration_ms: int | None = None


class TopicStat(BaseModel):
    topic: str
    count: int


class JudgeEvalMetrics(BaseModel):
    """RAG 검색 품질 judge 평가 (Haiku/Claude). v6·v9 등 버전별 averages."""
    version: str = ""
    faithfulness: float = 0.0
    answer_relevancy: float = 0.0
    context_precision: float = 0.0
    evaluated_at: str | None = None
    judge_variance_note: str | None = None


class StatusMetrics(BaseModel):
    test_pass: int
    test_total: int
    test_run_at: str | None
    chunk_counts: dict[str, int]
    chunk_total: int
    glossary_count: int
    feedback_count: int
    topic_top10: list[TopicStat] = []
    topic_tagged_total: int = 0
    # 추가 정량 지표 (2026-05-29)
    judge_eval: JudgeEvalMetrics | None = None
    # 사용자 의견 E2E 회귀 (2026-06-07) — tools/run_user_feedback_e2e.py 최신 실행 결과
    user_feedback_e2e_pass: int = 0
    user_feedback_e2e_total: int = 0
    user_feedback_e2e_at: str | None = None


class ArchitectureBlock(BaseModel):
    title: str
    detail: str


class IssueItem(BaseModel):
    label: str
    severity: Literal["high", "medium", "low"]
    detail: str | None = None


class MilestoneItem(BaseModel):
    """주요 마일스톤 — 큰 도약을 만든 사건. before/after 메트릭 명시."""
    date: str
    title: str
    metric_before: str | None = None
    metric_after: str | None = None
    summary: str
    impact: Literal["critical", "major", "milestone"] = "major"


class PublicStatusResponse(BaseModel):
    milestones: list[MilestoneItem] = []
    tracks: list[TrackItem] = []
    features: list[FeatureItem]
    changelog: list[ChangelogItem]
    roadmap: list[RoadmapItem]
    metrics: StatusMetrics
    test_cases: list[TestCase]
    architecture: list[ArchitectureBlock]
    known_issues: list[IssueItem]


@lru_cache(maxsize=1)
def _load_static() -> dict:
    with open(_STATUS_PATH, encoding="utf-8") as f:
        return json.load(f)


def _read_latest_test() -> tuple[int, int, str | None, list[dict]]:
    if not _TESTS_RESULTS_DIR.exists():
        return 0, 0, None, []
    # 회귀 테스트 결과는 YYYYMMDD_HHMMSS.json 형식. judge_eval_*·ledger_baseline_* 제외.
    files = sorted(
        f for f in _TESTS_RESULTS_DIR.glob("*.json")
        if not f.name.startswith(("judge_eval_", "ledger_baseline_"))
    )
    if not files:
        return 0, 0, None, []
    try:
        d = json.loads(files[-1].read_text(encoding="utf-8"))
        cases = [
            {"case_id": c.get("case_id", "?"), "passed": bool(c.get("passed")), "duration_ms": c.get("duration_ms")}
            for c in d.get("cases", [])
        ]
        return int(d.get("passed", 0)), int(d.get("total", 0)), d.get("run_at"), cases
    except Exception:
        return 0, 0, None, []


def _chroma_mtime() -> float:
    """캐시 무효화 키 — 코퍼스 재색인 시에만 바뀐다. 파일 없으면 0(캐시 1건으로 수렴)."""
    try:
        return (_CHROMA_PATH / "chroma.sqlite3").stat().st_mtime
    except OSError:
        return 0.0


# (2026-08-15 메모리 수리) 아래 두 함수는 요청마다 전 컬렉션(10만+ 메타 행)을 전수
# 스캔해 요청당 30~60MB 임시 할당을 만들었다 — 상태 페이지 폴링에 비례해 RSS가
# 성장한 주범 중 하나. 같은 파일의 다른 로더(@lru_cache, :123·:290)와 동일 규약으로
# 캐시하되, 코퍼스 재색인(chroma.sqlite3 mtime 변화)이면 재계산한다.
@lru_cache(maxsize=4)
def _chunk_counts_at(mtime: float) -> dict[str, int]:
    del mtime  # 캐시 키 전용
    if not _CHROMA_PATH.exists():
        return {}
    try:
        client = chromadb.PersistentClient(str(_CHROMA_PATH))
        return {col.name: client.get_collection(col.name).count() for col in client.list_collections()}
    except Exception:
        return {}


def _chunk_counts() -> dict[str, int]:
    return _chunk_counts_at(_chroma_mtime())


@lru_cache(maxsize=4)
def _topic_stats_at(mtime: float) -> tuple[list[dict], int]:
    """모든 컬렉션의 topics 메타데이터 집계."""
    del mtime  # 캐시 키 전용
    if not _CHROMA_PATH.exists():
        return [], 0
    try:
        client = chromadb.PersistentClient(str(_CHROMA_PATH))
        counter: dict[str, int] = {}
        tagged = 0
        for col_info in client.list_collections():
            col = client.get_collection(col_info.name)
            r = col.get(include=["metadatas"])
            for m in r.get("metadatas") or []:
                topics_str = (m.get("topics") or "").strip()
                if not topics_str:
                    continue
                tagged += 1
                for t in topics_str.split(","):
                    t = t.strip()
                    if t:
                        counter[t] = counter.get(t, 0) + 1
        top10 = sorted(counter.items(), key=lambda x: -x[1])[:10]
        return [{"topic": t, "count": n} for t, n in top10], tagged
    except Exception:
        return [], 0


def _topic_stats() -> tuple[list[dict], int]:
    return _topic_stats_at(_chroma_mtime())


def _glossary_count() -> int:
    if not _GLOSSARY_PATH.exists():
        return 0
    try:
        return len(json.loads(_GLOSSARY_PATH.read_text(encoding="utf-8")))
    except Exception:
        return 0


def _feedback_count() -> int:
    if not _FEEDBACK_LOG.exists():
        return 0
    try:
        return sum(1 for _ in _FEEDBACK_LOG.open(encoding="utf-8"))
    except Exception:
        return 0


def _user_feedback_e2e() -> tuple[int, int, str | None]:
    """tools/run_user_feedback_e2e.py 최신 실행 결과 — reports/user_feedback_e2e_latest.json"""
    p = _ROOT / "reports" / "user_feedback_e2e_latest.json"
    if not p.exists():
        return 0, 0, None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return int(d.get("passed", 0)), int(d.get("total", 0)), d.get("ts")
    except Exception:
        return 0, 0, None


def _judge_eval_latest() -> JudgeEvalMetrics | None:
    """tests/results/judge_eval_v*.json 중 가장 안정적인 v6_doc2query 우선,
    없으면 가장 최근 파일. v9는 judge 변동성이 커서 v6를 기본 표시값으로."""
    results_dir = _ROOT / "tests" / "results"
    if not results_dir.exists():
        return None
    candidates = sorted(results_dir.glob("judge_eval_*.json"))
    if not candidates:
        return None
    # 우선순위: v6_doc2query (가장 안정적인 측정), 없으면 마지막
    preferred = None
    for c in candidates:
        if "v6_doc2query" in c.name:
            preferred = c
            break
    if preferred is None:
        preferred = candidates[-1]
    try:
        d = json.loads(preferred.read_text(encoding="utf-8"))
        avg = d.get("averages", {}) or {}
        return JudgeEvalMetrics(
            version=preferred.stem,
            faithfulness=round(float(avg.get("faithfulness", 0.0)), 3),
            answer_relevancy=round(float(avg.get("answer_relevancy", 0.0)), 3),
            context_precision=round(float(avg.get("context_precision", 0.0)), 3),
            evaluated_at=d.get("evaluated_at"),
            judge_variance_note=d.get("judge_variance_note"),
        )
    except Exception:
        return None


@router.get("/public", response_model=PublicStatusResponse)
def get_public_status() -> PublicStatusResponse:
    try:
        static = _load_static()
    except FileNotFoundError:
        raise HTTPException(500, "project_status.json이 없습니다")

    passed, total, run_at, test_cases = _read_latest_test()
    chunks = _chunk_counts()
    topic_top10, topic_tagged = _topic_stats()

    return PublicStatusResponse(
        milestones=[MilestoneItem(**it) for it in static.get("milestones", [])],
        tracks=[TrackItem(**it) for it in static.get("tracks", [])],
        features=[FeatureItem(**it) for it in static.get("features", [])],
        changelog=[ChangelogItem(**it) for it in static.get("changelog", [])],
        roadmap=[RoadmapItem(**it) for it in static.get("roadmap", [])],
        metrics=StatusMetrics(
            test_pass=passed,
            test_total=total,
            test_run_at=run_at,
            chunk_counts=chunks,
            chunk_total=sum(chunks.values()),
            glossary_count=_glossary_count(),
            feedback_count=_feedback_count(),
            topic_top10=[TopicStat(**t) for t in topic_top10],
            topic_tagged_total=topic_tagged,
            judge_eval=_judge_eval_latest(),
            **dict(zip(("user_feedback_e2e_pass", "user_feedback_e2e_total", "user_feedback_e2e_at"), _user_feedback_e2e())),
        ),
        test_cases=[TestCase(**tc) for tc in test_cases],
        architecture=[ArchitectureBlock(**it) for it in static.get("architecture", [])],
        known_issues=[IssueItem(**it) for it in static.get("known_issues", [])],
    )


# ────────────────────────────────────────────────────────────────
# 2026-06-02: 룰 공개 — 사용자가 갭 발견하도록 (도메인 전문가 검수)
# ────────────────────────────────────────────────────────────────
_RULES_PATH = _ROOT / "rules" / "contract_rules.json"


@lru_cache(maxsize=1)
def _load_rules() -> list[dict]:
    if not _RULES_PATH.exists():
        return []
    return json.loads(_RULES_PATH.read_text(encoding="utf-8")).get("rules", [])


@_rules_router.get("/list")
async def public_rules_list():
    """전체 룰 요약 (rule_id·name·contract_type·조건·method·낙찰자결정·법령근거·alternatives 개수).

    사용자가 도메인 전문가로서 누락된 룰을 즉시 발견할 수 있도록 공개.
    rule body·notes 같은 메타는 제외 (간결).
    """
    rules = _load_rules()
    out = []
    for r in rules:
        res = r.get("result", {})
        out.append({
            "rule_id": r.get("rule_id", ""),
            "name": r.get("name", ""),
            "contract_type": r.get("contract_type", ""),
            "priority": r.get("priority", 0),
            "conditions": r.get("conditions", {}),
            "method": res.get("method") or res.get("method_by_amount"),
            "bidder_selection": res.get("bidder_selection"),
            "pass_score": res.get("pass_score") or res.get("pass_score_by_amount"),
            "lower_limit_rate": res.get("lower_limit_rate"),
            "alternatives_count": len(res.get("alternatives", []) or []),
            "bidder_options_count": len(res.get("bidder_options", []) or []),
            "legal_basis": r.get("legal_basis", []),
            "notes": (r.get("notes", "") or "")[:200],
        })
    # priority 내림차순 정렬 (가장 자주 매칭되는 룰부터)
    out.sort(key=lambda x: (x.get("contract_type", ""), -(x.get("priority") or 0)))
    by_ct = {}
    for r in out:
        ct = r.get("contract_type") or "기타"
        by_ct[ct] = by_ct.get(ct, 0) + 1
    return {"total": len(out), "by_contract_type": by_ct, "rules": out}


# F13-1 (2026-06-09): 금액별 계약방법 매트릭스 — 법령·예규 기준 시각화
# 사용자가 금액 입력 시 가능한 계약방법을 한눈에 검증 가능

_AMOUNT_BRACKETS = [
    {"label": "2천만 미만", "min": 0, "max": 20_000_000},
    {"label": "2천~5천만", "min": 20_000_000, "max": 50_000_000},
    {"label": "5천만~1억", "min": 50_000_000, "max": 100_000_000},
    {"label": "1억~2.3억(고시금액)", "min": 100_000_000, "max": 230_000_000},
    {"label": "2.3억~7.1억", "min": 230_000_000, "max": 710_000_000},
    {"label": "7.1억~30억", "min": 710_000_000, "max": 3_000_000_000},
    {"label": "30억~150억", "min": 3_000_000_000, "max": 15_000_000_000},
    {"label": "150억+", "min": 15_000_000_000, "max": 10**13},
]


_NON_AMOUNT_CONDITION_KEYS = {
    "negotiation_reason", "is_women_enterprise", "is_social_enterprise",
    "is_tech_developed_product", "is_sme_product", "pq_required",
    "performance_restriction", "sme_restriction", "small_enterprise_restriction",
    "regional_restriction", "joint_contract", "joint_contract_kind",
    "negotiation_contract", "is_technical_service", "service_type",
    "construction_specialty", "product_category",
}


def _resolve_method_for_amount(res: dict, price: int) -> str | None:
    """method 또는 method_by_amount에서 금액 구간에 맞는 method 추출.

    F14 (2026-06-09): 적격심사 대상 룰은 method 텍스트에 "(적격심사)" 자동 명시 —
    실무 표기가 "10억 적격심사" 등으로 쓰이므로 시스템 노출 통일.
    """
    method = res.get("method")
    if isinstance(method, str) and method:
        # method가 "일반경쟁입찰"이고 bidder_selection이 적격심사면 명시
        bs = (res.get("bidder_selection") or "").lower()
        if method == "일반경쟁입찰" and "적격" in (res.get("bidder_selection") or ""):
            return "일반경쟁입찰 (적격심사)"
        # pass_score 매핑이 있으면 (= 적격심사 대상) 명시
        if method == "일반경쟁입찰" and res.get("pass_score"):
            return "일반경쟁입찰 (적격심사)"
        return method
    mba = res.get("method_by_amount")
    if isinstance(mba, dict):
        # 키 형식: "gte_N" — 내림차순으로 매칭
        tiers = []
        for k, v in mba.items():
            try:
                threshold = int(k.split("_", 1)[1]) if k.startswith("gte_") else 0
                tiers.append((threshold, v))
            except (ValueError, IndexError):
                continue
        tiers.sort(key=lambda t: -t[0])
        for threshold, v in tiers:
            if price >= threshold:
                return v
    return None


def _possible_methods_for(rules: list[dict], contract_type: str, price: int) -> list[dict]:
    """주어진 (contract_type, price)에서 적용 가능한 계약방법 추출.

    F13-1 (2026-06-09): method_by_amount 금액 구간별 매핑 +
    1순위 룰의 alternatives 포함 + 시행령 7조 원칙(일반경쟁 default) 보강.
    """
    out: list[dict] = []
    seen_methods: set[str] = set()
    matched_primary: list[dict] = []

    for r in rules:
        rule_ct = r.get("contract_type", "")
        if rule_ct != "public_procurement" and rule_ct != contract_type:
            continue
        cond = r.get("conditions", {})
        # 금액 조건 체크
        if "estimated_price_gte" in cond and price < cond["estimated_price_gte"]:
            continue
        if "estimated_price_lt" in cond and price >= cond["estimated_price_lt"]:
            continue
        if "estimated_price_lte" in cond and price > cond["estimated_price_lte"]:
            continue
        # 비금액 조건 (수의 사유·여성기업 등) 명시 룰은 사용자 선택 필요 → 매트릭스 제외
        non_amount = [k for k in cond if k in _NON_AMOUNT_CONDITION_KEYS]
        if non_amount:
            if not (len(non_amount) == 1 and cond.get("construction_specialty") == "general"):
                continue
        res = r.get("result", {})
        method = _resolve_method_for_amount(res, price) or "기본"
        if method in seen_methods:
            continue
        seen_methods.add(method)
        item = {
            "method": method,
            "rule_id": r.get("rule_id", ""),
            "priority": r.get("priority", 0),
            "alternatives_count": len(res.get("alternatives", []) or []),
            "kind": "primary",
        }
        out.append(item)
        matched_primary.append(r)

    out.sort(key=lambda x: x.get("priority") or 999)
    # F31 (2026-06-10): primary methods max 3개 — INTL 같은 보조 룰이 4번째로 끼면 제외 (F13-1 회귀)
    if len(out) > 3:
        out = out[:3]
        matched_primary = matched_primary[:3]

    # F13-1 정정 (2026-06-09): 1순위 룰의 alternatives 추가 (사용자 재량 선택 가능)
    for r in matched_primary[:3]:  # 우선순위 top 3 룰의 alternatives만 펼침
        for alt in (r.get("result", {}).get("alternatives", []) or []):
            if not isinstance(alt, dict):
                continue
            am = alt.get("method", "")
            if not am or am in seen_methods:
                continue
            seen_methods.add(am)
            out.append({
                "method": am,
                "rule_id": r.get("rule_id", ""),
                "priority": (r.get("priority") or 0) + 50,  # alternative은 후순위
                "alternatives_count": 0,
                "kind": "alternative",
                "reason": alt.get("reason", ""),
            })

    # 시행령 7조 원칙: 일반경쟁입찰은 모든 금액에서 default 가능. 누락 시 fallback 추가.
    # (단, 종합심사낙찰제·국제입찰 같은 의무 구간은 제외)
    has_general = any("일반경쟁" in m["method"] for m in out)
    is_mandatory_other = any(k in (out[0]["method"] if out else "") for k in ("종합심사", "국제입찰"))
    if not has_general and not is_mandatory_other:
        out.append({
            "method": "일반경쟁입찰",
            "rule_id": "DEFAULT_GENERAL",
            "priority": 999,
            "alternatives_count": 0,
            "kind": "default",
            "reason": "시행령 제7조 원칙 — 일반경쟁이 기본. 다른 옵션이 강제되지 않으면 항상 가능.",
        })

    # F31 (2026-06-10): 최종 max 3개. 일반경쟁은 시행령 7조 원칙으로 최소 1개 보존.
    # 2026-06-22 정정: 일반경쟁 '변형'(적격심사·국제입찰·국내입찰 등)이 여러 개일 때 전부 보호하면
    # 종합심사낙찰제 같은 distinct 우선 방법을 밀어낸다(공사 ≥265억 = 국제입찰+종합심사 동시 대상 회귀).
    # → 일반경쟁은 최우선 1개만 보호하고 나머지 슬롯은 우선순위순 distinct 방법에 배정.
    if len(out) > 3:
        generals = sorted(
            [m for m in out if m.get("kind") == "default" or "일반경쟁" in m.get("method", "")],
            key=lambda x: x.get("priority") or 999,
        )
        protected = generals[:1]  # 시행령 7조 — 일반경쟁 최우선 1개만 보존
        others = sorted(
            [m for m in out if m not in protected],
            key=lambda x: x.get("priority") or 999,
        )
        keep = others[: max(0, 3 - len(protected))] + protected
        keep.sort(key=lambda x: x.get("priority") or 999)
        out = keep[:3]
    return out


@_rules_router.get("/matrix")
async def rules_matrix():
    """금액 × 계약유형 → 가능 계약방법 매트릭스. 사용자가 표 형태로 검증 가능."""
    rules = _load_rules()
    matrix: dict = {"contract_types": ["service", "product", "construction"],
                    "brackets": _AMOUNT_BRACKETS, "cells": {}}
    for ct in matrix["contract_types"]:
        matrix["cells"][ct] = []
        for b in _AMOUNT_BRACKETS:
            # 구간 중간값으로 가능 방법 추출
            mid = (b["min"] + b["max"]) // 2
            methods = _possible_methods_for(rules, ct, mid)
            matrix["cells"][ct].append({
                "bracket": b["label"],
                "min": b["min"], "max": b["max"],
                "methods": methods,
            })
    return matrix


@_rules_router.get("/possible-methods")
async def possible_methods(contract_type: str, estimated_price: int):
    """단일 금액 + 계약유형 → 가능 계약방법 (Step1 입력 시 라이브 호출)."""
    rules = _load_rules()
    return {
        "contract_type": contract_type,
        "estimated_price": estimated_price,
        "methods": _possible_methods_for(rules, contract_type, estimated_price),
    }


_TREE_CONTRACT_TYPES = {"service", "product", "construction"}


@lru_cache(maxsize=16)
def _cached_tree(contract_type: str, org_type: str) -> dict:
    # 룰엔진을 동치 의사결정트리로 도출(엔진 재사용). 룰 불변 동안 캐시.
    from backend.api.deps import get_rule_engine
    from backend.services.rule_tree import build_tree
    return build_tree(get_rule_engine(), contract_type, org_type)


@_rules_router.get("/tree", dependencies=[Depends(require_admin)])
async def rules_tree(contract_type: str, org_type: str = "public_corp"):
    """계약방법 룰엔진을 도메인 전문가 검증용 '동치 의사결정트리'로 반환.

    학습 DT가 아니라 RuleEngine.match(조건필터→priority)와 1:1 동치인 트리(자동 도출).
    coverage.reproduced == coverage.cells 이면 모델 입력공간 전수에서 엔진과 일치함을 의미.
    2026-07-29 비공개 전환: 룰셋 전체가 노출되는 표면이라 admin 전용.
    """
    if contract_type not in _TREE_CONTRACT_TYPES:
        raise HTTPException(400, f"contract_type은 {sorted(_TREE_CONTRACT_TYPES)} 중 하나")
    return _cached_tree(contract_type, org_type)
