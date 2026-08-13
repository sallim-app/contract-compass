"""법령 조문 원문 조회 — 법령 조문 컬렉션에서 단일 조문 조회."""
import re
from datetime import datetime
from functools import lru_cache
from fastapi import APIRouter, Depends, Query, HTTPException, Request
from pydantic import BaseModel
import chromadb

from backend.api.deps import get_rag_service
from backend.config import get_settings
from backend.services.crossref import detect_crossref_anomalies
from backend.services.law_history import (
    LawVersion, extract_article, neighbors, parse_versions, pick_asof,
    resolve_official_name,
)

router = APIRouter(prefix="/law", tags=["law"])

# 약칭/모호한 표현 → DB에 저장된 정규 law_name
# DB는 "국가계약법 시행령" 등 약칭으로 저장됨
_LAW_ALIASES = {
    "시행령": "국가계약법 시행령",
    "시행규칙": "국가계약법 시행규칙",
    "공기업·준정부기관 계약사무규칙": "공기업ㆍ준정부기관 계약사무규칙",
    "공기업ㆍ준정부기관 계약사무규칙": "공기업ㆍ준정부기관 계약사무규칙",
}


@lru_cache(maxsize=1)
def _get_collection():
    settings = get_settings()
    client = chromadb.PersistentClient(settings.chroma_path)
    from backend.services.embedding import GeminiEmbeddingFunction
    return client.get_collection(settings.collection_law_articles,
                                 embedding_function=GeminiEmbeddingFunction())


class LawArticleResponse(BaseModel):
    law_name: str
    article: str
    content: str
    law_ref: str
    # 법률 자체의 미정비 상호인용 경고(원문은 그대로) — 없으면 빈 리스트
    notes: list[dict] = []


class LawSearchHit(BaseModel):
    law_name: str
    article: str
    content: str
    snippet: str
    law_ref: str
    # 삭제·폐지 조문 경고(T-2026W32-184) — 스텁이면 인용 금지 안내, 아니면 생략
    note: str | None = None
    # 시맨틱 폴백 산출 표시 — 키워드 매치가 아님을 에이전트에게 공시
    matched_by: str | None = None


# 원문 청크의 항·호·목 표지 중복 아티팩트("① ①", "3. 3.", "가. 가.") 정규화.
# 색인 시점 파싱 잔재 — 재색인 없이도 API 반환 시점에 정리한다.
_DUP_MARKER_RE = re.compile(
    r"([①-⑳])\s*\1|(\d{1,2}\.)\s*\2|([가-힣]\.)\s*\3"
)


def _clean_markers(text: str) -> str:
    return _DUP_MARKER_RE.sub(lambda m: m.group(1) or m.group(2) or m.group(3), text or "")


# 실무 약어 → 정식 검색어 (glossary.json aliases가 단일 소스, 로드 실패 시 최소셋)
@lru_cache(maxsize=1)
def _abbrev_map() -> dict[str, str]:
    m = {"종심제": "종합심사낙찰제", "적심": "적격심사", "예가": "예정가격"}
    try:
        import json as _json
        from backend.config import BASE_DIR
        for e in _json.loads((BASE_DIR / "data" / "glossary.json").read_text(encoding="utf-8")):
            term = e.get("term", "")
            for a in e.get("aliases") or []:
                if a and a != term:
                    m.setdefault(a, term)
    except Exception:
        pass
    return m


def _keyword_variants(keyword: str) -> list[str]:
    """검색 키워드 변형 — 원문 그대로 → 공백 접합 → 약어 확장 순으로 시도."""
    out: list[str] = []
    for cand in (keyword, keyword.replace(" ", ""), _abbrev_map().get(keyword.replace(" ", ""), "")):
        if cand and cand not in out:
            out.append(cand)
    return out


def _keyword_tokens(keyword: str) -> list[str]:
    """다단어 질의를 토큰 목록으로 — 각 토큰은 약어 확장 적용, 1글자 토큰 제외."""
    abbrev = _abbrev_map()
    tokens: list[str] = []
    for t in keyword.split():
        t = abbrev.get(t, t)
        if len(t) >= 2 and t not in tokens:
            tokens.append(t)
    return tokens


# 시맨틱 폴백 관련성 하한 (T-2026W32-184) — 코사인 거리(0~2)가 이보다 멀면 버린다.
# 근거 실측(2026-08-09, MiniLM 384d): 관련 질의 0.70~0.81 / 역외 0.92+ / 무의미 1.01+.
_SEMANTIC_MAX_DIST = 0.90

