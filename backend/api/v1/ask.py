"""웹 Q&A(/api/v1/ask) — **종료된 엔드포인트의 묘비(tombstone)**.

D-2026W33-22(T-2026W33-180)로 웹 계약 Q&A 챗봇을 폐지했다. 이 파일에 있던
RAG+LLM 생성 경로(950줄)·인메모리 답변 캐시·chat_access 로그인 게이트는 전부 제거됐고,
남은 것은 **왜 없어졌는지 말하는 410 응답**뿐이다.

왜 라우터를 통째로 지우지 않는가 — 이 저장소의 원칙 2(폴백·절단 은폐 금지) 때문이다.
사용자 브라우저에 캐시된 옛 SPA 번들은 한동안 `/api/v1/ask/stream`을 계속 부른다.
그때 404(경로가 원래 없음)를 주면 "장애"로 읽히고, 우리가 무엇을 닫았는지 아무도
모른다. 410 Gone + 대체 경로(MCP)를 본문에 적으면 클라이언트도 사람도 사실을 안다.

**되살리지 마라.** 판정·법령 조회는 MCP 축(`/mcp`, 무LLM 도구 11종)이 담당한다.
웹 백엔드가 LLM 생성을 다시 하면 일일 캡·비용 축이 갈린다(CLAUDE.md 규칙 1).
"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/ask", tags=["ask"])

_GONE = {
    "error": "endpoint_removed",
    "message": (
        "웹 계약 Q&A 챗봇은 2026-08-15자로 종료되었습니다. "
        "계약방법 판정은 계약방법 결정 위저드를, 법령 조문·판례 조회는 MCP 서버를 이용하세요."
    ),
    "removed_on": "2026-08-15",
    "alternatives": {
        "wizard": "https://contract.sallim.app/#decide",
        "mcp": "https://contract.sallim.app/mcp",
        "mcp_docs": "https://github.com/sallim-app/contract-compass/blob/master/docs/MCP.md",
    },
}


def _gone() -> JSONResponse:
    # Sunset 헤더 = RFC 8594. 캐시된 옛 클라이언트가 기계적으로도 판별할 수 있게 남긴다.
    return JSONResponse(_GONE, status_code=410, headers={"Sunset": "Fri, 15 Aug 2026 00:00:00 GMT"})


@router.post("")
async def ask_question_removed() -> JSONResponse:
    return _gone()


@router.post("/stream")
async def ask_stream_removed() -> JSONResponse:
    return _gone()
