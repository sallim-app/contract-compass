"""계약나침반 MCP 서버 — 계약방법 결정·법령/코퍼스 검색을 MCP 도구로 노출.

에이전트(Codex·Claude 등)가 stdio(로컬) 또는 Streamable HTTP(원격,
https://contract.sallim.app/mcp)로 붙어 계약나침반 기능을 직접 호출한다.
대상 인스턴스는 env `CONTRACT_COMPASS_URL`(기본 로컬 백엔드 :8402 — CF 왕복 회피).

설계 원칙(2026-07-30): MCP 도구는 전부 **무LLM** — 클라이언트가 이미 LLM이므로
백엔드는 결정론 판정(decide, skip_llm)과 검색 원문(search_*)만 제공한다.
백엔드 OpenAI를 태우던 ask 도구는 제거(웹 UI 전용 /ask는 그대로).

실행: python3 mcp/server.py                  # stdio (로컬 검증·codex 등록용)
      python3 mcp/server.py streamable-http  # 원격 서빙 (systemd contract-mcp.service)
등록(codex): codex mcp add contract-compass -- python3 /path/to/mcp/server.py
"""
from __future__ import annotations

import json as _json
import os
import sys
import time
from pathlib import Path
from typing import Any, Literal

import httpx
from mcp import types as mcp_types
from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from auth import (PRICING_URL, DailyQuota, access_fields,  # noqa: E402 — mcp/ 로컬 모듈
                  resolve_access, unattributed_access)

BASE_URL = os.environ.get("CONTRACT_COMPASS_URL", "http://127.0.0.1:8402").rstrip("/")
API = f"{BASE_URL}/api/v1"
_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
# 모듈 전역 클라이언트 1개 — 호출마다 생성하면 커넥션 풀 재사용이 안 된다(mcp-tool-design §3).
# sync 도구는 SDK가 anyio.to_thread로 돌리므로(resolve.py:556 실측) thread-safe Client면 충분.
_http = httpx.Client(timeout=_TIMEOUT)
_ROOT = Path(__file__).resolve().parents[1]
_quota = DailyQuota(_ROOT / "logs" / "mcp_quota.json")
_CALL_LOG = _ROOT / "logs" / "mcp_calls.jsonl"
# 직전 호출 링버퍼 — report_issue가 자동 첨부(모델이 적는 도구명은 기억이라 틀린다,
# mcp-tool-design §2). 전역 공유라 동시 사용자 트래픽이 붙으면 섞일 수 있음(저트래픽 전제).
from collections import deque as _deque
RECENT_CALLS: "_deque[dict]" = _deque(maxlen=20)


def _denied_result(payload: dict) -> mcp_types.CallToolResult:
    """게이트 거부를 도구 결과(dict)로 반환 — 예외가 아니라 데이터.

    에이전트가 message를 그대로 사용자에게 읽어주는 realty-mcp 검증 패턴.
    isError=False 유지: 오류 채널로 보내면 클라이언트가 재시도만 반복한다(실측)."""
    return mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text",
                                       text=_json.dumps(payload, ensure_ascii=False, indent=2))],
        structured_content=payload,
    )


# stdio 전송으로 떠 있는가. **stdio 기동에서만 켠다 — 기본값은 False(fail-closed)다.**
# **왜 이 방향인가(2026-08-07, T-2026W32-105)**: 요청 컨텍스트 조회 실패를 `None`으로
# 삼키면 그 호출이 'stdio 로컬'로 판정돼 **무제한 티어 + 내부 분류**가 된다. HTTP로
# 떠 있는 상태에서 컨텍스트를 못 얻는 것은 로컬 호출이 아니라 **계측·게이트 고장**이고,
# 둘을 구별하지 못하면 외부 호출 전량이 조용히 내부로 분류돼 분모에서 사라진다
# (필드는 멀쩡해 집계기도 못 잡는다).
# 플래그를 `_HTTP_MODE`가 아니라 `_STDIO_MODE`로 둔 이유: ASGI import·gunicorn factory처럼
# `__main__`을 안 거치는 기동에서 HTTP 플래그는 False로 남아 **결함이 그대로 되살아난다**
# (codex 교차검증 지적). 모르는 상태의 기본값은 '로컬 무제한'이 아니라 '귀속 실패'여야 한다.
_STDIO_MODE = False


def _current_request(ctx) -> tuple[Any, str | None]:
    """(요청, 실패사유). stdio 기동에서만 요청 부재가 정상이므로 (None, None)."""
    try:
        req = ctx.request
    except Exception as e:  # noqa: BLE001 — 전송 구현이 던질 수 있다
        return None, type(e).__name__
    if req is None and not _STDIO_MODE:
        return None, "no_request_context"
    return req, None


