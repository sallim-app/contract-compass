import logging
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from backend.config import get_settings, BASE_DIR

logger = logging.getLogger("contract-compass")
from backend.api.v1 import filter, docs, admin, feedback, ask, glossary, law, classify, status, penalty, adjustment

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
)  # /api/v1/ask는 종료(D-2026W33-22) — 410만 반환하므로 계측 대상이 아니다


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
app.include_router(penalty.router, prefix="/api/v1")  # 이행단계 — 지체상금·지연배상금
app.include_router(adjustment.router, prefix="/api/v1")  # 이행단계 — 물가변동 계약금액 조정


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
    # 3) 배포된 프런트 산출물 신선도 — dist는 .gitignore인데 여기서 서빙하므로, 빌드를
    #    잊으면 커밋한 프런트 변경이 라이브에 없다(2026-08-10 실사고: 허브 링크 4일 미반영
    #    → 롱테일 113건 고아). 전부 warning — 이 판독으로 503을 내지 않는다.
    try:
        from backend.services.dist_status import scan_dist, evaluate_dist_freshness
        _dinfo, _dwarn = evaluate_dist_freshness(
            scan_dist(DIST_DIR, BASE_DIR / "frontend" / "public", BASE_DIR),
            _dt.now().timestamp())
        info.update(_dinfo)
        warnings.extend(_dwarn)
    except Exception as e:
        warnings.append(f"dist 신선도 판독 실패: {e}")
    status_ = "degraded" if failures else ("warn" if warnings else "ok")
    body = {"status": status_, "failures": failures, "warnings": warnings, "info": info}
    if failures:
        return JSONResponse(status_code=503, content=body)
    return body


# ── 소프트404 차단 (T-2026W33-11) ────────────────────────────────────────────
# 종전 serve_spa는 미존재 경로를 전부 index.html 200으로 돌려줬다(라이브 실측:
# /g/anything.html·/totally-bogus-xyz 모두 200). 프런트에 client-side 라우터가
# 없고(해시 상태전환뿐) 정당 HTML 경로는 `/`와 생성된 정적 파일뿐이라, 롱테일
# 113건 색인 직후인 지금은 크롤예산 낭비·색인오염이 실질 위험이다.
# 그래서 ① 확장자가 있는 경로 ② g/·assets/ 네임스페이스는 실물이 없으면 404,
# 그 외 확장자 없는 경로는 종전대로 SPA 폴백(미래에 라우터가 붙어도 안 깨지게).
_SPA_404_NAMESPACES = ("g", "assets")


def spa_should_404(full_path: str) -> bool:
    """실물이 없을 때 SPA 폴백(200) 대신 404를 줘야 하는 경로인가."""
    path = full_path.strip("/")
    if not path:
        return False
    head = path.split("/", 1)[0]
    if head in _SPA_404_NAMESPACES:
        return True
    return "." in path.rsplit("/", 1)[-1]


def resolve_dist_path(full_path: str, dist: Path = None):
    """dist 안으로 한정한 실경로 — 밖으로 나가는 경로(`../`)는 None."""
    base = (dist or DIST_DIR).resolve()
    try:
        candidate = (base / full_path).resolve()
    except OSError:
        return None
    return candidate if candidate == base or base in candidate.parents else None