# 삭제 스텁 조문 판정 (T-2026W32-184) — 제목 줄을 뺀 본문이 "삭제 <YYYY.M.D>"뿐인 청크.
# 조·항 표지("제64조", "② ②", "3.")가 앞에 붙는 변형을 전부 흡수한다(코퍼스 실측).
_DELETED_STUB_RE = re.compile(
    r"^(?:(?:제\d+조(?:의\d+)?|[①-⑳]|\d{1,2}\.)\s*)*삭제\s*<\d{4}\.\s*\d{1,2}\.\s*\d{1,2}\.?>$"
)


def _deleted_note(doc: str) -> str | None:
    """삭제·폐지 스텁이면 인용 금지 경고문, 아니면 None — 모든 검색 경로 공통."""
    body = (doc.split("\n", 1)[1] if "\n" in doc else doc).strip()
    body = re.sub(r"\s+", " ", body)
    if _DELETED_STUB_RE.match(body):
        return "삭제된 조문 — 현행 법령에 규정이 없다. 판단 근거로 인용하지 말 것."
    return None


# ── 조문 미발견 응답 (T-2026W33-146) ────────────────────────────────────────
# 기치② "못 봄 ≠ 없음": 이 코퍼스는 공공계약 특화라 민사집행법 같은 법령은 애초에
# 담고 있지 않다. 그런데 "제229조 조문을 찾을 수 없습니다"처럼 답하면 에이전트는
# **그런 조문이 없다**고 읽고 판례 참조조문을 틀렸다고 말하거나 자체 지식으로
# 조용히 폴백한다(2026-08-14 Claude 탐침 실측). 코퍼스 밖 법령과 없는 조문을
# 서로 다른 메시지로 갈라내고, 법령명을 지우지 않는다.
@lru_cache(maxsize=1)
def _corpus_law_names() -> tuple[str, ...]:
    """코퍼스에 실재하는 법령명 목록 — 메타데이터 전수 1회 스캔 후 프로세스 캐시."""
    try:
        metas = _get_collection().get(include=["metadatas"]).get("metadatas") or []
    except Exception:  # noqa: BLE001 — 목록 산출 실패가 404 응답 자체를 막지 않게
        return ()
    return tuple(sorted({(m.get("law_name") or "").strip() for m in metas if m.get("law_name")}))


def _corpus_name_keys() -> set[str]:
    """코퍼스 법령명의 비교용 키 집합(약칭·정식명 양쪽, 공백·중점 제거)."""
    from backend.config import BASE_DIR
    from backend.services.law_history import _cached_name_map, norm_name
    name_map = _cached_name_map(str(BASE_DIR / "tools" / "laws"))
    keys: set[str] = set()
    for n in _corpus_law_names():
        keys.add(norm_name(n))
        keys.add(norm_name(name_map.get(norm_name(n), n)))
    return keys


def _law_in_corpus(target_law: str) -> bool:
    """사용자가 부른 법령명이 코퍼스에 있는가 — 약칭·정식명·표기 흔들림 흡수."""
    if not target_law:
        return False
    from backend.config import BASE_DIR
    from backend.services.law_history import _cached_name_map, norm_name
    name_map = _cached_name_map(str(BASE_DIR / "tools" / "laws"))
    q = norm_name(target_law)
    keys = _corpus_name_keys()
    return q in keys or norm_name(name_map.get(q, target_law)) in keys


def _article_not_found(ref: str, article: str, target_law: str,
                       also_in: list[str] | None = None) -> HTTPException:
    """조문 미발견 404 — 코퍼스 밖 법령(모름)과 없는 조문(부존재)을 분리해 알린다.

    also_in: 그 조문번호를 가진 **다른** 법령들(있으면 재질의 단서로 실어 보낸다).
    """
    in_corpus = _law_in_corpus(target_law)
    laws = list(_corpus_law_names())
    if target_law and not in_corpus:
        message = (f"'{ref}'을(를) 조회하지 못했습니다 — '{target_law}'은(는) 이 서버 코퍼스에 "
                   f"없는 법령입니다(공공계약 특화 코퍼스, 법령 {len(laws)}건). "
                   f"{article}이(가) 존재하지 않는다는 뜻이 아닙니다.")
        hint = (f"**'{article}'이 없다고 말하지 마라** — 우리가 안 가지고 있을 뿐이다. "
                f"'{target_law}'은 이 코퍼스 범위 밖임을 사용자에게 밝히고, 조문 원문이 필요하면 "
                f"get_law_article_asof(코퍼스 밖 법령도 law.go.kr 연혁에서 직접 조회한다) 또는 "
                f"law.go.kr을 안내하라. 판례 참조조문 교차확인 중이었다면 그 참조조문이 "
                f"틀렸다고 판단하지 말 것.")
    elif target_law:
        message = (f"'{ref}'을(를) 조회하지 못했습니다 — '{target_law}'은(는) 코퍼스에 있으나 "
                   f"{article}은(는) 없습니다.")
        hint = (f"법령명은 맞으니 조문번호를 확인하라. search_law로 '{target_law}'의 관련 조문을 "
                f"검색하거나, 개정으로 조문번호가 바뀌었을 수 있으니 get_law_article_asof로 "
                f"당시 시점을 조회하라.")
    else:
        message = (f"'{ref}'에서 법령명을 식별하지 못했습니다 — {article}만으로는 어느 법령의 "
                   f"조문인지 특정할 수 없습니다.")
        hint = ("법령명을 붙여 다시 호출하라(예: '국가계약법 시행령 제26조'). "
                "corpus_laws에 조회 가능한 법령명이 들어 있다.")
    others = [n for n in (also_in or []) if n]
    if others:
        uniq = sorted(set(others))
        message += f" (같은 조문번호를 가진 코퍼스 법령: {', '.join(uniq)})"
    return HTTPException(404, {
        "error": "article_not_found" if target_law else "law_not_specified",
        "message": message,
        "ref": ref,
        "law_name": target_law,
        "article": article,
        "law_in_corpus": in_corpus,
        "hint": hint,
        "article_found_in_other_laws": sorted(set(others)),
        "corpus_laws": laws,
    })