class QuotaGate:
    """tools/call 단위 티어 게이트 + JSONL 호출 로그 (ServerMiddleware, 2026-07-30).

    stdio(ctx.request=None)는 local 티어로 무제한 — 야간 QA·codexw 하네스 보호.
    원격(streamable-http)은 무료 IP당 FREE_DAILY, cc_live_* 키는 키당 한도.
    **HTTP 모드에서 요청 컨텍스트가 없으면 그것은 stdio가 아니라 고장이다** —
    tier=unknown으로 적고 free와 같게 잠근다(unattributed_access)."""

    async def __call__(self, ctx, call_next):
        if ctx.method != "tools/call":
            return await call_next(ctx)
        tool = (ctx.params or {}).get("name", "?")
        req, attribution_error = _current_request(ctx)
        access = unattributed_access(attribution_error) if attribution_error \
            else resolve_access(req)
        if access.error:
            return _denied_result(access.error)
        if access.daily_limit is not None and not _quota.consume(access.subject, access.daily_limit):
            return _denied_result({
                "error": "daily_limit_exceeded",
                "message": f"무료 한도(하루 {access.daily_limit}콜)를 모두 사용했습니다. "
                           f"내일(UTC 자정) 리셋되며, 더 필요하면 유료 키 안내: {PRICING_URL}",
                "hint": "이 사실을 사용자에게 알리고 오늘은 추가 조회를 멈춰라.",
            })
        t0 = time.time()
        result = await call_next(ctx)
        try:
            args = (ctx.params or {}).get("arguments") or {}
            sc = getattr(result, "structured_content", None)
            entry = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "tool": tool,
                # 귀속 필드는 auth.access_fields가 단일 진실원 — 여기서 손으로 담으면
                # 하나가 빠져도 집계기는 0으로 읽을 뿐 아무도 못 본다(원문 IP·UA 미포함).
                **access_fields(access),
                "args": _json.dumps(args, ensure_ascii=False)[:300],
                "error": (sc or {}).get("error") if isinstance(sc, dict) else None,
                "dur_ms": round((time.time() - t0) * 1000),
            }
            RECENT_CALLS.append(entry)
            _CALL_LOG.parent.mkdir(parents=True, exist_ok=True)
            with _CALL_LOG.open("a", encoding="utf-8") as f:
                f.write(_json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001 — 계측 실패가 호출을 막지 않는다
            pass
        return result

# 전 도구 읽기전용 — 어노테이션이 없으면 codex(비대화)가 승인 대상으로 보고 자동 취소한다(2026-07-29 실측)
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
# report_issue 전용 — 유일한 쓰기 도구. 정직하게 non-readonly로 달아 대화형 클라이언트가
# 사용자 승인을 받게 한다(비대화 codex는 자동 취소될 수 있음 — 제보는 선택 기능이라 허용).
WRITE_FEEDBACK = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)

# server.json·공식 레지스트리와 단일 진실 — 3중 불일치(1.1.0/1.1.1/1.1.2) 정합(2026-08-09).
# 재게시 절차: server.json version 동기 → mcp-publisher login dns(sallim.app) → publish.
SERVER_VERSION = "1.1.3"

server = MCPServer(
    name="contract-compass",
    title="AI 계약나침반 (Contract Compass)",
    instructions=(
        "한국 공공계약(국가계약법·지방계약법) 계약방법 결정 도우미. "
        "decide_contract_method로 결정론 룰엔진 판정을, search_law·get_law_article로 "
        "법령 조문을, search_references로 예규·적격심사 세부기준·실무가이드까지 "
        "전 코퍼스를 조회한다. 분쟁·처분·해석 다툼 질문은 search_cases/get_case로 "
        "판례·법령해석례(law.go.kr 실시간)를 찾아 근거를 보강하라. "
        "모든 도구는 LLM을 쓰지 않으며 호출당 1~3초다 — 답변 합성은 "
        "네(클라이언트)가 도구 근거로 직접 하라. 판례·해석례가 필요한 질문인지는 "
        "네가 판단하되, 인용했다면 판례의 참조조문을 get_law_article로 교차확인하라. "
        "2~3개 병렬 호출은 무방하나 다발(4개 이상 동시) 호출은 피하라"
        "(단일 워커 백엔드라 대기 누적으로 타임아웃). "
        "도구가 {'error': ...}를 반환하면 그 hint를 따르고, 도구 근거 없이 "
        "자체 지식으로 법령 수치를 단정하지 마라. 도구 결과가 명백히 틀렸거나 "
        "사용자가 오류를 지적하면 report_issue로 제보하라(사용자에게 알리고). "
        "답변은 정보 제공용이며 법적 자문이 아니다."
    ),
    version=SERVER_VERSION,
)
server.middleware.append(QuotaGate())  # tools/call 티어 게이트 + 호출 로그