# ── 법적 고지 실물 페이지 (2026-08-16) ─────────────────────────────────────
# Creem 심사 선행조건 — 종전엔 /legal/privacy·/legal/terms가 SPA 폴백으로 홈 200을
# 돌려줘 "문서 없음"으로 판정됐다(사장님 Computer Use 실사, D-2026W33-21). 문서 정본은
# realty-mcp server.py _LEGAL_HTML과 동일 내용(적용 범위가 살림 제품군 전체를 명시) —
# 여기 사본을 두는 이유는 심사가 스토어 도메인(contract.sallim.app) 안의 URL을 요구해서다.
# 내용 개정 시 두 곳을 함께 갱신할 것. SPA 캐치올보다 먼저 등록돼야 한다(등록 순서 승자).
_LEGAL_HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>개인정보처리방침 · 이용약관 — 살림(Sallim)</title>
<style>body{font-family:system-ui,-apple-system,sans-serif;max-width:720px;margin:40px auto;padding:0 20px;line-height:1.7;color:#1a202c}
h1{font-size:1.4rem}h2{font-size:1.15rem;margin-top:2rem}h3{font-size:1rem;margin-top:1.4rem}
small{color:#64748b}ul{padding-left:1.2em}</style></head><body>
<h1>개인정보처리방침 · 이용약관</h1>
<p><small>적용 범위: 살림(Sallim) 제품군 — sallim.app과 그 하위 도메인(realty.sallim.app,
contract.sallim.app, eatlog.sallim.app) 및 데이터 블로그(realty.naru.build/blog).
시행일 2026-08-15.</small></p>

<h2 id="privacy">개인정보처리방침</h2>
<h3>1. 수집하는 항목과 방법</h3>
<ul>
<li><b>주문·문의</b>: 이메일로 주문·문의하실 때 이메일 주소와 문의 내용을 받습니다.</li>
<li><b>카드 결제</b>(contract.sallim.app): 결제는 결제대행사 Creem이 처리하며 카드번호 등
결제수단 정보는 저희 서버에 저장되지 않습니다. 결제 완료 시 결제대행사로부터 이메일 주소와
주문 식별자를 전달받아 이용권 발급·문의 대응에 사용합니다.</li>
<li><b>API 키</b>: 발급된 키는 원문이 아닌 단방향 해시(sha256)로만 저장합니다.</li>
<li><b>자동 수집</b>: 웹서버 접속 기록(IP 주소·접속 시각·요청 경로·브라우저 정보)과
API 호출 기록(도구명·시각·키 식별자)을 보안, 오류 진단, 사용량 한도 관리 목적으로 남깁니다.</li>
<li><b>방문 통계</b>: 자체 호스팅 분석 도구(umami)로 쿠키 없이 익명 통계만 수집하며,
개인을 식별하지 않습니다.</li>
</ul>
<h3>2. 이용 목적</h3>
<p>이용권 발급·유효기간 관리, 문의·환불 대응, 부정 사용 방지, 서비스 개선.</p>
<h3>3. 보유·파기</h3>
<p>주문·이용권 정보는 이용기간 종료 후 환불·분쟁 대응에 필요한 동안 보관하고, 목적이 다하면
지체 없이 파기합니다. 접속 기록은 서버 운영 주기에 따라 순환 삭제됩니다.</p>
<h3>4. 제3자 제공·처리 위탁</h3>
<p>개인정보를 제3자에게 제공하지 않습니다. 서비스 운영을 위해 결제 처리(Creem)와 콘텐츠
전송·보안(Cloudflare)을 위탁하며, 두 업체는 국외 사업자입니다.</p>
<h3>5. 이용자 권리</h3>
<p>본인 정보의 열람·정정·삭제를 아래 문의처로 요청하실 수 있으며 지체 없이 처리합니다.</p>
<h3>6. 문의</h3>
<p>sallimapp@gmail.com</p>

<h2 id="terms">이용약관</h2>
<h3>1. 서비스의 성격</h3>
<p>공공데이터 기반 정보 제공 도구(웹·MCP API)입니다. <b>투자·법률 자문이 아니며</b>,
정보의 정확성·완전성을 보장하지 않습니다. 이용 판단과 그 결과는 이용자 책임입니다.</p>
<h3>2. 이용권</h3>
<p>유료 이용권은 기간제(예: 7·30·90일)이며 <b>자동 갱신·자동 청구가 없습니다</b>.
연장은 재구매로 합니다. 키의 공유·재판매는 금지합니다.</p>
<h3>3. 환불</h3>
<p>결제 오류·중복 결제는 전액 환불합니다. 서비스가 종료되는 경우 잔여 기간을 환불합니다.
환불 문의는 이메일로 받습니다.</p>
<h3>4. 금지 행위</h3>
<p>서비스 안정성을 해치는 과도한 자동 호출, 원천 데이터의 이용조건을 위반하는 사용.</p>
<h3>5. 책임 한계</h3>
<p>무료 도구는 있는 그대로 제공됩니다. 유료 이용권 관련 책임은 결제 금액을 한도로 합니다.</p>
<h3>6. 약관의 변경</h3>
<p>변경 시 이 페이지에 게시합니다.</p>
<p><small>운영자: 살림(Sallim) · 문의 sallimapp@gmail.com</small></p>
</body></html>"""

_LEGAL_HEADERS = {"cache-control": "public, max-age=3600"}


@app.get("/legal/privacy", include_in_schema=False)
@app.get("/legal/terms", include_in_schema=False)
@app.get("/privacy", include_in_schema=False)
@app.get("/terms", include_in_schema=False)
async def serve_legal():
    return HTMLResponse(_LEGAL_HTML, headers=_LEGAL_HEADERS)


# React SPA 정적 서빙 (루트 경로 — API 라우터가 먼저 등록돼 /api/*는 영향 없음)
if DIST_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(DIST_DIR / "assets")), name="assets")

    # index.html은 캐시 금지 — 진입점 HTML이 캐시되면 브라우저가 옛 JS 해시를 계속 참조
    _NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str):
        candidate = resolve_dist_path(full_path) if full_path else None
        if candidate is not None:
            if candidate.is_file():
                return FileResponse(str(candidate))
            # 디렉터리 진입(`/g/`)은 실물 허브 페이지로 합류시킨다 — 껍데기 200 금지.
            # sitemap·canonical이 /g/index.html이라 URL을 하나로 모으는 쪽이 맞다.
            if candidate.is_dir() and (candidate / "index.html").is_file():
                rel = candidate.relative_to(DIST_DIR.resolve())
                return RedirectResponse(f"/{rel.as_posix()}/index.html", status_code=301)
        if spa_should_404(full_path):
            # 껍데기를 보여주되 상태코드로는 없음을 말한다(soft 404 회피).
            return FileResponse(str(DIST_DIR / "index.html"), status_code=404, headers=_NO_CACHE)
        return FileResponse(str(DIST_DIR / "index.html"), headers=_NO_CACHE)