def _make_snippet(text: str, q: str, around: int = 80) -> str:
    idx = text.find(q)
    if idx < 0:
        return text[:200].strip()
    start = max(0, idx - around)
    end = min(len(text), idx + len(q) + around)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return prefix + text[start:end].strip() + suffix


@router.get("/article", response_model=LawArticleResponse)
def get_article(ref: str = Query(..., min_length=2, max_length=100)):
    """조문 참조 문자열로 원문 조회. 예: '시행령 제30조', '국가계약법 시행령 제26조 제1항'."""
    article_match = re.search(r"제\d+조(?:의\d+)?", ref)
    if not article_match:
        raise HTTPException(404, "조문번호(제N조)를 찾을 수 없습니다")
    article = article_match.group(0)

    law_part = ref[: article_match.start()].strip().rstrip("ㆍ·,")
    target_law = _LAW_ALIASES.get(law_part, law_part) if law_part else ""

    col = _get_collection()
    results = col.get(
        where={"article_titles": article},
        include=["documents", "metadatas"],
    )
    docs = results.get("documents") or []
    metas = results.get("metadatas") or []
    # 조문번호가 코퍼스 어디에도 없을 때 — 갈림길은 "그 번호가 아무 법령에나 있느냐"가
    # 아니라 "사용자가 부른 법령을 우리가 갖고 있느냐"다(T-2026W33-146).
    if not docs:
        raise _article_not_found(ref, article, target_law)

    if not target_law:
        raise _article_not_found(ref, article, "")

    # 1단계: law_name 정확 일치
    best = None
    for doc, meta in zip(docs, metas):
        if (meta.get("law_name") or "") == target_law:
            best = (doc, meta)
            break
    # 2단계: target_law가 law_name에 포함 (예: "국가계약법" → "국가계약법 시행령" 매치 방지를 위해 같은 접미사 확인)
    if best is None:
        for doc, meta in zip(docs, metas):
            law_name = meta.get("law_name") or ""
            # target_law의 마지막 토큰(시행령/시행규칙/법 등)이 law_name 끝부분과 일치할 때만 채택
            if law_name == target_law:
                best = (doc, meta)
                break
            # "국가계약법" 입력 시 "국가계약법 시행령"으로 가지 않도록: target이 law_name보다 길거나 같을 때만 부분 매치 허용
            if len(target_law) >= len(law_name) and law_name and law_name in target_law:
                best = (doc, meta)
                break

    if best is None:
        raise _article_not_found(ref, article, target_law,
                                 also_in=[m.get("law_name") or "" for m in metas])

    doc, meta = best
    # 긴 조문은 색인 시 parent가 2,000자에서 잘려 저장됨 — 자식 청크(항 단위)를
    # law_ref 순서로 조립해 조문 전문을 복원한다. (2026-07-29 Codex 적대 테스트 발견)
    if meta.get("chunk_level") == "parent":
        try:
            kids = col.get(
                where={"parent_ref": meta.get("law_ref", "")},
                include=["documents", "metadatas"],
            )
            def _order(km: dict) -> tuple:
                kr = km.get("law_ref", "")
                hang = re.search(r"제(\d+)항", kr)
                cont = re.search(r"\(계속(\d+)\)", kr)
                return (int(hang.group(1)) if hang else 999, int(cont.group(1)) if cont else 0)
            pairs = sorted(zip(kids.get("documents") or [], kids.get("metadatas") or []),
                           key=lambda p: _order(p[1] or {}))
            if pairs:
                header = f"{meta.get('law_name','')} {article}"
                parts = []
                for kdoc, km in pairs:
                    body = re.sub(r"^" + re.escape((km or {}).get("law_ref", "")) + r"\s*", "", kdoc or "")
                    parts.append(body.strip())
                doc = header + "\n" + "\n".join(parts)
        except Exception:
            pass  # 조립 실패 시 parent 축약본이라도 반환
    cleaned = _clean_markers(doc)
    return LawArticleResponse(
        law_name=meta.get("law_name", ""),
        article=article,
        content=cleaned,
        law_ref=meta.get("law_ref", ""),
        notes=detect_crossref_anomalies(cleaned),
    )