def _friendly_error(exc: Exception) -> dict:
    """백엔드 오류를 에이전트가 이해·중계할 수 있는 구조화 dict로.

    기존엔 httpx 예외가 그대로 도구 실패로 터져 에이전트가 원인을 모른 채
    재시도하다 자체 지식으로 조용히 폴백했다(2026-07-30 실측) — 원인·행동지침을 명시한다.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        try:
            detail = exc.response.json().get("detail", {})
        except Exception:  # noqa: BLE001 — 비JSON 응답은 코드만 전달
            detail = {}
        code = detail.get("error") if isinstance(detail, dict) else None
        if code == "daily_cap_exceeded":
            return {"error": "daily_cap_exceeded", "status": status,
                    "message": detail.get("message", "일일 AI 이용량 소진"),
                    "hint": "오늘은 AI 답변 예산이 소진됨(매일 09:00 KST 리셋). "
                            "이 사실을 사용자에게 알리고, search_law·get_law_article"
                            "(LLM 미사용)로 조문 근거만 제시하라."}
        if code == "rate_limit_exceeded":
            return {"error": "rate_limit_exceeded", "status": status,
                    "message": "요청 빈도 한도 초과",
                    "hint": f"{detail.get('retry_after', 60)}초 후 재시도하라. 병렬 호출 금지."}
        return {"error": "backend_error", "status": status,
                "message": str(detail or exc)[:300],
                "hint": "요청 인자를 바꿔도 같은 오류면 사용자에게 오류를 알려라."}
    if isinstance(exc, httpx.TimeoutException):
        return {"error": "timeout", "message": "백엔드 응답 지연(60초 초과)",
                "hint": "도구를 병렬로 여러 개 호출하면 지연이 누적된다 — 한 번에 하나씩 순차 호출하라."}
    return {"error": "connection_error", "message": str(exc)[:300],
            "hint": "백엔드 미도달 — 잠시 후 1회만 재시도하고, 실패 지속 시 사용자에게 알려라."}


def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    try:
        r = _http.get(f"{API}{path}", params=params)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as exc:
        return _friendly_error(exc)


def _post(path: str, body: dict[str, Any], headers: dict[str, str] | None = None) -> Any:
    try:
        r = _http.post(f"{API}{path}", json=body, headers=headers)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as exc:
        return _friendly_error(exc)


def _is_error(d: Any) -> bool:
    return isinstance(d, dict) and "error" in d


@server.tool(annotations=READ_ONLY)
def decide_contract_method(
    contract_type: Literal["construction", "service", "product"],
    estimated_price: int,
    org_type: Literal["national", "local", "public_corp"] = "public_corp",
    service_type: Literal["technical", "academic", "facility", "it_service", "other"] | None = None,
    construction_specialty: str | None = None,
    is_sme_competition_product: bool = False,
    negotiation_reason: str | None = None,
    is_women_enterprise: bool = False,
    is_disabled_enterprise: bool = False,
    is_social_enterprise: bool = False,
    is_youth_startup: bool = False,
    project_name: str = "MCP 조회",
) -> dict:
    """계약방법 결정론 판정 — 룰엔진이 적용 가능한 계약방법 후보와 법령 근거를 반환.

    Args:
        contract_type: "construction"(공사) | "service"(용역) | "product"(물품)
        estimated_price: 추정가격(원)
        org_type: "national"(국가기관) | "local"(지자체) | "public_corp"(공기업·준정부, 기본)
        service_type: 용역일 때 "technical"|"academic"|"facility"|"it_service"|"other"
        construction_specialty: 공사일 때 "general"(종합)|"electrical"|"ict"|"fire_safety" 등
        is_sme_competition_product: 중소기업자간 경쟁제품 여부
        negotiation_reason: 수의 사유 "urgent"|"rebid_failure"|"technical_difficulty"|
            "patent_new_tech"|"specific_person"|"small_repeat"|"other_justified"
        is_women_enterprise: 여성기업 여부 — 지자체 물품·용역 2천만원 초과 1억원 이하
            수의계약(시행령 제25조제1항제5호바목) 판정에 필요. 사용자가 "여성기업",
            "장애인기업", "사회적기업"이라고 말하면 **반드시 해당 플래그를 세워라** —
            빠뜨리면 수의계약 후보가 통째로 빠지고 경쟁입찰만 제시된다.
        is_disabled_enterprise: 장애인기업 여부 (위와 같은 목)
        is_social_enterprise: 사회적기업·사회적협동조합·자활기업·마을기업 여부 (위와 같은 목).
            이 유형은 행정안전부 고시 취약계층 고용비율 충족이 추가 요건이다.
        is_youth_startup: 청년창업기업 여부 — 지자체 물품·용역 2천만원 초과 5천만원
            이하 수의계약(같은 조 제5호 다목, 중소기업창업 지원법 제2조제11호)
    """
    body: dict[str, Any] = {
        "contract_type": contract_type,
        "estimated_price": estimated_price,
        "org_type": org_type,
        "is_sme_competition_product": is_sme_competition_product,
        "is_women_enterprise": is_women_enterprise,
        "is_disabled_enterprise": is_disabled_enterprise,
        "is_social_enterprise": is_social_enterprise,
        "is_youth_startup": is_youth_startup,
        "project_name": project_name,
        # 에이전트 클라이언트는 자체 LLM으로 설명을 합성 — 백엔드 LLM 보조설명 생략
        # (판정 결과·법령 근거는 동일, OpenAI 일일 예산 0 소모. 2026-07-30)
        "skip_llm": True,
    }
    if service_type:
        body["service_type"] = service_type
    if construction_specialty:
        body["construction_specialty"] = construction_specialty
    if negotiation_reason:
        body["negotiation_reason"] = negotiation_reason
    d = _post("/filter/step1", body)
    if _is_error(d):
        return d
    # 에이전트가 소화하기 좋은 축약 형태로 정리
    return {
        "candidates": [
            {
                "rank": c.get("rank"),
                "method": c.get("method"),
                "rule_id": c.get("rule_id"),
                "summary": c.get("summary"),
                "key_params": c.get("key_params"),
                "legal_basis": c.get("legal_basis"),
            }
            for c in d.get("candidates", [])
        ],
        "practice_alternatives": d.get("practice_alternatives", []),
        "explanation": (d.get("decision_pack") or {}).get("human_explanation", ""),
        "laws_applied": [
            {"key": l.get("key"), "law_name": l.get("law_name")}
            for l in (d.get("decision_pack") or {}).get("laws_applied", [])
        ],
        "follow_up_questions": [
            {"id": q.get("id"), "text": q.get("text"), "description": q.get("description")}
            for q in d.get("next_step_questions", [])
        ],
    }


@server.tool(annotations=READ_ONLY)
def search_law(query: str, top_k: int = 8) -> dict:
    """법령 조문 검색 — 키워드 또는 조문번호로 조문 스니펫 반환(상위 top_k건).

    전문이 필요하면 get_law_article(ref)로 이어서 조회.
    hit에 note가 있으면 삭제·폐지된 조문이다 — 판단 근거로 인용하지 마라.

    Args:
        query: "수의계약", "시행령 제26조", "제21조" 등
        top_k: 반환 건수 (기본 8, 최대 20)
    """
    hits = _get("/law/search", {"q": query})
    if _is_error(hits):
        return hits
    total_found = len(hits)
    out = []
    for h in hits[: max(1, min(top_k, 20))]:
        h = dict(h)
        for k in ("content", "snippet"):
            if isinstance(h.get(k), str) and len(h[k]) > 400:
                h[k] = h[k][:400] + "…"
        out.append(h)
    result: dict[str, Any] = {"hits": out, "count": len(out), "total_found": total_found}
    if total_found > len(out):
        result["note"] = f"총 {total_found}건 중 상위 {len(out)}건만 표시 — 더 필요하면 top_k를 올려라"
    if not out:
        # 0건은 오류가 아니라 재질의 신호 — 에이전트가 "실패"로 오독하고 자체 지식으로
        # 빠지지 않게 다음 행동을 명시한다(2026-07-30, 복합 쿼리 0건 6/12 실측).
        result["hint"] = ("0건 — 짧은 단일 키워드('수의계약')나 '법령명 제N조' 형태로 "
                          "재검색하거나, search_references로 예규·가이드까지 넓혀 찾아라.")
    return result


@server.tool(annotations=READ_ONLY)
def search_references(query: str, top_k: int = 6) -> dict:
    """전 코퍼스 통합 검색 — 법령+계약예규+조달청·행안부 세부기준+실무가이드. LLM 미사용.

    search_law가 법령 조문 전용인 것과 달리 예규·적격심사 세부기준·실무가이드까지
    검색한다. 낙찰하한율·적격심사 배점·실무 절차 등 법령 본문 밖 질문에 사용하라.
    AI 생성 없이 검색 근거 원문만 반환한다(백엔드 LLM 예산 미차감).

    Args:
        query: 자연어 검색어 (예: "적격심사 낙찰하한율 50억 미만")
        top_k: 반환 건수 (기본 6, 최대 12)
    """
    hits = _get("/law/references", {"q": query, "top_k": max(1, min(top_k, 12))})
    if _is_error(hits):
        return hits
    result: dict[str, Any] = {"hits": hits, "count": len(hits)}
    # 2026-07-30 R9: 순위 근거를 에이전트에게 밝힌다. rerank가 못 돌면(키 미설정·한도 초과)
    # 순서는 하이브리드 검색(RRF) 그대로이고 relevance 값은 관련도가 아니라 표시용이다 —
    # 이걸 숨기면 에이전트가 1위 청크를 정답으로 단정한다.
    if hits and all(h.get("ranked_by") == "hybrid_rrf" for h in hits):
        result["ranking"] = "hybrid_rrf"
        result["hint"] = ("재정렬(rerank) 미가동 — 순서는 키워드·의미 혼합 검색 순이고 "
                          "relevance는 관련도가 아니다. 상위 결과를 정답으로 단정하지 말고 "
                          "본문을 직접 읽어 판단하라.")
    elif hits:
        result["ranking"] = "rerank"
    if not hits:
        result["hint"] = "0건 — 핵심 명사 위주로 짧게 재검색하거나 search_law로 조문을 직접 찾아라."
    return result


@server.tool(annotations=READ_ONLY)
def search_cases(query: str, top_k: int = 5, kind: Literal["prec", "expc", "all"] = "all") -> dict:
    """판례·법령해석례 검색 — law.go.kr 실시간 조회(항상 현행). LLM 미사용.

    분쟁·처분취소·해석 다툼("~해도 되나", "~취소될 수 있나")에 조문만으로 부족할 때
    쓰라. 본문은 get_case(kind, case_id)로 이어서 조회.

    Args:
        query: 핵심 명사 위주 검색어 (예: "부정당업자 제한", "유찰 수의계약")
        top_k: 종류당 반환 건수 (기본 5, 최대 10)
        kind: "prec"(법원 판례) | "expc"(법제처 법령해석례) | "all"(둘 다, 기본)
    """
    hits = _get("/law/cases", {"q": query, "top_k": max(1, min(top_k, 10)), "kind": kind})
    if _is_error(hits):
        return hits
    result: dict[str, Any] = {"hits": hits, "count": len(hits)}
    if not hits:
        result["hint"] = "0건 — 더 짧은 핵심어(예: '지체상금', '담합')로 재검색하라."
    return result


@server.tool(annotations=READ_ONLY)
def get_case(kind: Literal["prec", "expc"], case_id: str) -> dict:
    """판례/해석례 본문 조회 — 판시사항·판결요지·참조조문(판례) 또는 질의요지·회답·이유(해석례).

    Args:
        kind: "prec" | "expc" (search_cases 결과의 kind)
        case_id: search_cases 결과의 case_id
    """
    return _get("/law/case", {"kind": kind, "case_id": case_id})


@server.tool(annotations=READ_ONLY)
def get_law_article(ref: str) -> dict:
    """법령 조문 원문 전체 조회.

    응답의 `notes`가 비어 있지 않으면 **법률 자체의 미정비 상호인용**이 탐지된
    것이다(예: 제5항이 '제2항 각 호'를 인용하나 제2항에 각 호가 없음). 원문은
    law.go.kr 현행 그대로이며 우리가 고치지 않는다 — 그 조문을 근거로 답할 때는
    notes의 내용을 사용자에게 함께 알리고 단정을 피하라.

    Args:
        ref: 정확한 조문 참조 (예: "국가계약법 시행령 제26조")
    """
    d = _get("/law/article", {"ref": ref})
    if _is_error(d):
        return d
    if isinstance(d.get("content"), str) and len(d["content"]) > 6000:
        d["content"] = d["content"][:6000] + "…(생략)"
    return d


@server.tool(annotations=READ_ONLY)
def get_law_article_asof(ref: str, date: str) -> dict:
    """**특정 시점에 시행 중이던** 조문 원문 조회 (law.go.kr 연혁 라이브).

    get_law_article은 항상 현행이다. 과거 사건에 현행 조문을 적용하면 조용히 틀린
    답이 된다 — 계약 체결·입찰공고·처분 시점이 과거이면 **반드시 이 도구를 쓰라**:
      - "2023년에 체결한 계약인데 지체상금률이 맞나"
      - "재작년 부정당업자 제재가 당시 기준으로 적법했나"
      - 감사·분쟁·소송 대응(적용법령은 행위시법이 원칙)

    응답 필드:
      effective_date  그 시점에 시행 중이던 판의 시행일자
      is_current      그 판이 지금도 현행인가 (False면 이후 개정됨)
      prev/next_effective_date  직전·직후 개정 시행일 — 경계 판단용
      notes           미정비 상호인용 경고 (get_law_article과 동일)

    Args:
        ref: 조문 참조 (예: "국가계약법 제27조", "국가계약법 시행령 제26조")
        date: 기준일 "YYYY-MM-DD" 또는 "YYYYMMDD" (예: 계약 체결일)
    """
    d = _get("/law/article-asof", {"ref": ref, "date": date})
    if _is_error(d):
        return d
    if isinstance(d.get("content"), str) and len(d["content"]) > 6000:
        d["content"] = d["content"][:6000] + "…(생략)"
    return d


@server.tool(annotations=WRITE_FEEDBACK)
def report_issue(
    category: Literal["wrong_citation", "outdated_law", "wrong_ruling",
                      "tool_error", "feature_request", "other"],
    message: str,
    related_tool: str | None = None,
    related_query: str | None = None,
    expected: str | None = None,
) -> dict:
    """오류·개선 제보 — 운영자에게 전달된다(웹 피드백과 같은 검토 파이프라인).

    사용자가 "틀렸다"고 지적하면 **먼저 이 도구로 제보한 뒤** 정정 답을 제시하라.
    도구 결과가 조문·수치·판례와 명백히 불일치할 때도 제보하라. 추측으로 부르지 말 것.
    서버가 직전 도구 호출 기록을 자동 첨부하므로 도구명·인자를 기억으로 적을 필요 없다.

    Args:
        category: "wrong_citation"(오인용) | "outdated_law"(개정 미반영) |
            "wrong_ruling"(룰엔진 오판정) | "tool_error"(도구 오류) |
            "feature_request"(기능 요청) | "other"
        message: 무엇이 어떻게 잘못됐는지 구체적으로 (근거 조문·기대값 포함 권장)
        related_tool: 문제가 난 도구명 (예: "search_references")
        related_query: 문제를 재현하는 질의·입력
        expected: 올바르다고 생각하는 값·조문 (알고 있다면)
    """
    cats = {"wrong_citation", "outdated_law", "wrong_ruling", "tool_error",
            "feature_request", "other"}
    if category not in cats:
        return {"error": "bad_category", "message": f"category는 {sorted(cats)} 중 하나"}
    if not message or len(message.strip()) < 10:
        return {"error": "too_short", "message": "message에 문제 상황을 10자 이상 구체적으로 적어라"}
    comment = f"[MCP:{category}] {message.strip()[:2000]}"
    if related_tool:
        comment += f"\n도구: {related_tool[:100]}"
    if expected:
        comment += f"\n기대값: {expected[:500]}"
    import uuid as _uuid
    # 직전 호출 자동 첨부 — 모델 기억 대신 서버 기록(전역 링버퍼, 저트래픽 전제)
    recent = [f"{c['ts']} {c['tool']}({c['args'][:120]})" + (f" ERR={c['error']}" if c.get("error") else "")
              for c in list(RECENT_CALLS)[-6:-1]]  # 마지막(=이 제보 자신) 제외 5건
    if recent:
        comment += "\n[직전 호출(서버 기록)]\n" + "\n".join(recent)
    body = {
        "session_id": f"mcp-{_uuid.uuid4().hex[:12]}",
        "rating": "3" if category == "feature_request" else "1",
        "comment": comment,
        "feedback_type": "general",
        "question": (related_query or "")[:1000],
        "page": "MCP",
    }
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.post(f"{API}/feedback", data=body)  # 웹과 동일 엔드포인트(Form)
            r.raise_for_status()
    except httpx.HTTPError as exc:
        return _friendly_error(exc)
    return {"ok": True,
            "message": "제보가 접수됐습니다. 운영자가 검토 후 반영합니다.",
            "note": "사용자에게 '운영자에게 전달됐다'고 알리고, 가능한 범위의 "
                    "정정 답변(다른 도구로 교차확인한 근거)을 함께 제시하라."}


async def _health(request):  # noqa: ANN001 — Starlette Request
    """앱+백엔드 도달 + 데이터 신선도를 한 번에 판정한다(Kuma·배포 게이트 규약).

    data_as_of = 코퍼스(chroma_db) 마지막 인덱싱 시각 — 서버가 살아 있어도
    코퍼스가 낡았으면 쓸모가 다르다(mcp-tool-design §6)."""
    from starlette.responses import JSONResponse
    try:
        with httpx.Client(timeout=httpx.Timeout(5.0)) as c:
            backend = c.get(f"{BASE_URL}/ready").status_code
    except httpx.HTTPError:
        backend = 0
    data_as_of = None
    try:
        mtimes = [f.stat().st_mtime for f in (_ROOT / "chroma_db").glob("**/*.bin")]
        if not mtimes:
            mtimes = [(_ROOT / "chroma_db").stat().st_mtime]
        data_as_of = time.strftime("%Y-%m-%d", time.gmtime(max(mtimes)))
    except OSError:
        pass
    ok = backend == 200
    # version: 클라이언트 스테일 도구목록 캐시 판별의 유일한 단서(realty 재검증 제보 #16
    # 교훈 이식 — 배포 후 옛 목록으로 '미배포' 오탐 신고가 왔던 자리).
    return JSONResponse({"status": "ok" if ok else "degraded", "backend_ready": backend,
                         "data_as_of": data_as_of, "version": SERVER_VERSION},
                        status_code=200 if ok else 503)


# 외부는 nginx가 /mcp* 만 이 서버로 넘긴다 — /mcp/health가 외부 감시 경로다.
for _hp in ("/health", "/mcp/health"):
    server.custom_route(_hp, methods=["GET"], include_in_schema=False)(_health)


_PRICING_HTML = """<!doctype html><html lang="ko"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>계약나침반 MCP — 요금 안내</title>
<body style="font-family:system-ui,sans-serif;max-width:44rem;margin:4rem auto;padding:0 1rem;line-height:1.65">
<h1>🧭 계약나침반 MCP 요금 안내</h1>
<p>한국 공공계약 법령·판례 MCP 서버 — 모든 도구는 LLM 없이 검증 가능한 법적 근거만
반환합니다. 무료로 전 도구를 쓸 수 있고, 유료 키는 <b>한도만</b> 올립니다(기능 차이 없음).</p>
<table border="1" cellpadding="8" style="border-collapse:collapse;width:100%">
<tr><th>티어</th><th>일일 한도</th><th>기능</th><th>가격</th></tr>
<tr><td>무료</td><td>IP당 50콜 (UTC 자정 리셋)</td><td>도구 8종 전부</td><td>0원</td></tr>
<tr><td>체험 키</td><td>키당 2,000콜</td><td>동일</td><td>7일 1,000원</td></tr>
<tr><td>PRO 키</td><td>키당 2,000콜</td><td>동일 + 우선 지원</td><td>30일 9,900원 · 90일 24,900원</td></tr>
</table>
<p><b>카드결제</b> — <b>해외결제 가능한 카드</b>(Visa/Mastercard 등 국제 브랜드)면
개인·법인·정부구매카드 구분 없이 결제됩니다. 결제 즉시 라이선스 키가 이메일로 자동
발송되고 영수증(인보이스)도 함께 발행됩니다. 자동결제(구독) 없음 — 기간 만료 시 무료
티어로 자연 복귀합니다. 결제가 거절되면 해외(온라인)결제 차단 여부를 카드사에 확인해
주세요. 기관 구매·세금계산서 등 별도 서류가 필요하면 <b>contract@sallim.app</b>으로
문의해 주세요.</p>
<h2>연결 방법</h2>
<pre style="background:#f4f4f5;padding:12px;border-radius:8px;overflow-x:auto">
# Claude Code
claude mcp add --transport http contract-compass https://contract.sallim.app/mcp

