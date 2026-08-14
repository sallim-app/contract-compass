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
SERVER_VERSION = "1.6.1"

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
        # 백엔드가 이미 구조화 오류(error+hint)를 준 경우는 **그대로 중계**한다 —
        # str()로 뭉개면 "못 봄 ≠ 없음"을 가르는 law_in_corpus·corpus_laws 같은
        # 판단 근거가 사라진다(T-2026W33-146: article_not_found / law_not_specified).
        if isinstance(detail, dict) and detail.get("error") and detail.get("hint"):
            return {**detail, "status": status}
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


# 검색 도구별 top_k 상한. 상한 자체는 정당한 운영 상수지만 **말없이 깎으면 안 된다** —
# 모델은 자기가 요청한 범위를 받았다고 믿는다(mcp-tool-design §12.4, T-2026W33-160).
_TOP_K_CAPS = {"search_law": 20, "search_references": 12, "search_cases": 10}


def _applied_top_k(requested: int, cap: int) -> tuple[int, dict | None]:
    """(적용값, 조정 공시). 요청과 다른 값을 쓸 때만 공시 dict를 돌려준다."""
    applied = max(1, min(requested, cap))
    if applied == requested:
        return applied, None
    return applied, {
        "requested": requested, "applied": applied, "cap": cap,
        "reason": f"top_k 허용 범위는 1~{cap} — 요청값을 조정했다(응답 건수는 applied 기준)."}


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
    is_small_enterprise: bool = False,
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
        is_youth_startup: 청년창업기업 여부 — 물품·용역 2천만원 초과 5천만원 이하
            수의계약(지방 제5호 다목 / 국가 시행령 제26조①5호가목7, 중소기업창업
            지원법 제2조제11호)
        is_small_enterprise: 상대방이 소기업·소상공인인지 여부 — 2천만원 초과 1억원
            이하 수의계약(국가 시행령 제26조①5호가목3 / 지방 시행령 제25조①5호라목)
            판정에 필요. **주의: 국가·공기업 2천만원 초과~1억원 이하는 무조건
            소액수의가 아니다** — 소기업·소상공인/특수 지식·기술(academic)/여성·
            장애인·사회적기업/청년창업(5천만 이하) 요건 충족 시에만 수의 가능하므로,
            해당하면 플래그를 세워라. 미충족이면 경쟁입찰이 원칙이다.
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
        "small_enterprise_restriction": is_small_enterprise,
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
    # 에이전트가 소화하기 좋은 축약 형태로 정리.
    # 2026-08-12 R23·R25(T-2026W33-58): 종전엔 notes(요건 경고)와 조문 본문(articles)을
    # 여기서 버려, 룰 파일에 정확히 적힌 "2천만 초과~1억은 상대방 요건 충족 시만 수의"
    # 경고가 에이전트에 배달되지 않았고 조문으로 자력 정정도 불가능했다 — 배달한다.
    # 조문 본문은 6,000자 캡(시행령 제26조 전문 5,729자 수록) + 절단 공시(거짓말 금지).
    def _cap_article(a: dict) -> dict:
        body = a.get("body") or ""
        out = {"title": a.get("title"), "body": body}
        if len(body) > 6000:
            out["body"] = body[:6000] + "…"
            out["truncated"] = f"총 {len(body)}자 중 앞 6,000자만 표시"
        return out

    return {
        "candidates": [
            {
                "rank": c.get("rank"),
                "method": c.get("method"),
                "rule_id": c.get("rule_id"),
                "summary": c.get("summary"),
                "key_params": c.get("key_params"),
                "legal_basis": c.get("legal_basis"),
                "notes": c.get("notes"),
            }
            for c in d.get("candidates", [])
        ],
        # 2026-08-14 T-2026W33-158: 노출 상한 3개로 잘린 후보 실토(조용한 절단 금지).
        # 비어 있으면 잘린 것이 없다는 뜻 — "이 3개가 전부"를 단정할 근거는 이 필드뿐이다.
        "omitted_candidates": d.get("omitted_candidates", []),
        "practice_alternatives": d.get("practice_alternatives", []),
        "explanation": (d.get("decision_pack") or {}).get("human_explanation", ""),
        "laws_applied": [
            {"key": l.get("key"), "law_name": l.get("law_name"),
             "articles": [_cap_article(a) for a in (l.get("articles") or [])]}
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
    applied, clamp = _applied_top_k(top_k, _TOP_K_CAPS["search_law"])
    out = []
    for h in hits[:applied]:
        h = dict(h)
        for k in ("content", "snippet"):
            if isinstance(h.get(k), str) and len(h[k]) > 400:
                h[k] = h[k][:400] + "…"
        out.append(h)
    result: dict[str, Any] = {"hits": out, "count": len(out), "total_found": total_found}
    if clamp:
        result["top_k_applied"] = clamp
    if total_found > len(out):
        # 2026-08-14 T-2026W33-160: 종전 안내는 상한에 이미 닿은 호출에도 "top_k를 올려라"라고
        # 해서 **실행 불가능한 행동**을 지시했다(codex 탐침). 상한이면 다른 길을 준다.
        result["note"] = (
            f"총 {total_found}건 중 상위 {len(out)}건만 표시 — top_k 상한이 "
            f"{_TOP_K_CAPS['search_law']}이라 더 올릴 수 없다. 질의를 좁히거나"
            "('법령명 제N조' 형태) get_law_article로 특정 조문을 직접 조회하라."
            if applied >= _TOP_K_CAPS["search_law"] else
            f"총 {total_found}건 중 상위 {len(out)}건만 표시 — 더 필요하면 top_k를 올려라"
            f"(최대 {_TOP_K_CAPS['search_law']})")
    # 2026-08-14 T-2026W33-167(codex 탐침): 키워드 0건일 때 시맨틱 폴백 결과가 그대로
    # count>0으로 나가 **무결과가 정상 검색결과로 위장**됐다(실측: 존재하지 않는 가상
    # 식별자 질의에 무관한 예규 2건). 히트마다 matched_by="semantic"은 있었지만 그건
    # 히트를 들여다봐야 보이는 것이고, 모델은 count부터 읽는다 — 최상위에 실토한다.
    if out and all(h.get("matched_by") == "semantic" for h in out):
        result["matched_by"] = "semantic_fallback"
        result["note_fallback"] = (
            f"키워드 매치 0건 — 의미(임베딩) 검색으로 폴백한 결과 {len(out)}건이다. "
            "질의어를 본문에 포함하지 않을 수 있으니 **그대로 근거로 인용하지 말고** "
            "본문을 읽어 관련성을 직접 판단하라. 찾는 조문이 없을 가능성이 높다.")
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
    applied, clamp = _applied_top_k(top_k, _TOP_K_CAPS["search_references"])
    hits = _get("/law/references", {"q": query, "top_k": applied})
    if _is_error(hits):
        return hits
    result: dict[str, Any] = {"hits": hits, "count": len(hits)}
    if clamp:
        result["top_k_applied"] = clamp
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
    applied, clamp = _applied_top_k(top_k, _TOP_K_CAPS["search_cases"])
    hits = _get("/law/cases", {"q": query, "top_k": applied, "kind": kind})
    if _is_error(hits):
        return hits
    result: dict[str, Any] = {"hits": hits, "count": len(hits)}
    if not hits:
        result["hint"] = "0건 — 더 짧은 핵심어(예: '지체상금', '담합')로 재검색하라."
    return result


@server.tool(annotations=READ_ONLY)
def get_case(kind: Literal["prec", "expc"], case_id: str) -> dict:
    """판례/해석례 본문 조회 — 판시사항·판결요지·참조조문(판례) 또는 질의요지·회답·이유(해석례).

    응답의 `source_url`은 국가법령정보센터 원문 주소다 — 판례·해석례를 인용할 때는
    **이 링크를 함께 제시하라**(감사·보고서에서 근거를 되짚을 수 있어야 한다).

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

    응답에 `assumption`이 있으면 **법령명을 우리가 추정해 채운 것**이다(예: "시행령
    제26조" → 국가계약법 시행령). 지방계약 질문이었다면 틀린 법을 보고 있는 것이니
    assumption.hint대로 법령명을 붙여 다시 부르고, 어느 법령 기준인지 사용자에게 밝혀라.

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


@server.tool(annotations=READ_ONLY)
def estimate_delay_penalty(
    contract_kind: Literal["construction", "product_manufacture", "product_repair",
                           "service", "military_food", "transport_storage"],
    org_type: Literal["national", "local", "public_corp"],
    contract_amount: int,
    delay_days: int,
    excluded_days: int = 0,
    accepted_portion_amount: int = 0,
    design_build_approved: bool = False,
) -> dict:
    """지체상금(국가·공기업)·지연배상금(지방) 산정 — 법정 요율·기준금액·30% 한도를 결정론 적용.

    **국가와 지방은 요율이 다르다**(물품 0.75/1000 ↔ 0.8/1000, 용역 1.25/1000 ↔ 1.3/1000)
    — org_type을 반드시 사용자에게 확인해서 넣어라. 법정 용어도 다르다(국가=지체상금,
    지방=지연배상금).

    **이 도구는 지체일수를 정하지 않는다.** 준공검사 소요기간·검사 불합격 재검사 기간·
    발주기관 귀책 일수 같은 것은 사실 판단이다 — delay_days/excluded_days는 사용자가
    선언한 값으로 계산에 그대로 쓰이고, 응답의 counted_days.disclaimer가 이 사실을 밝힌다.
    면책 사유 해당 여부가 쟁점이면 search_references로 예규·감사원 실무가이드를 찾아라.

    응답 필드:
      term/counterpart_term  기관유형에 따른 법정 용어(+반대편 용어)
      rate                   적용 요율·근거 조문(호까지). inferred=true면 법문이 아니라 우리 해석
      base_amount            계약금액 − 인수분 산출 내역
      counted_days           선언 지체일수 − 선언 면책일수
      amount_raw / cap / amount   한도 적용 전 금액 / 30% 한도 / 최종(한도 적용 후)
      warnings               미선언 항목·한도 적용·용어 비대칭 등 실토
      legal_basis            근거 조문 — get_law_article로 원문 확인 가능

    Args:
        contract_kind: 요율 호와 1:1. "construction"(공사) | "product_manufacture"(물품
            제조·구매) | "product_repair"(물품 수리·가공·대여) | "service"(용역·기타) |
            "military_food"(군용 음·식료품) | "transport_storage"(운송·보관·양곡가공)
        org_type: "national"(국가기관) | "local"(지자체) | "public_corp"(공기업·준정부).
            **추측 금지** — 요율이 달라 틀린 금액이 된다
        contract_amount: 계약금액(원). **장기계속계약이면 총액이 아니라 연차별 계약금액**
        delay_days: 지체일수(총 지체일수 — 면책일수를 포함해서 넣고, 면책분은 아래에 따로)
        excluded_days: 계약상대자 책임 없는 사유 일수(모르면 0으로 두되 응답 경고를 전달하라)
        accepted_portion_amount: 검사를 거쳐 인수한 기성·기납 부분 금액(원)
        design_build_approved: 설계·제조 일괄 + 발주기관 승인이 필요한 물품인지(요율 예외)
    """
    return _post("/penalty/delay", {
        "contract_kind": contract_kind,
        "org_type": org_type,
        "contract_amount": contract_amount,
        "delay_days": delay_days,
        "excluded_days": excluded_days,
        "accepted_portion_amount": accepted_portion_amount,
        "design_build_approved": design_build_approved,
    })


@server.tool(annotations=READ_ONLY)
def delay_exemption_guide(
    contract_kind: Literal["construction", "product_manufacture", "product_repair",
                           "service", "military_food", "transport_storage"],
    ground: Literal["force_majeure", "gov_supplied_material_delay", "owner_caused_delay",
                    "contractor_default_surety", "design_change", "innovative_product_defect",
                    "raw_material_shortage", "sw_requirement_change",
                    "product_owner_side_delay"] | None = None,
) -> dict:
    """지체일수에서 **빼는(불산입) 사유** 지도 — estimate_delay_penalty가 정하지 않는 부분.

    "이 지연은 우리 책임이 아닌데 지체상금을 물어야 하나", "동절기 공사중지 기간도
    지체일수인가", "관급자재가 늦게 와서 늦어졌다" 같은 질문에 쓰라.

    **이 도구는 해당 여부를 판정하지 않는다.** 일반조건 문언 자체가 "계약담당공무원이
    인정할 때"를 요건으로 두므로 판단은 발주기관 몫이다. 도구가 주는 것은 셋이다 —
    ①예규에 있는 사유 목록과 원문 인용 ②각 사유가 인정되려면 **확정돼야 할 사실**
    (must_establish — 사용자와 하나씩 확인하라) ③기재부·행안부 회신 선례.

    쓰는 순서: 이 도구로 사유를 좁힌다 → must_establish를 사용자와 확인한다 →
    불산입 일수가 정해지면 estimate_delay_penalty의 excluded_days에 넣어 다시 계산한다.
    (sw_requirement_change는 해당 일수의 **1/2**만 넣는다 — 예규가 절반만 빼준다.)

    주의: `quote_truncated: true`인 항목은 우리가 회수한 조문 인용이 중간에서 끊긴 것이다
    — 그대로 인용하지 말고 search_references로 전문을 확인하라. 끊긴 문장을 이어서
    지어내면 그것이 이 서버가 막으려는 오답이다.

    Args:
        contract_kind: estimate_delay_penalty와 같은 값. 일반조건 계열(공사/물품/용역)로
            매핑되며, 실제로 계약서에 편입된 일반조건이 진실원임을 응답이 경고한다
        ground: 특정 사유 하나만 상세히 볼 때. 생략하면 그 계약유형의 전체 목록
    """
    params: dict[str, Any] = {"contract_kind": contract_kind}
    if ground:
        params["ground"] = ground
    return _get("/penalty/delay/exemptions", params)


@server.tool(annotations=READ_ONLY)
def check_price_adjustment(
    org_type: Literal["national", "local", "public_corp"],
    contract_date: str,
    check_date: str,
    last_adjustment_date: str | None = None,
    adjustment_rate_pct: float | None = None,
    method_specified_in_contract: Literal["item", "index"] | None = None,
    urgent_exception: bool = False,
    single_item_rate_pct: float | None = None,
    single_item_share_over_5permille: bool | None = None,
    is_construction: bool = False,
    adjustment_base_amount: int | None = None,
    advance_payment_ratio: float | None = None,
) -> dict:
    """물가변동 계약금액 조정(에스컬레이션) 요건 판정 + 산식 적용 — 이행단계 Phase 3.

    "자재값이 올랐는데 계약금액을 올려받을 수 있나", "90일 지났나", "단품 조정 되나"에 쓰라.

    **이 도구는 조정률을 산정하지 못한다.** 품목조정률·지수조정률은 산출내역서와 지수·단가
    원천(한국은행 생산자물가지수 등)으로 계산하는 값인데 이 서버는 그 데이터를 갖고 있지
    않다 — 그러니 `adjustment_rate_pct`는 **사용자·발주기관이 산정한 값**을 받아 쓰고,
    안 주면 요건 ②를 `met: null`로 두고 판정을 보류한다. 없는 값을 지어내지 마라.

    판정하는 것(결정론): ①기간 요건(계약체결일 또는 직전 조정기준일부터 90일 이상)
    ②등락률 3% 문턱 ③**단품 조정 문턱 — 국가·공기업 15%, 지방 10%(2024 개정으로 갈렸다)**
    ④조정 방식 결정 규칙(계약서에 지수조정률 명시가 없으면 품목조정률)
    ⑤조정금액 = 물가변동적용대가 × 조정률, 선금 공제 = 위 값 × 선금급률.

    응답의 verdict: requirements_met / requirements_not_met / exception_path(천재지변·
    원자재 급등 예외 검토 대상 — 인정 주체는 발주기관) / undetermined(조정률 미제공).

    Args:
        org_type: "national"|"local"|"public_corp" — **추측 금지**(단품 문턱이 다르다)
        contract_date: 계약체결일 "YYYY-MM-DD". 장기계속계약은 **제1차계약 체결일**
        check_date: 조정 검토·청구 시점 "YYYY-MM-DD"
        last_adjustment_date: 직전 조정기준일(있으면 기간 기산점이 이쪽으로 바뀐다)
        adjustment_rate_pct: 산정된 품목·지수 조정률(%). 감액도 그대로(음수) 넣어라
        method_specified_in_contract: 계약서에 지수조정률이 명시됐으면 "index", 품목이면
            "item". 모르면 생략 — 기본값(품목조정률)으로 안내하되 그 사실을 응답에 밝힌다
        urgent_exception: 천재지변·원자재 급등으로 90일 이내 조정을 검토하는가
        single_item_rate_pct: 단품 조정 검토 시 해당 자재 가격증감률(%)
        single_item_share_over_5permille: 그 자재가 재료비·노무비·경비 합계액의 1천분의 5를
            초과하는가(산출내역서로 확인 — 우리가 계산하지 못한다)
        is_construction: 공사계약인가(단품 조정은 공사 전용 제도)
        adjustment_base_amount: 물가변동적용대가(원) — 조정기준일 **이후** 이행분의 대가
        advance_payment_ratio: 선금급률(비율, 30%면 0.3)
    """
    body: dict[str, Any] = {
        "org_type": org_type, "contract_date": contract_date, "check_date": check_date,
        "last_adjustment_date": last_adjustment_date,
        "adjustment_rate_pct": adjustment_rate_pct,
        "method_specified_in_contract": method_specified_in_contract,
        "urgent_exception": urgent_exception,
        "single_item_rate_pct": single_item_rate_pct,
        "single_item_share_over_5permille": single_item_share_over_5permille,
        "is_construction": is_construction,
        "adjustment_base_amount": adjustment_base_amount,
        "advance_payment_ratio": advance_payment_ratio,
    }
    return _post("/adjustment/price", body)


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


# ── 구매 버튼 게이트 (2026-08-11, T-2026W33-59) ──────────────────────────────
# Creem 판매자 계정의 **라이브 결제 온보딩·본인검증이 끝나야** 체크아웃이 뜬다.
# 미완이면 상품 링크가 렌더 계층에서만 "Payment Error / Live payments are not
# enabled for your account"를 띄우고 **HTTP는 200에 리다이렉트도 정상** — 상태코드
# 감시로는 원리적으로 못 잡는다. 그러니 켤 때는 반드시 렌더 텍스트로 확인할 것:
#   python3 /data/ops/probe_browser.py text https://creem.io/product/<prod_id>
# 위 문자열이 사라지고 결제 폼이 보일 때 CREEM_CHECKOUT_ENABLED=1 (기본 꺼짐 = fail-closed).
_CHECKOUT_LINKS = {
    "trial7": "https://creem.io/product/prod_4Rg75B2ZKFL8Zs0N9Hqesu",
    "pro30": "https://creem.io/product/prod_4Mh1o1y9oty4l6bFPXlfAR",
    "pro90": "https://creem.io/product/prod_5R2Iw6vbWxlB62La22kcqt",
}
_SOON = ('<span style="color:#a00">결제 개통 준비 중</span>')


def _checkout_open() -> bool:
    return os.environ.get("CREEM_CHECKOUT_ENABLED", "") == "1"


def _buy(slot: str) -> str:
    """구매 셀 꼬리표 — 개통 전에는 죽은 결제 링크 대신 준비 중 표기."""
    if not _checkout_open():
        return _SOON
    return f'<a href="{_CHECKOUT_LINKS[slot]}">구매</a>'


_CLOSED_NOTICE = """<p style="background:#fff6e5;border:1px solid #e0b070;padding:12px 14px;
border-radius:8px"><b>결제 개통 준비 중입니다.</b> 카드 결제사(Creem) 계정 검증이 끝나지
않아 구매 버튼을 열어두지 않았습니다. <b>무료 티어(IP당 50콜/일)로 도구 8종을 지금 그대로
쓸 수 있고</b>, 유료 한도가 당장 필요하시면 <b>contract@sallim.app</b>으로 알려 주세요 —
개통 즉시(또는 수동 발급으로) 키를 보내 드립니다.</p>"""

# 템플릿 치환은 .format()이 아니라 replace를 쓴다 — 아래 pre 블록의 Cursor 설정 예시에
# 중괄호가 들어 있어 format이 깨진다.
_PRICING_HTML = """<!doctype html><html lang="ko"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>계약나침반 MCP — 요금 안내</title>
<body style="font-family:system-ui,sans-serif;max-width:44rem;margin:4rem auto;padding:0 1rem;line-height:1.65">
<h1>🧭 계약나침반 MCP 요금 안내</h1>
<p>한국 공공계약 법령·판례 MCP 서버 — 모든 도구는 LLM 없이 검증 가능한 법적 근거만
반환합니다. 무료로 전 도구를 쓸 수 있고, 유료 키는 <b>한도만</b> 올립니다(기능 차이 없음).</p>
<!--NOTICE-->
<table border="1" cellpadding="8" style="border-collapse:collapse;width:100%">
<tr><th>티어</th><th>일일 한도</th><th>기능</th><th>가격</th></tr>
<tr><td>무료</td><td>IP당 50콜 (UTC 자정 리셋)</td><td>도구 8종 전부</td><td>0원</td></tr>
<tr><td>체험 키</td><td>키당 2,000콜</td><td>동일</td>
<td>7일 $1.00 (약 ₩1,400) — <!--BUY_TRIAL7--></td></tr>
<tr><td>PRO 키</td><td>키당 2,000콜</td><td>동일 + 우선 지원</td>
<td>30일 $7.00 (약 ₩9,500) — <!--BUY_PRO30--><br>
90일 $16.90 (약 ₩23,300) — <!--BUY_PRO90--></td></tr>
</table>
<p><b>카드결제(USD)<!--PAY_SUFFIX--></b> — <b>해외결제 가능한 카드</b>(Visa/Mastercard 등
국제 브랜드)면 개인·법인·정부구매카드 구분 없이 결제됩니다. 결제 완료 화면에서 라이선스
키가 즉시 표시되고(1회) 영수증(인보이스)은 이메일로 발행됩니다. 자동결제(구독) 없음 —
기간 만료 시 무료 티어로 자연 복귀합니다. 결제가 거절되면 해외(온라인)결제 차단 여부를
카드사에 확인해 주세요. 기관 구매·세금계산서 등 별도 서류가 필요하면
<b>contract@sallim.app</b>으로 문의해 주세요.</p>
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


def _pricing_html() -> str:
    """요금 페이지 — 구매 버튼은 요청 시 env로 결정. 개통되면 코드 수정 없이
    .env에 CREEM_CHECKOUT_ENABLED=1 + 서비스 재시작만으로 링크가 살아난다."""
    open_ = _checkout_open()
    return (_PRICING_HTML
            .replace("<!--NOTICE-->", "" if open_ else _CLOSED_NOTICE)
            .replace("<!--BUY_TRIAL7-->", _buy("trial7"))
            .replace("<!--BUY_PRO30-->", _buy("pro30"))
            .replace("<!--BUY_PRO90-->", _buy("pro90"))
            .replace("<!--PAY_SUFFIX-->", "" if open_ else " · 개통 후 기준"))


async def _pricing(request):  # noqa: ANN001
    from starlette.responses import HTMLResponse
    return HTMLResponse(_pricing_html())


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
        customer = obj.get("customer") or {}
        email = customer.get("email", "") if isinstance(customer, dict) else ""
        # 라이선스 미러 vs 자체 발급 (2026-08-09 설계 전환): API로 생성한 상품에는
        # Creem이 토글과 무관하게 라이선스를 안 붙인다(실결제 3회 실측 — payload에
        # license 필드 자체가 없음). 실려 오면 미러, 없으면 우리 키를 자체 발급하고
        # 성공 페이지(/mcp/purchase-success)에서 1회 공개로 전달한다.
        plain, rec = keystore.issue(
            name=plan.get("label", f"Creem {product_id}"),
            days=int(plan.get("days", 30)),
            daily=int(plan.get("daily", 2000)),
            channel="creem",
            amount_krw=int(plan.get("amount_krw", 0)),
            contact=email,
            order_id=_order_ref(),
            key=lic, source="creem_mirror" if lic else "self_issued",
        )
        if plain and not lic:
            _pending_put(_order_ref(), plain)
        return JSONResponse({"ok": True, "key_prefix": rec["key_prefix"],
                             "delivery": "creem_license" if lic else "success_page"})

    if event in ("refund.created", "subscription.expired", "subscription.canceled",
                 "dispute.created"):
        rec = keystore.revoke_by_order(_order_ref())
        return JSONResponse({"ok": True, "revoked": bool(rec)})

    return JSONResponse({"ok": True, "ignored": event})


# ── 자체 발급 키의 성공 페이지 전달 (2026-08-09) ──────────────────────────────
# 평문 키는 대장에 해시로만 남으므로, 웹훅 발급 직후 주문번호→평문을 임시 보관했다가
# 성공 페이지에서 딱 1회 보여주고 지운다. 결제 사실은 쿼리 파라미터를 믿지 않고
# 서버→Creem API 대조로 확인한다(리다이렉트 서명이 문서화돼 있지 않음).
_PENDING_PATH = Path(__file__).parent.parent / "data" / "pending_keys.json"
_PENDING_TTL = 48 * 3600


def _pending_load() -> dict:
    try:
        return _json.loads(_PENDING_PATH.read_text())
    except Exception:
        return {}


def _pending_save(d: dict) -> None:
    _PENDING_PATH.write_text(_json.dumps(d, ensure_ascii=False))
    os.chmod(_PENDING_PATH, 0o600)


def _pending_put(order_ref: str, plain: str) -> None:
    d = _pending_load()
    now = time.time()
    d = {k: v for k, v in d.items() if now - v.get("ts", 0) < _PENDING_TTL}
    d[order_ref] = {"key": plain, "ts": now}
    _pending_save(d)


def _pending_claim(order_ref: str) -> str | None:
    d = _pending_load()
    ent = d.pop(order_ref, None)
    if ent:
        _pending_save(d)
        return ent.get("key")
    return None


def _creem_api_base_key() -> tuple[str, str]:
    live = os.environ.get("CREEM_LIVE_MODE", "") == "1"
    key = os.environ.get("CREEM_API_KEY", "")
    return ("https://api.creem.io/v1" if live else "https://test-api.creem.io/v1", key)


async def _purchase_success(request):  # noqa: ANN001
    from starlette.responses import HTMLResponse
    checkout_id = request.query_params.get("checkout_id", "")
    order_id = request.query_params.get("order_id", "")
    base, api_key = _creem_api_base_key()

    def _page(title: str, body: str, code: int = 200):
        return HTMLResponse(
            f'<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{title}</title><style>body{{font-family:system-ui,sans-serif;'
            f'max-width:640px;margin:12vh auto 0;padding:0 24px;line-height:1.7}}'
            f'code{{background:#f2f2f2;padding:.3em .5em;border-radius:4px;'
            f'word-break:break-all;display:inline-block}}</style></head>'
            f'<body><h1>{title}</h1>{body}'
            f'<p>문의: contract@sallim.app</p></body></html>', status_code=code)

    if not (checkout_id and order_id and api_key):
        return _page("확인 불가", "<p>주문 정보가 없습니다. 결제 완료 화면에서 다시 이동해 주세요.</p>", 400)
    # 결제 사실을 Creem에 직접 확인 — 파라미터 위조로는 남의 키를 못 꺼낸다
    # (주문번호 일치 + paid 상태 + 미수령 상태가 전부 맞아야 1회 공개).
    try:
        async with httpx.AsyncClient(timeout=10) as cli:
            r = await cli.get(f"{base}/checkouts", params={"checkout_id": checkout_id},
                              headers={"x-api-key": api_key})
        chk = r.json() if r.status_code == 200 else {}
    except Exception:
        chk = {}
    order = chk.get("order") or {}
    if not (order.get("id") == order_id and order.get("status") == "paid"):
        return _page("결제 확인 실패", "<p>결제 확인에 실패했습니다. 잠시 후 새로고침하거나 문의해 주세요.</p>", 404)
    def _key_page(k: str):
        return _page("결제 완료 — 라이선스 키",
                     f"<p>아래 키는 <b>이 화면에서 딱 한 번만</b> 표시됩니다. 지금 복사해 보관하세요.</p>"
                     f"<p><code>{k}</code></p>"
                     f"<p>사용법: MCP 클라이언트 헤더 <code>Authorization: Bearer &lt;키&gt;</code>. "
                     f"자세한 안내는 <a href='{PRICING_URL}'>요금 페이지</a> 참조.</p>")

    plain = _pending_claim(f"creem-{order_id}")
    if plain:
        return _key_page(plain)
    import keystore
    rec = keystore.find_by_order(f"creem-{order_id}")
    if rec:
        return _page("이미 발급된 주문",
                     f"<p>이 주문의 키(접두 <code>{rec.get('key_prefix', '')}</code>)는 이미 표시됐습니다. "
                     f"분실하셨으면 주문번호와 함께 문의해 주세요.</p>")
    # 웹훅 미도달 폴백(2026-08-10 라이브 컷오버): 라이브 모드 웹훅 미등록·whsec 불일치로
    # checkout.completed를 놓쳐도 위에서 Creem API로 paid를 직접 확인했으므로 여기서 발급한다.
    # keystore.issue가 order_id 멱등이라 웹훅이 뒤늦게 도착해도 중복 발급은 없다.
    product = chk.get("product") or (order.get("product") if isinstance(order, dict) else None) or {}
    product_id = product.get("id") if isinstance(product, dict) else str(product or "")
    plan = _creem_plans().get(str(product_id), {})
    if plan:
        customer = chk.get("customer") or {}
        email = customer.get("email", "") if isinstance(customer, dict) else ""
        plain, rec = keystore.issue(
            name=plan.get("label", f"Creem {product_id}"),
            days=int(plan.get("days", 30)), daily=int(plan.get("daily", 2000)),
            channel="creem", amount_krw=int(plan.get("amount_krw", 0)),
            contact=email, order_id=f"creem-{order_id}", source="self_issued")
        if plain:
            return _key_page(plain)
    return _page("처리 중", "<p>결제 확인은 됐지만 키 발급이 아직입니다. 몇 초 후 새로고침해 주세요.</p>", 202)


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
for _sp in ("/purchase-success", "/mcp/purchase-success"):
    server.custom_route(_sp, methods=["GET"], include_in_schema=False)(_purchase_success)


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