# ── 시점(as-of) 조문 조회 ────────────────────────────────────────────────────
# 코퍼스는 현행 스냅샷만 갖는다. 과거 계약·감사·분쟁은 **그 당시 시행 조문**이
# 기준이므로 law.go.kr 연혁(eflaw)에서 해당 시점 판을 골라 라이브로 가져온다.
_ASOF_MAX_PAGES = 3


@lru_cache(maxsize=256)
def _versions_cached(official: str) -> tuple:
    """법령 연혁 목록 — eflaw 검색은 느려 프로세스 캐시를 둔다.

    캐시가 낡아도 위험이 낮다: 과거 판은 불변이고, 새 개정이 누락되면 '더 오래된
    판'을 고를 뿐 없는 조문을 지어내지 않는다. 워커 재기동 시 자연 갱신.
    """
    out: list[LawVersion] = []
    seen: set[tuple[str, str]] = set()
    for page in range(1, _ASOF_MAX_PAGES + 1):
        xml = _drf_get("lawSearch.do", {
            "OC": _law_oc(), "target": "eflaw", "type": "XML",
            "query": official, "display": "100", "page": str(page),
        })
        page_versions = parse_versions(xml, official)
        fresh = [v for v in page_versions if (v.ef_date, v.mst) not in seen]
        for v in fresh:
            seen.add((v.ef_date, v.mst))
            out.append(v)
        # 정확 일치는 상위 페이지에 몰린다(실측: 29건 전부 1페이지) — 빈 페이지가
        # 나오면 더 뒤질 이유가 없다.
        if not fresh or len(re.findall(r"<law ", xml)) < 100:
            break
    return tuple(sorted(out, key=lambda v: v.ef_date))


class LawArticleAsOfResponse(BaseModel):
    law_name: str
    article: str
    content: str
    as_of: str
    effective_date: str          # 이 시점에 시행 중이던 판의 시행일자
    revision: str                # 제개정구분명
    promulgation_no: str
    is_current: bool             # 그 판이 지금도 현행인가
    prev_effective_date: str | None = None
    next_effective_date: str | None = None
    total_versions: int = 0
    notes: list[dict] = []


@router.get("/article-asof", response_model=LawArticleAsOfResponse)
def get_article_asof(
    request: Request,
    ref: str = Query(..., min_length=2, max_length=100),
    date: str = Query(..., pattern=r"^\d{4}-?\d{2}-?\d{2}$",
                      description="기준일 YYYY-MM-DD 또는 YYYYMMDD"),
):
    """특정 시점에 **시행 중이던** 조문 원문 조회 (law.go.kr 연혁 라이브).

    예: 2023년 체결 계약의 적법성 검토 → ref='국가계약법 제27조', date='2023-06-01'
    """
    from backend.services.rate_limiter import get_rate_limiter, LIMITS_LLM
    limiter = get_rate_limiter()
    limiter.record(limiter.check(request, LIMITS_LLM))

    as_of = date.replace("-", "")
    # 정규식은 자릿수만 본다 — 2026-01-32·2024-02-30 같은 달력상 없는 날짜가 통과하면
    # pick_asof의 문자열 비교가 엉뚱한 시행판을 조용히 고른다(행위시법 오적용).
    try:
        datetime.strptime(as_of, "%Y%m%d")
    except ValueError:
        raise HTTPException(422, f"'{date}'는 달력에 없는 날짜입니다 (YYYY-MM-DD 형식의 실재 날짜로 지정하십시오)")

    article_match = re.search(r"제\d+조(?:의\d+)?", ref)
    if not article_match:
        raise HTTPException(404, "조문번호(제N조)를 찾을 수 없습니다")
    article = article_match.group(0)

    law_part = ref[: article_match.start()].strip().rstrip("ㆍ·,")
    if not law_part:
        raise HTTPException(404, "법령명을 식별할 수 없습니다 (예: '국가계약법 제27조')")
    law_part = _LAW_ALIASES.get(law_part, law_part)
    from backend.config import BASE_DIR
    official = resolve_official_name(law_part, BASE_DIR / "tools" / "laws")

    versions = list(_versions_cached(official))
    if not versions:
        raise HTTPException(404, f"'{official}' 법령 연혁을 law.go.kr에서 찾지 못했습니다 "
                                 f"(정식 법령명으로 다시 시도해 보십시오)")

    chosen = pick_asof(versions, as_of)
    if chosen is None:
        raise HTTPException(
            404,
            f"{as_of[:4]}-{as_of[4:6]}-{as_of[6:]} 기준으로 시행 중이던 판이 없습니다 "
            f"(최초 시행일 {versions[0].ef_date})",
        )

    xml = _drf_get("lawService.do", {
        "OC": _law_oc(), "target": "law", "type": "XML", "MST": chosen.mst,
    })
    content = extract_article(xml, article)
    if not content:
        raise HTTPException(
            404,
            f"시행 {chosen.ef_date} 판에는 {article}이 없습니다 "
            f"(해당 시점에 신설 전이거나 삭제된 조문일 수 있습니다)",
        )

    prev_d, next_d = neighbors(versions, chosen)
    cleaned = _clean_markers(content)
    return LawArticleAsOfResponse(
        law_name=chosen.name,
        article=article,
        content=cleaned,
        as_of=as_of,
        effective_date=chosen.ef_date,
        revision=chosen.revision,
        promulgation_no=chosen.promul_no,
        is_current=chosen.is_current,
        prev_effective_date=prev_d,
        next_effective_date=next_d,
        total_versions=len(versions),
        notes=detect_crossref_anomalies(cleaned),
    )