# Cursor (.cursor/mcp.json)
{ "mcpServers": { "contract-compass": { "url": "https://contract.sallim.app/mcp" } } }


# 유료 키 사용 시 (헤더)
Authorization: Bearer cc_live_...        # 또는 URL 뒤 ?key=cc_live_... (ChatGPT 커넥터)
</pre>
<p style="color:#666;font-size:.9rem">데이터 출처: 국가법령정보센터(law.go.kr) Open API·
기획재정부 계약예규·조달청/행안부 세부기준·감사원 공개 간행물. 모든 응답은 정보 제공
목적이며 법적 자문이 아닙니다. 도구 명세:
<a href="https://github.com/sallim-app/contract-compass/blob/master/docs/MCP.md">docs/MCP.md</a></p>
</body></html>"""


async def _pricing(request):  # noqa: ANN001
    from starlette.responses import HTMLResponse
    return HTMLResponse(_PRICING_HTML)


for _pp in ("/pricing", "/mcp/pricing"):
    server.custom_route(_pp, methods=["GET"], include_in_schema=False)(_pricing)


# ── 구매 웹훅: Creem(주채널) + Lemon Squeezy(예비) ───────────────────────────
# 결제사가 라이선스 키 생성·고객 이메일 발송까지 담당 — 이 웹훅은 그 키를
# 대장(keystore)에 미러해 기존 sha256 인증 경로(auth.py 무수정)로 수용하고,
# 주문·금액·고객을 매출 대장에 자동 기록한다. 환불류 이벤트는 키 회수.
# 디스패치: creem-signature 헤더가 있으면 Creem, x-signature면 LS. 해당 채널
# 시크릿 미설정이면 503 — 계정 연결 전 비활성 상태.
_LS_PLANS_ENV = "LS_VARIANT_PLANS"  # JSON: {"variant_id": {"days":30,"daily":2000,"amount_krw":9900,"label":"PRO 30일"}}
_CREEM_PLANS_ENV = "CREEM_PRODUCT_PLANS"  # JSON: {"prod_x": {"days":30,"daily":2000,"amount_krw":9500,"label":"PRO 30일"}}


def _ls_plans() -> dict:
    try:
        return _json.loads(os.environ.get(_LS_PLANS_ENV, "") or "{}")
    except Exception:
        return {}


def _creem_plans() -> dict:
    try:
        return _json.loads(os.environ.get(_CREEM_PLANS_ENV, "") or "{}")
    except Exception:
        return {}


def _walk_find(obj, want_key_substr: str, *, as_dict_with: str | None = None):
    """중첩 payload에서 키 이름에 want_key_substr가 든 값을 깊이우선으로 찾는다.

    Creem 문서가 checkout.completed 안 라이선스 키의 정확한 경로를 못 박지 않아
    (transaction object 안 어딘가), 스키마 드리프트에 강한 방어적 추출을 쓴다.
    as_dict_with가 주어지면 dict 값에서 그 하위 필드를 꺼낸다(예: license.key).
    """
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                if want_key_substr in k.lower():
                    if isinstance(v, str) and v:
                        return v
                    if isinstance(v, dict) and as_dict_with and isinstance(v.get(as_dict_with), str):
                        return v[as_dict_with]
                stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


async def _creem_webhook(raw: bytes, request):  # noqa: ANN001
    import hashlib as _hl
    import hmac as _hmac

    from starlette.responses import JSONResponse
    secret = os.environ.get("CREEM_WEBHOOK_SECRET", "")
    if not secret:
        return JSONResponse({"error": "webhook_disabled"}, status_code=503)
    sig = request.headers.get("creem-signature", "")
    expect = _hmac.new(secret.encode(), raw, _hl.sha256).hexdigest()
    if not _hmac.compare_digest(sig, expect):
        return JSONResponse({"error": "bad_signature"}, status_code=401)

    import keystore
    try:
        payload = _json.loads(raw)
    except Exception:
        return JSONResponse({"error": "bad_json"}, status_code=400)
    event = payload.get("eventType", "")
    obj = payload.get("object") or {}

    def _order_ref() -> str:
        order = obj.get("order")
        oid = order.get("id") if isinstance(order, dict) else (order if isinstance(order, str) else "")
        return f"creem-{oid or payload.get('id', '')}"

    if event == "checkout.completed":
        product = obj.get("product")
        product_id = product.get("id") if isinstance(product, dict) else (product if isinstance(product, str) else "")
        plan = _creem_plans().get(str(product_id), {})
        lic = _walk_find(obj, "license", as_dict_with="key")
        if not lic:
            # 대시보드에서 상품의 License Key 기능이 꺼져 있으면 여기로 온다 —
            # 조용히 넘기면 고객이 결제하고도 키를 못 받는다. 로그에 크게 남긴다.
            def _shape(o, depth=0):
                # 값은 빼고 키 구조만 — 키 경로 규명용(PII·키 원문 로그 금지)
                if isinstance(o, dict):
                    return {k: _shape(v, depth + 1) if depth < 3 else "…" for k, v in o.items()}
                if isinstance(o, list):
                    return [_shape(o[0], depth + 1)] if o else []
                return type(o).__name__
            print(f"[purchase-webhook] Creem checkout.completed에 라이선스 키 없음 — "
                  f"상품 {product_id} License 기능 토글 확인 필요 (order={_order_ref()}) "
                  f"payload구조={_json.dumps(_shape(payload), ensure_ascii=False)[:1500]}",
                  file=sys.stderr, flush=True)
            return JSONResponse({"ok": False, "warning": "no_license_key",
                                 "hint": "Creem 대시보드에서 해당 상품 License Key 기능 활성 필요"})
        customer = obj.get("customer") or {}
        email = customer.get("email", "") if isinstance(customer, dict) else ""
        _, rec = keystore.issue(
            name=plan.get("label", f"Creem {product_id}"),
            days=int(plan.get("days", 30)),
            daily=int(plan.get("daily", 2000)),
            channel="creem",
            amount_krw=int(plan.get("amount_krw", 0)),
            contact=email,
            order_id=_order_ref(),
            key=lic, source="creem_mirror",
        )
        return JSONResponse({"ok": True, "key_prefix": rec["key_prefix"]})

    if event in ("refund.created", "subscription.expired", "subscription.canceled",
                 "dispute.created"):
        rec = keystore.revoke_by_order(_order_ref())
        return JSONResponse({"ok": True, "revoked": bool(rec)})

    return JSONResponse({"ok": True, "ignored": event})


async def _purchase_webhook(request):  # noqa: ANN001
    import hashlib as _hl
    import hmac as _hmac

    from starlette.responses import JSONResponse
    raw = await request.body()
    if "creem-signature" in request.headers:
        return await _creem_webhook(raw, request)
    secret = os.environ.get("LEMONSQUEEZY_WEBHOOK_SECRET", "")
    if not secret:
        return JSONResponse({"error": "webhook_disabled"}, status_code=503)
    sig = request.headers.get("x-signature", "")
    expect = _hmac.new(secret.encode(), raw, _hl.sha256).hexdigest()
    if not _hmac.compare_digest(sig, expect):
        return JSONResponse({"error": "bad_signature"}, status_code=401)

    import keystore
    try:
        payload = _json.loads(raw)
    except Exception:
        return JSONResponse({"error": "bad_json"}, status_code=400)
    event = (payload.get("meta") or {}).get("event_name", "")
    attrs = (payload.get("data") or {}).get("attributes", {}) or {}

    if event == "license_key_created":
        ls_key = attrs.get("key", "")
        if not ls_key:
            return JSONResponse({"error": "no_key"}, status_code=400)
        variant = str(attrs.get("variant_id") or (attrs.get("order_item") or {}).get("variant_id") or "")
        plan = _ls_plans().get(variant, {})
        _, rec = keystore.issue(
            name=plan.get("label", f"LS variant {variant}"),
            days=int(plan.get("days", 30)),
            daily=int(plan.get("daily", 2000)),
            channel="lemonsqueezy",
            amount_krw=int(plan.get("amount_krw", 0)),
            contact=attrs.get("user_email", "") or attrs.get("customer_email", ""),
            order_id=f"ls-{attrs.get('order_id', payload.get('data', {}).get('id', ''))}",
            key=ls_key, source="ls_mirror",
        )
        return JSONResponse({"ok": True, "key_prefix": rec["key_prefix"]})

    if event in ("order_refunded", "subscription_expired", "subscription_cancelled",
                 "license_key_updated"):
        # license_key_updated는 비활성화(disabled) 신호일 때만 회수
        if event == "license_key_updated" and attrs.get("status") not in ("disabled", "inactive"):
            return JSONResponse({"ok": True, "ignored": True})
        order_ref = f"ls-{attrs.get('order_id', payload.get('data', {}).get('id', ''))}"
        rec = keystore.revoke_by_order(order_ref)
        return JSONResponse({"ok": True, "revoked": bool(rec)})

    return JSONResponse({"ok": True, "ignored": event})


for _wp in ("/purchase-webhook", "/mcp/purchase-webhook"):
    server.custom_route(_wp, methods=["POST"], include_in_schema=False)(_purchase_webhook)


if __name__ == "__main__":
    transport = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    if transport == "streamable-http":
        server.run("streamable-http",
                   host="0.0.0.0",
                   port=int(os.environ.get("MCP_PORT", "8403")),
                   stateless_http=True)
    else:
        # 여기서만 "요청 컨텍스트 부재는 정상"이 된다(_current_request 참조).
        _STDIO_MODE = True
        server.run()  # stdio — codex 등록·로컬 검증 경로 유지
