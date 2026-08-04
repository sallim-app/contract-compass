import logging
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from backend.config import get_settings, BASE_DIR

logger = logging.getLogger("contract-compass")
from backend.api.v1 import filter, docs, admin, feedback, ask, glossary, law, classify, status

DIST_DIR = BASE_DIR / "frontend" / "dist"

app = FastAPI(
    title="계약나침반 — 공공계약 방법 결정 도우미",
    description="국가계약법·지방계약법 기반 계약방법 결정 지원 서비스",
    version="1.0.0",
    # 2026-08-04 보안 감사: /docs·/openapi.json이 contract.naru.build에 공개돼
    # 있었다(실측 200). 무인증 LLM 서비스의 전 라우트·스키마 지도이고 관리자
    # 라우트 이름까지 노출된다. SPA는 고정 경로만 부르므로 동작 영향 없다.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

settings = get_settings()

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 이용 계측 — 주요 API 경로 히트만 jsonl 기록. 본문 미기록, IP는 마스킹(개인 식별 없음).
_USAGE_LOG = BASE_DIR / "logs" / "usage_events.jsonl"
_USAGE_PREFIXES = (
    "/api/v1/filter/step1", "/api/v1/filter/step2",
    "/api/v1/ask",
)


# CF 엣지 캐시 대상(2026-07-30 P2): 결정론 읽기 GET — 성공 응답에만 Cache-Control을 실어
# CF 캐시 룰(contract.naru.build /api/v1/law/*)이 엣지에서 재사용하게 한다.
# 조문(article)은 재색인 전까지 불변에 가까워 1일, 검색·판례 프록시는 1시간.
# 캐시 히트는 오리진(1 vCPU)에 도달하지 않아 레이트리밋·임베딩 비용도 안 든다.
_EDGE_CACHE_TTL = {
    "/api/v1/law/article": 86400,
    "/api/v1/law/article-asof": 86400,  # 과거 시점 본문은 불변
    "/api/v1/law/search": 3600,
    "/api/v1/law/references": 3600,
    "/api/v1/law/cases": 3600,
    "/api/v1/law/case": 86400,  # 판례 본문은 사실상 불변
}


@app.middleware("http")
async def _usage_middleware(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if request.method == "GET" and response.status_code == 200:
        ttl = _EDGE_CACHE_TTL.get(path)
        if ttl:
            response.headers["Cache-Control"] = f"public, max-age={ttl}"
    if response.status_code < 400 and any(path.startswith(p) for p in _USAGE_PREFIXES):
        try:
            from datetime import datetime, timezone
            import json as _json
            from backend.services.usage_logger import extract_client_meta, extract_device_id, rotate_if_oversize
            client_ip, user_agent = extract_client_meta(request)
            device_id = extract_device_id(request)
            _USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
            rotate_if_oversize(_USAGE_LOG)  # 50MB 초과 시 날짜 파일로 회전(데이터 보존)
            with _USAGE_LOG.open("a", encoding="utf-8") as f:
                f.write(_json.dumps({
                    "ts": datetime.now(timezone.utc).isoformat(), "path": path,
                    "client_ip": client_ip, "user_agent": user_agent, "device_id": device_id,
                }) + "\n")
        except Exception:  # noqa: BLE001 — 계측 실패가 서비스에 영향 주지 않게
            pass
    return response


@app.exception_handler(Exception)
async def _unhandled_exception(request: Request, exc: Exception):
    """미처리 예외는 서버에만 전체 스택 로깅, 클라이언트엔 일반 메시지(내부정보 노출 차단).

    FastAPI의 HTTPException/검증오류(4xx)는 이 핸들러를 거치지 않으므로 기존 동작 유지.
    """
    logger.exception("unhandled error: %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "내부 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."},
    )


app.include_router(filter.router, prefix="/api/v1")
app.include_router(docs.router, prefix="/api/v1")
app.include_router(admin.router, prefix="/api/v1")
app.include_router(feedback.router, prefix="/api/v1")
app.include_router(ask.router, prefix="/api/v1")
app.include_router(glossary.router, prefix="/api/v1")
app.include_router(law.router, prefix="/api/v1")
app.include_router(classify.router, prefix="/api/v1")
app.include_router(status.router, prefix="/api/v1")
app.include_router(status._rules_router, prefix="/api/v1")  # 룰 공개 조회


@app.on_event("startup")
async def _warmup() -> None:
    """모델·인덱스 사전 로드 — 첫 사용자 cold start(약 30초) 제거."""
    import asyncio
    try:
        from backend.api.deps import get_rag_service
        from backend.services.embedding import _get_model
        await asyncio.to_thread(lambda: _get_model().encode(["워밍업"], show_progress_bar=False))
        rag = get_rag_service()
        await asyncio.to_thread(rag.search_knowledge_web, "계약방법", "construction", 3)
        print("[warmup] 임베딩 모델 + RAG 인덱스 로드 완료")
    except Exception as e:  # 워밍업 실패는 서비스 기동을 막지 않음
        print(f"[warmup] 건너뜀(무시): {e}")


@app.get("/health")
async def health():
    return {"status": "ok", "llm_provider": settings.llm_provider}


@app.get("/ready")
async def ready():
    """내용 헬스(readiness) — 인덱스·코퍼스의 실체를 판정 (/health는 프로세스 생존만 봄).

    failures(치명, HTTP 503) / warnings(주의, 200 유지) 분리.
    """
    from datetime import datetime as _dt
    failures, warnings, info = [], [], {}
    # 1) 인덱스 실체 — 총청크·핵심 컬렉션 하한 (공개 코퍼스 기준)
    try:
        from backend.api.deps import get_rag_service
        rag = get_rag_service()
        total = sum(c.count() for c in rag._client.list_collections())
        crit = {}
        for label, name, floor in (("law", settings.collection_law_articles, 1500),
                                   ("admin_rules", settings.collection_admin_rules, 300)):
            try:
                n = rag._client.get_collection(name).count()
            except Exception:
                n = 0
            crit[label] = n
            if n < floor:
                failures.append(f"{label} 컬렉션 청크 {n} < 하한 {floor}")
        info["chunks_total"], info["critical"] = total, crit
        if total < 2500:
            failures.append(f"총 청크 {total} < 하한 2500")
        if rag._bm25_data is None:
            warnings.append("BM25 인덱스 미로드 — dense 단독 검색으로 degrade")
    except Exception as e:
        failures.append(f"인덱스 접근 실패: {type(e).__name__}: {e}")
    # 2) 서빙 코퍼스 신선도 — 재색인 스크립트가 남긴 index_status.json의 indexed_at 기준.
    #    코퍼스는 수동 재색인이라 SLA가 아니므로 warning으로만 다룬다(파일 부재 = 신호 없음).
    try:
        from backend.services.index_status import read_index_status, evaluate_corpus_freshness
        _cinfo, _cwarn = evaluate_corpus_freshness(read_index_status(), _dt.now().timestamp())
        info.update(_cinfo)
        warnings.extend(_cwarn)
    except Exception as e:
        warnings.append(f"코퍼스 신선도 판독 실패: {e}")
    status_ = "degraded" if failures else ("warn" if warnings else "ok")
    body = {"status": status_, "failures": failures, "warnings": warnings, "info": info}
    if failures:
        return JSONResponse(status_code=503, content=body)
    return body


# React SPA 정적 서빙 (루트 경로 — API 라우터가 먼저 등록돼 /api/*는 영향 없음)
if DIST_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(DIST_DIR / "assets")), name="assets")

    # index.html은 캐시 금지 — 진입점 HTML이 캐시되면 브라우저가 옛 JS 해시를 계속 참조
    _NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str):
        candidate = DIST_DIR / full_path
        if full_path and candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(DIST_DIR / "index.html"), headers=_NO_CACHE)