@router.get("/references")
def search_references(
    request: Request,
    q: str = Query(..., min_length=2, max_length=200),
    top_k: int = Query(6, ge=1, le=12),
    rag=Depends(get_rag_service),
) -> list[dict]:
    """전 코퍼스 통합 검색(법령+계약예규+조달청·행안부 세부기준+실무가이드) — LLM 미사용.

    MCP·에이전트용(2026-07-30): /ask는 검색 후 OpenAI 생성까지 수행해 일일 캡을
    차감하지만, 에이전트 클라이언트는 자신이 LLM이므로 검색 청크 원문만 있으면 된다.
    /law/search는 법령(law_articles) 전용이라 예규·세부기준 코퍼스(admin_rules 등)에
    닿는 무LLM 경로가 없던 공백을 메운다. 임베딩(Gemini)만 사용 — OpenAI 캡 미차감,
    IP 슬라이딩 윈도우 한도는 동일 적용.
    """
    from backend.services.rate_limiter import get_rate_limiter, LIMITS_LLM
    limiter = get_rate_limiter()
    # check만 하면 카운트가 안 쌓여 한도가 무력화된다(2026-07-30 실측) — 무LLM이지만
    # 임베딩 비용·외부 API 보호를 위해 요청 자체를 계상한다.
    limiter.record(limiter.check(request, LIMITS_LLM))
    # 2026-07-30 R9: MCP 검색 경로에 rerank 미배선이던 결함 수리.
    # /ask(웹 챗봇)는 _cohere_rerank를 거치지만 이 경로는 RRF 순서를 그대로 냈고,
    # BM25-only 후보는 relevance_score가 0.6 하드코딩(rag_service.py:889)이라
    # 순위 신호가 사실상 없었다 — "감사 지적"이 "지적(地籍) 정리"에 매칭돼
    # 국유재산법 시행규칙 제47조가 1위로 나오던 동음이의 오매칭이 대표 증상.
    # 후보를 넓게 뽑아 rerank로 좁힌다. rerank 무산출이면 순서는 기존과 동일하되
    # 그 사실을 응답에 드러낸다(폴백 은폐 금지 — 이 저장소의 기존 원칙).
    from backend.services import reranker
    _want = top_k * 2
    # rerank가 있으면 후보 풀을 넓혀도 비용은 호출 1회로 같다 — 좁은 풀에서는 정답 청크가
    # 애초에 회수되지 않아 재정렬로도 못 살린다(예: '분할발주 금지'의 시행령 제68조).
    _pool = 28 if reranker.is_available() else max(top_k, 12)
    chunks = rag.search_all(q.strip(), top_k=_pool)
    _reranked = False
    if chunks and reranker.is_available():
        ordered = reranker.rerank(q.strip(), chunks, top_n=_want)
        if any(c.get("_rerank_score") is not None for c in ordered):
            chunks, _reranked = ordered, True

    # excerpt를 청크 앞 600자 고정이 아니라 질의 토큰 첫 매치 주변으로 창을 잡는다 —
    # 별표류 긴 청크(1,200자)는 정답 행이 뒤쪽에 있으면 회수돼도 본문이 안 보였다
    # (2026-07-30 배터리 업체-062: 부정당 별표2 '계약 미체결' 행).
    q_tokens = [t for t in q.split() if len(t) >= 2]

    def _excerpt(content: str) -> str:
        if len(content) <= 600 or not q_tokens:
            return content[:600]
        # 300자 보폭으로 600자 창을 밀며 질의 토큰 매치 수가 최대인 창을 고른다
        # (동률이면 앞쪽 창 — 기존 head-600과 호환). 토큰 가중치는 길이(특이도 근사).
        best_start, best_score = 0, -1
        for start in range(0, len(content) - 300, 300):
            win = content[start:start + 600]
            score = sum(len(t) for t in q_tokens if t in win)
            if score > best_score:
                best_start, best_score = start, score
        return ("..." if best_start else "") + content[best_start:best_start + 600]

    def _row(c: dict) -> dict:
        content = c.get("content") or ""
        section = c.get("section_title") or ""
        # 별표(제재기준·심사기준 표) 청크는 답이 특정 행 하나에 있어 어떤 절단도
        # 손실이다 — 청크 자체가 1,200자 캡(fetch_law_tables)이므로 전문 반환.
        excerpt = content[:1400] if "별표" in section else _excerpt(content)
        return {
            "source": c.get("document_id") or "",
            "section": section,
            "source_type": c.get("source_type") or "",
            "excerpt": _clean_markers(excerpt),
            # rerank 점수가 있으면 그것이 진짜 관련도 — 없으면 RRF 순서만 신뢰 가능하므로
            # 하드코딩 0.6을 관련도인 양 내보내지 않고 ranked_by로 근거를 밝힌다.
            "relevance": (round(float(c["_rerank_score"]), 3) if c.get("_rerank_score") is not None
                          else round(float(c.get("relevance_score") or 0), 3)),
            "ranked_by": "rerank" if c.get("_rerank_score") is not None else "hybrid_rrf",
        }

    return [_row(c) for c in chunks[:_want]]


@router.get("/search", response_model=list[LawSearchHit], response_model_exclude_none=True)
def search_law(q: str = Query(..., min_length=1, max_length=200)) -> list[LawSearchHit]:
    """법령 키워드 또는 조문번호로 조문 검색.

    - "제26조" → 모든 법령의 제26조 반환
    - "수의계약" → 본문에 '수의계약' 포함된 조문
    - "시행령 제26조" → 정확 매치 우선 + 키워드 매치
    """
    col = _get_collection()
    q = q.strip()
    if not q:
        return []

    article_match = re.search(r"제\d+조(?:의\d+)?", q)
    keyword = re.sub(r"제\d+조(?:의\d+)?", "", q).strip()
    # 상세 인용("…제26조제1항제5호가목")의 항·호·목 꼬리는 필터에서 제외 —
    # 조문은 조 단위로 저장되므로 남겨두면 매치가 전부 걸러져 0건이 된다.
    keyword = re.sub(r"제\s*\d+\s*[항호목](?:의\d+)?", "", keyword).strip()

    seen_refs: set[str] = set()
    results: list[LawSearchHit] = []

    # 1. 조문번호 정확 매치 우선
    if article_match:
        article = article_match.group(0)
        r = col.get(where={"article_titles": article}, include=["documents", "metadatas"])
        for doc, meta in zip(r.get("documents") or [], r.get("metadatas") or []):
            law_name = meta.get("law_name") or ""
            law_ref = meta.get("law_ref") or ""
            # 키워드가 있다면 law_name 또는 본문에 포함되어야 함 (공백 변형 허용)
            if keyword and not any(v in law_name or v in doc for v in _keyword_variants(keyword)):
                continue
            if law_ref in seen_refs:
                continue
            seen_refs.add(law_ref)
            results.append(LawSearchHit(
                law_name=law_name,
                article=article,
                content=_clean_markers(doc),
                snippet=_clean_markers(_make_snippet(doc, keyword or article)),
                law_ref=law_ref,
                note=_deleted_note(doc),
            ))

    # 2. 키워드 본문 substring 검색 (조문번호 없거나 추가 결과)
    # 원문 그대로 → 공백 접합("지명 경쟁"→"지명경쟁") → 약어 확장("종심제"→
    # "종합심사낙찰제") 순서로 시도, 결과가 나오는 첫 변형에서 멈춘다.
    if keyword:
        for variant in _keyword_variants(keyword):
            r = col.get(
                where_document={"$contains": variant},
                include=["documents", "metadatas"],
                limit=50,
            )
            found_any = False
            for doc, meta in zip(r.get("documents") or [], r.get("metadatas") or []):
                found_any = True
                law_ref = meta.get("law_ref") or ""
                if law_ref in seen_refs:
                    continue
                seen_refs.add(law_ref)
                results.append(LawSearchHit(
                    law_name=meta.get("law_name") or "",
                    article=meta.get("article_titles") or "",
                    content=_clean_markers(doc),
                    snippet=_clean_markers(_make_snippet(doc, variant)),
                    law_ref=law_ref,
                    note=_deleted_note(doc),
                ))
                if len(results) >= 30:
                    break
            if found_any:
                break

    # 3. 다단어 부분 매치 폴백 — 위 변형(전체 문구 substring)이 전부 0건일 때,
    #    토큰별 substring 검색 후 매치 토큰 수로 순위. AND-전체는 토큰 하나만 코퍼스에
    #    없어도("낙찰하한율"류 예규 용어) 0건이 되므로, 2개 이상 매치를 통과선으로 한다.
    tokens = _keyword_tokens(keyword) if keyword else []
    if not results and len(tokens) >= 2:
        by_ref: dict[str, tuple[int, str, dict]] = {}  # ref → (매치수, doc, meta)
        for t in tokens[:6]:
            r = col.get(
                where_document={"$contains": t},
                include=["documents", "metadatas"],
                limit=100,
            )
            for doc, meta in zip(r.get("documents") or [], r.get("metadatas") or []):
                ref = meta.get("law_ref") or ""
                cnt = by_ref[ref][0] + 1 if ref in by_ref else 1
                by_ref[ref] = (cnt, doc, meta)
        ranked = sorted(
            ((cnt, doc, meta) for cnt, doc, meta in by_ref.values() if cnt >= 2),
            key=lambda x: -x[0],
        )
        for cnt, doc, meta in ranked:
            law_ref = meta.get("law_ref") or ""
            if law_ref in seen_refs:
                continue
            seen_refs.add(law_ref)
            hit_token = next((t for t in tokens if t in doc), tokens[0])
            results.append(LawSearchHit(
                law_name=meta.get("law_name") or "",
                article=meta.get("article_titles") or "",
                content=_clean_markers(doc),
                snippet=_clean_markers(_make_snippet(doc, hit_token)),
                law_ref=law_ref,
                note=_deleted_note(doc),
            ))
            if len(results) >= 30:
                break

    # 4. 시맨틱 폴백 — substring이 전혀 안 걸리는 자연어 질의 구제.
    #    임베딩 호출 실패(쿼터 등)는 조용히 빈 결과 유지(검색 기능 자체는 죽이지 않음).
    #    T-2026W32-184: 무의미 질의('존재하지않는법률용어_9f7c2a')에 최근접 8건이
    #    전부 '삭제 <날짜>' 스텁으로 채워져 근거처럼 반환되던 결함 —
    #    (a) 삭제 스텁은 후보에서 제외(짧은 스텁이 임베딩 허브가 돼 상위 독식,
    #        구어체 질의 top-20의 17~19건이 스텁이었던 실측),
    #    (b) 거리 하한 미달이면 0건으로 실토. 임계 0.90은 2026-08-09 실측 근거:
    #        관련 질의 0.70~0.81 / 역외('블록체인 가스비') 0.92+ / 무의미 1.01+.
    if not results and keyword:
        try:
            qr = col.query(
                query_texts=[keyword], n_results=20,
                include=["documents", "metadatas", "distances"],
            )
            for doc, meta, dist in zip((qr.get("documents") or [[]])[0],
                                       (qr.get("metadatas") or [[]])[0],
                                       (qr.get("distances") or [[]])[0]):
                if dist is not None and dist > _SEMANTIC_MAX_DIST:
                    break  # 거리 오름차순 — 이후는 전부 하한 미달
                if _deleted_note(doc):
                    continue  # 삭제 스텁은 시맨틱 근거가 될 수 없다
                law_ref = meta.get("law_ref") or ""
                if law_ref in seen_refs:
                    continue
                seen_refs.add(law_ref)
                results.append(LawSearchHit(
                    law_name=meta.get("law_name") or "",
                    article=meta.get("article_titles") or "",
                    content=_clean_markers(doc),
                    snippet=_clean_markers(_make_snippet(doc, keyword)),
                    law_ref=law_ref,
                    matched_by="semantic",
                ))
                if len(results) >= 8:
                    break
        except Exception:  # noqa: BLE001 — 임베딩 장애 시 키워드 결과만으로 동작
            pass

    return results


# ── 판례·법령해석례 라이브 프록시 (law.go.kr DRF, 2026-07-30) ────────────────
# 코퍼스 인덱싱 대신 실시간 조회 — 항상 현행, 저장·재색인 부담 0. LLM 미사용.
# MCP search_cases/get_case 도구가 사용한다. 외부 API 장애는 502로 정직하게 전달.
_LAW_DRF = "http://www.law.go.kr/DRF"


def _law_oc() -> str:
    # run.sh는 .env를 export하지 않는다 — 키는 pydantic Settings(.env 로드)에서 읽는다.
    oc = (get_settings().law_api_key or "").strip()
    if not oc:
        raise HTTPException(503, "LAW_API_KEY 미설정 — 판례 조회 비활성")
    return oc


def _drf_get(path: str, params: dict) -> str:
    import httpx
    try:
        with httpx.Client(timeout=httpx.Timeout(15.0, connect=5.0)) as c:
            r = c.get(f"{_LAW_DRF}/{path}", params=params)
            r.raise_for_status()
            return r.text
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"law.go.kr 조회 실패: {type(exc).__name__}")


def _cdata(tag: str, block: str) -> str:
    m = re.search(rf"<{tag}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>", block, re.S)
    return (m.group(1).strip() if m else "").replace("<br/>", " ")


@router.get("/cases")
def search_cases(
    request: Request,
    q: str = Query(..., min_length=2, max_length=100),
    top_k: int = Query(5, ge=1, le=10),
    kind: str = Query("all", pattern="^(prec|expc|all)$"),
) -> list[dict]:
    """판례(prec)·법령해석례(expc) 검색 — 사건명·기관·일자·일련번호 목록.

    본문은 /law/case?kind=&case_id= 로 이어서 조회. 검색어는 사건명 기준이므로
    '부정당업자 제한', '유찰 수의계약'처럼 핵심 명사 위주가 잘 잡힌다.
    """
    from backend.services.rate_limiter import get_rate_limiter, LIMITS_LLM
    limiter = get_rate_limiter()
    limiter.record(limiter.check(request, LIMITS_LLM))  # check만으론 카운트 미적립 — 계상 필수
    oc = _law_oc()
    out: list[dict] = []
    kinds = ["prec", "expc"] if kind == "all" else [kind]
    for k in kinds:
        xml = _drf_get("lawSearch.do", {"OC": oc, "target": k, "type": "XML",
                                        "display": top_k, "query": q})
        for block in re.findall(rf"<{k} id=.*?</{k}>", xml, re.S):
            if k == "prec":
                out.append({
                    "kind": "prec",
                    "case_id": _cdata("판례일련번호", block),
                    "title": _cdata("사건명", block),
                    "org": _cdata("법원명", block),
                    "case_no": _cdata("사건번호", block),
                    "date": _cdata("선고일자", block),
                })
            else:
                out.append({
                    "kind": "expc",
                    "case_id": _cdata("법령해석례일련번호", block),
                    "title": _cdata("안건명", block),
                    # 검색 응답은 회신기관/회신일자, 본문 응답은 해석기관/해석일자 — 명칭이 다르다
                    "org": _cdata("회신기관명", block) or _cdata("해석기관명", block),
                    "case_no": _cdata("안건번호", block),
                    "date": _cdata("회신일자", block) or _cdata("해석일자", block),
                })
    return out


@router.get("/case")
def get_case(
    request: Request,
    kind: str = Query(..., pattern="^(prec|expc)$"),
    case_id: str = Query(..., min_length=1, max_length=20),
) -> dict:
    """판례/해석례 본문 — 판시사항·판결요지·참조조문(판례), 질의요지·회답·이유(해석례)."""
    from backend.services.rate_limiter import get_rate_limiter, LIMITS_LLM
    limiter = get_rate_limiter()
    limiter.record(limiter.check(request, LIMITS_LLM))  # check만으론 카운트 미적립 — 계상 필수
    xml = _drf_get("lawService.do", {"OC": _law_oc(), "target": kind,
                                     "ID": case_id, "type": "XML"})
    def _f(tag: str, limit: int = 2500) -> str:
        v = _cdata(tag, xml)
        return v[:limit] + ("…(생략)" if len(v) > limit else "")
    # 2026-07-30 R9 (배터리 report_issue 제보): 검색(lawSearch)에는 뜨지만 본문
    # API가 "일치하는 판례가 없습니다"를 주는 판례(하급심 등 본문 미제공)를
    # 전 필드 빈 문자열로 조용히 넘기던 결함 — 구조화 오류+행동 지침으로 대체.
    if "일치하는" in xml or not (_cdata("사건명", xml) or _cdata("안건명", xml)):
        return {"error": "case_body_unavailable", "kind": kind, "case_id": case_id,
                "hint": "이 판례·해석례는 law.go.kr에 본문이 제공되지 않습니다"
                        "(하급심·타기관 제공 등). 검색 결과의 사건명·사건번호를 그대로"
                        " 인용하되 본문 근거가 필요하면 다른 판례를 조회하세요."}
    if kind == "prec":
        return {"kind": "prec", "case_id": case_id,
                "title": _f("사건명"), "org": _f("법원명"), "case_no": _f("사건번호"),
                "date": _f("선고일자"), "issue": _f("판시사항"),
                "summary": _f("판결요지"), "referenced_laws": _f("참조조문", 800)}
    return {"kind": "expc", "case_id": case_id,
            "title": _f("안건명"), "org": _f("해석기관명"), "case_no": _f("안건번호"),
            "date": _f("해석일자"), "question": _f("질의요지"),
            "answer": _f("회답"), "reasoning": _f("이유", 4000)}
