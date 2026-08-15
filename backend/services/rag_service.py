"""ChromaDB 하이브리드 검색 — 규칙 엔진 결과에 법령 원문 근거 추가.

공개 코퍼스 3원 체제: law_articles(법령 조문, law.go.kr) + admin_rules(계약예규 등
행정규칙) + public_guides(감사원 공공계약 실무가이드 등 공개 간행물). faq는
public_guides에서 파생한 Q&A 청크(선택 — 없으면 자동 생략).
"""
import re
import chromadb
from chromadb import Collection
from backend.config import get_settings
from backend.services.embedding import GeminiEmbeddingFunction

_settings = get_settings()

# 계약유형과 무관하게 공개 가이드 단일 컬렉션을 검색한다(가이드는 유형 횡단 자료).
GUIDES_COLLECTION = _settings.collection_public_guides
COLLECTION_MAP = {
    "service": GUIDES_COLLECTION,
    "product": GUIDES_COLLECTION,
    "public_procurement": GUIDES_COLLECTION,
    "construction": GUIDES_COLLECTION,
}

LAW_ARTICLES_COLLECTION = _settings.collection_law_articles
ADMIN_RULES_COLLECTION = _settings.collection_admin_rules
FAQ_COLLECTION = _settings.collection_faq
DOC2QUERY_COLLECTION = _settings.collection_doc2query

# 사용자 질문에서 토픽 추출용 키워드 사전 (tools/tag_topics.py와 동기 유지)
_TOPIC_KEYWORDS: dict[str, list[str]] = {
    "수의계약": ["수의계약", "수의로"],
    "소액수의": ["소액수의"],
    "일반경쟁": ["일반경쟁"],
    "제한경쟁": ["제한경쟁"],
    "지명경쟁": ["지명경쟁"],
    "협상계약": ["협상계약", "협상에 의한"],
    "적격심사": ["적격심사"],
    "종합심사": ["종합심사"],
    "PQ": ["PQ", "사전심사", "사업수행능력"],
    "낙찰자결정": ["낙찰자", "낙찰"],
    "예정가격": ["예정가격"],
    "물가변동": ["물가변동", "물가조정"],
    "설계변경": ["설계변경", "신규비목", "협의단가"],
    "공기연장": ["공기연장", "공기 연장"],
    "변경계약": ["변경계약", "계약변경"],
    "계약보증금": ["계약보증금"],
    "입찰보증금": ["입찰보증금"],
    "이행보증금": ["이행보증금"],
    "하자보수": ["하자보수", "하자담보"],
    "지체상금": ["지체상금"],
    "선금": ["선금"],
    "단가계약": ["단가계약"],
    "장기계속계약": ["장기계속"],
    "공동수급체": ["공동수급체", "공동이행", "분담이행"],
    "중기간경쟁": ["중소기업자간", "중기간"],
    "공사용자재직접구매": ["공사용자재", "직접구매"],
    "계약이행능력심사": ["계약이행능력심사", "이행능력심사"],
    "검사": ["준공검사", "납품검사", "검사 기간"],
    "부정당업자": ["부정당업자"],
    "특정조달": ["특정조달", "정부조달협정"],
    "입찰공고": ["입찰공고"],
    "재공고": ["재공고"],
    "유찰": ["유찰"],
}


def _load_abbreviations() -> dict[str, str]:
    """glossary.json의 aliases를 {약어: 정식어}로 로드 — 약어 검색 확장용 (단일 소스)."""
    try:
        import json as _json
        from pathlib import Path as _Path
        gp = _Path(__file__).resolve().parents[2] / "data" / "glossary.json"
        data = _json.loads(gp.read_text(encoding="utf-8"))
        m: dict[str, str] = {}
        for e in data:
            term = e.get("term", "")
            for a in (e.get("aliases") or []):
                if a and a != term:
                    m[a] = term
        return m
    except Exception:
        return {}


_ABBREVIATIONS = _load_abbreviations()


def expand_query_abbreviations(query: str) -> str:
    """질의에 약어가 있으면 정식어를 덧붙여 임베딩·키워드 매칭 보강.

    예: '예가 산정 방법' → '예가 산정 방법 예정가격'. 원문 보존(append만).
    """
    if not query:
        return query
    extra = [full for ab, full in _ABBREVIATIONS.items() if ab in query and full not in query]
    return (query + " " + " ".join(extra)) if extra else query


def _extract_query_topics(query: str) -> set[str]:
    """사용자 질문에서 매칭되는 토픽 ID 집합 반환."""
    return {tid for tid, patterns in _TOPIC_KEYWORDS.items() if any(p in query for p in patterns)}


# BM25 토큰화 (tools/build_bm25_index.py와 동기)
_BM25_TOKEN_RE = re.compile(r"[가-힣]{2,}|[A-Za-z]{2,}|\d+")
_BM25_STOPWORDS = {
    "이다", "하다", "있다", "없다", "되다", "그것", "이것", "저것",
    "그리고", "하지만", "그러나", "또한", "또는", "따라", "위해",
    "대한", "관한", "에서", "에게", "으로", "라고", "이라", "한다",
    "있는", "없는", "되는", "하는", "같은", "다른", "모든", "각각",
}


def _bm25_tokenize(text: str) -> list[str]:
    tokens = _BM25_TOKEN_RE.findall(text or "")
    return [t for t in tokens if t not in _BM25_STOPWORDS and len(t) >= 2]

_GUIDE_PATTERNS = ["건설엔지니어", "sw_guide", "engineering_guide", "감사원", "가이드", "판례"]


def _classify_source(document_id: str) -> str:
    """document_id 패턴으로 소스 유형 분류 — 공개 코퍼스는 전부 guide 계열."""
    return "guide"



# ── 추출 품질 게이트 (T-2026W32-161, 2026-08-14) ────────────────────────────
# 코퍼스 전수 스캔으로 특정한 손상 문서 2종. 법령(law_articles 8,953청크)·예규
# (admin_rules 1,345청크)는 오염 0건이라 핵심 근거는 온전하고, 손상은 PDF 가이드류다.
#
#  ① service_sw_guide_2025 — public_guides 56청크 **전부** 폰트 인코딩 깨짐(cmap 미적용):
#     "ᛜᛜ6:⪓⾬ㄿ ᛧ⎓oᗷᶬ…" 처럼 글자 자체가 판독 불가다. 경고를 달 대상이 아니라
#     **뺄** 대상 — 읽을 수 없는 텍스트를 '근거'로 내놓는 것은 인용이 아니라 소음이다.
#  ② general_감사원공공계 — 842청크 중 621건에 BEL(0x07) 등 제어문자 + 2단 편집 원문의
#     행교차(다른 단 문장이 문단 중간에 끼어든다). 실무 선례가 실린 유일한 축이라
#     빼면 손실이 크다 → **세정 + 품질 경고 부착**으로 남긴다.
#
# 되돌리기: _EXCLUDED_SOURCES를 비우면 즉시 원복(재색인 불필요).
# 2026-08-14 결정(T-2026W33-174, 사장님 확정): SW 가이드는 **영구 제외**한다. 청크는
# 코퍼스에서 삭제했고(public_guides 56건), 색인 스크립트도 fail-closed로 막았다
# (tools/index_sw_guide_2025.py). 이 표는 그 위의 **3중 방어** — 어떤 경로로든 다시
# 들어오면 서빙에서 걸러진다. 되돌리려면 이 항목을 지우고 색인 게이트를 열면 된다.
_EXCLUDED_SOURCES = {
    "service_sw_guide_2025": "배포본 PDF 폰트 cmap 손상으로 본문 판독 불가(83쪽 한글 12자) — "
                             "영구 제외 결정(2026-08-14). SW 질의는 소프트웨어 진흥법 조문으로 커버",
}
# 2026-08-14 재추출 완료(T-2026W33-173): general_감사원공공계는 열 인식 정렬로 다시 색인해
# 행교차·제어문자가 사라졌다(public_guides 818청크·faq 190청크 모두 제어문자 0건, 실측) —
# 그래서 이 표에서 뺐다. **경고를 지운 게 아니라 경고할 대상이 없어진 것**이다.
# 새 손상 문서가 생기면 여기 한 줄을 넣으면 그 즉시 다시 공시된다.
_DEGRADED_SOURCES: dict[str, str] = {}
_CTRL_RE = re.compile("[\u0000-\u0008\u000b\u000c\u000e-\u001f\u200b-\u200f\u202a-\u202e\ufeff]")


def _match_source(did: str, table: dict) -> str | None:
    """document_id ↔ 원본 문서 대응. 같은 PDF가 컬렉션마다 접두어를 달고 들어온다
    (`general_감사원공공계` / `faq_general_감사원공공계`) — 접두어 때문에 경고가
    한쪽에만 붙던 것을 부분일치로 맞춘다."""
    for key in table:
        if key in did:
            return key
    return None


def _quality_gate(chunks: list[dict]) -> list[dict]:
    """판독 불가 문서는 빼고, 손상 문서는 세정 후 품질 경고를 붙인다.

    서빙 시점에 거는 이유: 재색인은 임베딩 비용·시간이 들고, 게이트를 코드에 두면
    다음 추출 개선이 들어왔을 때 목록만 지우면 원복된다(회귀도 그 형태로 박는다).

    **뺀 것은 뺐다고 말한다**: 검색에 걸렸는데 품질 때문에 버린 청크 수를 첫 청크에
    `_gate_dropped`로 실어 보내 API가 공시하게 한다(못 봄 ≠ 없음 — 우리가 가진 것을
    판독 못 해 뺐다는 사실과, 원래 자료가 없다는 것은 다르다).
    """
    out: list[dict] = []
    dropped: dict[str, int] = {}
    for c in chunks:
        did = c.get("document_id") or ""
        excluded = _match_source(did, _EXCLUDED_SOURCES)
        if excluded:
            dropped[excluded] = dropped.get(excluded, 0) + 1
            continue
        if _CTRL_RE.search(c.get("content") or "") or _CTRL_RE.search(c.get("section_title") or ""):
            c["content"] = _CTRL_RE.sub(" ", c.get("content") or "")
            c["section_title"] = _CTRL_RE.sub("", c.get("section_title") or "").strip()
            c["extraction_quality"] = "control_chars_cleaned"
        degraded = _match_source(did, _DEGRADED_SOURCES)
        if degraded:
            c["extraction_quality"] = "two_column_pdf"
            c["quality_warning"] = _DEGRADED_SOURCES[degraded]
        out.append(c)
    if dropped and out:
        out[0]["_gate_dropped"] = [{"source": k, "chunks": v, "reason": _EXCLUDED_SOURCES[k]}
                                   for k, v in dropped.items()]
    return out


class RAGService:
    def __init__(self, chroma_path: str):
        self._client = chromadb.PersistentClient(path=chroma_path)
        self._ef = GeminiEmbeddingFunction()
        # 키워드 부스팅 후보 사전 구축 캐시(정렬 2모드) — 컬렉션 count 변화 시 재구축.
        # workers=1 장수 프로세스에서 요청마다 law_articles 전량을 물질화하면 아레나
        # 단편화로 RSS가 계단식 성장한다(+1.0GB/7h 실측, 2026-08-15 메모리 수리).
        self._kw_boost_cache: dict = {}
        # 2026-05-20: BM25 하이브리드 검색 — Dense(임베딩) + BM25(키워드) RRF 결합
        self._bm25_data: dict | None = None
        try:
            import pickle
            from pathlib import Path
            idx_path = Path(chroma_path) / "bm25_index.pkl"
            if idx_path.exists():
                with open(idx_path, "rb") as f:
                    self._bm25_data = pickle.load(f)
        except Exception:
            self._bm25_data = None
        self._build_law_ref_index()

    def _get_collection(self, contract_type: str) -> Collection | None:
        name = COLLECTION_MAP.get(contract_type)
        if not name:
            return None
        try:
            kwargs = {"embedding_function": self._ef} if self._ef else {}
            return self._client.get_collection(name, **kwargs)
        except Exception:
            return None

    def search(self, query: str, contract_type: str, top_k: int = 5) -> list[dict]:
        """벡터 유사도 검색 + 키워드 부스팅. law_refs 메타데이터 포함."""
        query = expand_query_abbreviations(query)  # 약어→정식어 확장 (예가→예정가격 등)
        collection = self._get_collection(contract_type)
        if collection is None:
            return []

        results = collection.query(
            query_texts=[query],
            n_results=min(top_k * 2, collection.count() or 1),
            include=["documents", "metadatas", "distances"],
        )

        chunks = []
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        dists = results.get("distances", [[]])[0]
        ids = results.get("ids", [[]])[0]

        for i, (doc, meta, dist) in enumerate(zip(docs, metas, dists)):
            relevance = max(0.0, 1.0 - dist)
            boost = 0.1 if self._keyword_match(query, doc) else 0.0
            law_refs_raw = meta.get("law_refs", "")
            law_refs = [r.strip() for r in law_refs_raw.split(",") if r.strip()] if law_refs_raw else []
            chunks.append({
                "chunk_id": ids[i] if i < len(ids) else meta.get("chunk_id", ""),
                "document_id": meta.get("document_id", ""),
                "section_title": meta.get("section_title", ""),
                "chunk_type": meta.get("chunk_type", ""),
                "content": doc[:600],
                "relevance_score": round(min(1.0, relevance + boost), 3),
                "contract_type": meta.get("contract_type", ""),
                "law_refs": law_refs,
                "topics": meta.get("topics", ""),
            })

        chunks.sort(key=lambda x: x["relevance_score"], reverse=True)
        return chunks[:top_k]

    @staticmethod
    def _ref_lookup_variants(ref: str) -> list[str]:
        """법령 참조를 여러 형태로 변환하여 레지스트리 키 매칭 범위 확장."""
        variants = [ref]
        art_m = re.search(r"제\d+조(?:의\d+)?", ref)
        art_str = art_m.group(0) if art_m else ""

        # "XXX 시행령 제N조" → "시행령 제N조" (축약)
        m = re.search(r"(시행령\s*제\d+조(?:의\d+)?)", ref)
        if m and m.group(1) != ref:
            variants.append(m.group(1))

        # "시행령 제N조" → "국가계약법 시행령 제N조" (확장)
        if re.match(r"^시행령\s*제\d+조", ref) and art_str:
            variants.append(f"국가계약법 시행령 {art_str}")
            variants.append(f"국가계약법 시행규칙 {art_str}")

        # "국가계약법 제N조" → 그대로 (이미 정확)
        return list(dict.fromkeys(variants))  # 순서 유지 중복 제거

    def _get_law_articles(self, law_refs: list[str]) -> list[dict]:
        """law_refs 목록에 해당하는 법령 조문 청크를 law_articles 컬렉션에서 조회.

        Hierarchical 보강 (2026-05-31):
        - chunk_level='child'(항 단위) 매칭 시 parent_ref(조문 전체)도 함께 가져옴
        - LLM·사용자가 부분(항)만 보는 게 아니라 조문 전체 맥락 동시 참조
        """
        try:
            collection = self._client.get_collection(LAW_ARTICLES_COLLECTION, embedding_function=self._ef)
        except Exception:
            return []

        articles = []
        seen_refs: set[str] = set()
        tried: set[str] = set()
        parent_refs_to_fetch: set[str] = set()

        for ref in law_refs:
            if ref in seen_refs:
                continue
            seen_refs.add(ref)
            for variant in self._ref_lookup_variants(ref):
                if variant in tried:
                    continue
                tried.add(variant)
                try:
                    result = collection.get(
                        where={"law_ref": variant},
                        include=["documents", "metadatas"],
                    )
                    for doc, meta in zip(result.get("documents", []), result.get("metadatas", [])):
                        chunk_level = meta.get("chunk_level", "single")
                        parent_ref = meta.get("parent_ref", "")
                        safe_id = re.sub(r"[^a-zA-Z0-9가-힣]", "_", variant)
                        articles.append({
                            "chunk_id": f"law_{safe_id}",
                            "section_title": f"[법령 조문] {meta.get('law_ref', variant)}",
                            "chunk_type": "law_article",
                            "chunk_level": chunk_level,
                            "content": doc[:800],
                            "relevance_score": 1.0,
                            "contract_type": "law",
                            "law_ref": variant,
                        })
                        # Hierarchical: child 매칭 시 parent도 큐에 추가
                        if chunk_level == "child" and parent_ref and parent_ref not in seen_refs:
                            parent_refs_to_fetch.add(parent_ref)
                except Exception:
                    continue

        # 2단계: parent_ref들을 가져와 articles에 추가 (앞쪽에 삽입 — 맥락 우선)
        parent_articles = []
        for parent_ref in parent_refs_to_fetch:
            if parent_ref in seen_refs:
                continue
            seen_refs.add(parent_ref)
            try:
                result = collection.get(
                    where={"law_ref": parent_ref},
                    include=["documents", "metadatas"],
                )
                for doc, meta in zip(result.get("documents", []), result.get("metadatas", [])):
                    safe_id = re.sub(r"[^a-zA-Z0-9가-힣]", "_", parent_ref)
                    parent_articles.append({
                        "chunk_id": f"law_parent_{safe_id}",
                        "section_title": f"[법령 조문 — 조 전체] {meta.get('law_ref', parent_ref)}",
                        "chunk_type": "law_article",
                        "chunk_level": "parent",
                        "content": doc[:1500],  # parent는 더 길게 (조문 전체 맥락)
                        "relevance_score": 0.95,  # child보다 약간 낮은 점수
                        "contract_type": "law",
                        "law_ref": parent_ref,
                    })
            except Exception:
                continue

        # parent를 앞에, child를 뒤에 (LLM이 맥락 먼저 읽도록)
        return parent_articles + articles

    def search_with_references(
        self, query: str, contract_type: str, top_k: int = 5
    ) -> tuple[list[dict], list[dict]]:
        """벡터 검색 + 법령 조문 참조 확장.

        Returns:
            (content_chunks, law_chunks) — 내용 청크와 참조 법령 조문 청크
        """
        content_chunks = self.search(query, contract_type, top_k)

        # 검색된 청크들의 law_refs 수집 (순서 유지, 중복 제거)
        seen: dict[str, None] = {}
        for chunk in content_chunks:
            for ref in chunk.get("law_refs", []):
                seen[ref] = None

        law_chunks = self._get_law_articles(list(seen.keys())) if seen else []
        return content_chunks, law_chunks

    def _build_law_ref_index(self) -> None:
        """가이드 컬렉션을 순회하며 law_ref → 청크 역인덱스 구축 (초기화 1회)."""
        self._law_ref_index: dict[str, list[dict]] = {}
        for coll_name in {GUIDES_COLLECTION}:
            try:
                col = self._client.get_collection(coll_name, embedding_function=self._ef)
                items = col.get(include=["documents", "metadatas"])
            except Exception:
                continue
            for chunk_id, doc, meta in zip(
                items.get("ids", []),
                items.get("documents", []),
                items.get("metadatas", []),
            ):
                if not meta:
                    continue
                law_refs_raw = meta.get("law_refs", "")
                if not law_refs_raw:
                    continue
                law_refs = [r.strip() for r in law_refs_raw.split(",") if r.strip()]
                doc_id = meta.get("document_id", "")
                chunk_data = {
                    "chunk_id": chunk_id,
                    "collection": coll_name,
                    "document_id": doc_id,
                    "source_type": _classify_source(doc_id),
                    "section_title": meta.get("section_title", ""),
                    "content": doc[:600] if doc else "",
                    "contract_type": meta.get("contract_type", ""),
                    "law_refs": law_refs,
                    "relevance_score": 0.0,
                }
                for ref in law_refs:
                    self._law_ref_index.setdefault(ref, []).append(chunk_data)

    def search_knowledge_web(
        self, query: str, contract_type: str, top_k: int = 5
    ) -> dict[str, list[dict]]:
        """GraphRAG: 법령 허브 경유로 가이드·법령 카테고리별 청크 반환."""
        primary_chunks = self.search(query, contract_type, top_k)

        seen_refs: dict[str, None] = {}
        for chunk in primary_chunks:
            for ref in chunk.get("law_refs", []):
                seen_refs[ref] = None
        all_law_refs = list(seen_refs.keys())

        buckets: dict[str, list[dict]] = {"textbook": [], "guide": []}
        seen_ids: set[str] = set()

        for chunk in primary_chunks:
            stype = _classify_source(chunk.get("document_id", ""))
            chunk["source_type"] = stype
            cid = chunk["chunk_id"]
            if cid not in seen_ids:
                seen_ids.add(cid)
                buckets.setdefault(stype, []).append(chunk)

        for ref in all_law_refs:
            for cd in self._law_ref_index.get(ref, []):
                cid = cd["chunk_id"]
                if cid in seen_ids:
                    continue
                seen_ids.add(cid)
                stype = cd["source_type"]
                buckets.setdefault(stype, []).append(cd)

        law_chunks = self._get_law_articles(all_law_refs) if all_law_refs else []
        law_sources = []
        for lc in law_chunks:
            law_sources.append({
                "chunk_id": lc["chunk_id"],
                "document_id": (lc.get("law_name") or LAW_ARTICLES_COLLECTION),
                "section_title": lc["section_title"],
                "content": lc["content"],
                "relevance_score": lc["relevance_score"],
                "source_type": "law",
                "law_refs": [],
            })

        return {
            "textbook": buckets.get("textbook", []),
            "guide": buckets.get("guide", []),
            "law": law_sources,
        }

    def build_knowledge_web_context(
        self,
        knowledge_web: dict[str, list[dict]],
        max_chars: int = 2000,
    ) -> str:
        """카테고리별 청크를 구분된 LLM 컨텍스트 문자열로 변환."""
        LABELS = {"textbook": "📘 참고자료", "guide": "📋 실무가이드", "law": "⚖️ 법령"}
        parts: list[str] = []
        total = 0
        for stype in ("textbook", "guide", "law"):
            chunks = knowledge_web.get(stype, [])
            if not chunks:
                continue
            label = LABELS[stype]
            for c in chunks:
                excerpt = f"[{label} — {c.get('section_title', '')}]\n{c.get('content', '')}"
                if total + len(excerpt) > max_chars:
                    break
                parts.append(excerpt)
                total += len(excerpt)
            if total >= max_chars:
                break
        return "\n\n---\n\n".join(parts)

    @staticmethod
    def _extract_search_keywords(query: str) -> list[str]:
        """쿼리에서 한국어 핵심어 + 접미어 분리 변형 추출 (키워드 폴백 검색용)."""
        # 조사·어미 등 짧은 기능어 제외
        tokens = re.findall(r"[가-힣]{2,}", query)
        stop = {
            "어떻게", "어떤가", "무엇", "입니까", "인가요", "하나요", "됩니까", "있나요",
            "위해서", "위하여", "절차가", "절차는", "절차를", "방법은", "방법이",
            "있어요", "되나요", "이어야", "해야하", "최소", "최대", "기간은", "기간이",
            "일인지", "일인가", "경우는", "경우에", "때에는", "대하여", "관련해",
        }
        # 조사·어미 제거 (은/는/이/가/을/를/에/의/로/도/만/과/와/으로 등)
        _PARTICLES = re.compile(r"(은|는|이|가|을|를|에|의|로|도|만|과|와|으로|에서|에게|부터|까지|이란|이라|라면)$")
        stripped: list[str] = []
        for t in tokens:
            m = _PARTICLES.search(t)
            if m and len(t) - len(m.group()) >= 2:
                stripped.append(t[: -len(m.group())])
            else:
                stripped.append(t)
        tokens = stripped

        # 2글자 범용어(최소, 경우 등)는 제외하되, 도메인 핵심 2글자어(선금, 하자 등)는 허용
        _GENERIC_TWO = {"최소", "최대", "경우", "방법", "기준", "요건", "조건", "절차",
                        "관련", "규정", "기간", "이상", "이하", "이전", "이후", "적용",
                        "대상", "내용", "여부", "사항", "현재", "제출", "해당", "가능"}
        base = [t for t in tokens if t not in stop and (len(t) >= 3 or t not in _GENERIC_TWO)]

        # 복합어 접미어 제거 변형 추가 (예: 불용처리→불용, 지급절차→지급)
        _SUFFIXES = ("처리", "절차", "방법", "기준", "요건", "규정", "관련", "검토", "여부", "률", "율", "액", "비")
        expanded: list[str] = []
        seen: set[str] = set()
        for kw in base:
            for s in _SUFFIXES:
                if kw.endswith(s) and len(kw) > len(s) + 1:
                    short = kw[: -len(s)]
                    if len(short) >= 2 and short not in seen:
                        seen.add(short)
                        expanded.append(short)
            if kw not in seen:
                seen.add(kw)
                expanded.append(kw)
        return expanded[:6]

    def _keyword_fallback(
        self, query: str, keywords: list[str], top_k: int, seen: dict[str, float],
        clean_query: str | None = None,
    ) -> list[dict]:
        """키워드가 포함된 청크를 where_document 필터로 추가 검색."""
        # 4글자 미만 단어는 너무 범용적이라 노이즈 유발 → WHERE 필터에서 제외
        # 단, 도메인 핵심 2~3글자어(선금, 하자, 지체 등)는 허용
        _SHORT_DOMAIN = {"선금", "하자", "지체", "낙찰", "입찰", "수의", "공고", "보증", "불용", "협상"}
        # 계약문서 전체에 편재하는 범용어 → WHERE 필터로 쓰면 수십 개 무관 청크 유입
        _FILTER_BLOCKLIST = {"계약금액", "계약금", "계약서", "계약기간", "계약체결", "계약방법", "계약내용"}
        filter_kws = [k for k in keywords if (len(k) >= 4 or k in _SHORT_DOMAIN) and k not in _FILTER_BLOCKLIST]
        if not filter_kws:
            filter_kws = keywords[:1]  # 최소 1개 보장

        # 도메인 동의어·연관어 확장: 원문이 다른 표기를 쓰는 경우 커버
        _DOMAIN_SYNONYMS: dict[str, list[str]] = {
            "하자보수보증금": ["하자보수보증율", "하자보증금액", "하자보수보증금"],  # 원문: 보증율(용역)/하자보증금액(물품) vs 쿼리: 보증금
            "하자보수보증율": ["하자보수보증금", "하자보증금액", "하자보수보증율"],
            "입찰공고": ["공고기간", "공고시기"],  # 입찰공고 기간 관련 청크는 '공고기간' 포함
            "물가변동": ["조정요건", "품목조정율"],  # 물가변동 조정요건 청크는 '조정요건'·'품목조정율' 포함
            "협상": ["협상적격자", "협상순위", "협상에 의한"],  # 협상계약 세부 청크 포함
        }
        expanded_kws: list[str] = []
        seen_kw: set[str] = set(filter_kws)
        for kw in filter_kws:
            for syn in _DOMAIN_SYNONYMS.get(kw, []):
                if syn not in seen_kw:
                    seen_kw.add(syn)
                    expanded_kws.append(syn)
        filter_kws = filter_kws + expanded_kws

        extras: list[dict] = []
        for kw in filter_kws:
            for coll_name in COLLECTION_MAP.values():
                try:
                    col = self._client.get_collection(coll_name, embedding_function=self._ef)
                    cnt = col.count()
                    if cnt == 0:
                        continue
                    results = col.query(
                        query_texts=[query],
                        n_results=min(10, cnt),
                        where_document={"$contains": kw},
                        include=["documents", "metadatas", "distances"],
                    )
                    docs = results.get("documents", [[]])[0]
                    metas = results.get("metadatas", [[]])[0]
                    dists = results.get("distances", [[]])[0]
                    ids = results.get("ids", [[]])[0]
                    for i, (doc, meta, dist) in enumerate(zip(docs, metas, dists)):
                        cid = ids[i] if i < len(ids) else meta.get("chunk_id", "")
                        if cid in seen:
                            continue
                        seen[cid] = 0.0
                        # 벡터 유사도 + 0.25 부스트 (최대 0.80)
                        score = round(min(0.80, max(0.0, 1.0 - dist) + 0.25), 3)
                        doc_id = meta.get("document_id", "")
                        extras.append({
                            "chunk_id": cid,
                            "document_id": doc_id,
                            "section_title": meta.get("section_title", ""),
                            "chunk_type": meta.get("chunk_type", ""),
                            "content": doc[:600],
                            "relevance_score": score,
                            "contract_type": meta.get("contract_type", ""),
                            "source_type": _classify_source(doc_id),
                            "law_refs": [],
                        })
                except Exception:
                    continue
        return extras

    def _kw_boost_items(self, law_col, local: bool) -> list[dict]:
        """law_articles 키워드 부스팅 후보의 사전 구축 캐시(정렬 2모드).

        (2026-08-15 메모리 수리) 종전에는 요청마다 컬렉션 전량을 파이썬 객체로
        물질화(요청당 25~50MB)했고, workers=1 장수 프로세스라 그 임시 할당이 아레나
        단편화로 눌러앉아 RSS +1.0GB/7h 성장의 주범이었다. 필요한 필드만 압축해
        1회 상주(~20MB)로 바꾸고, 컬렉션 count가 변할 때만 재구축한다(재색인 대응).
        동시 첫 호출 시 드물게 이중 구축될 수 있으나 멱등이라 무해하다.

        정렬 규약(2026-07-29·07-30 교정 그대로): 실무 절차·한도의 실체인 시행령·
        시행규칙을 본법보다 앞세우고, 지자체 질문(local=True)은 지방계약법령을 최우선.
        """
        count = law_col.count()
        cache = self._kw_boost_cache
        if cache.get("count") != count:
            raw = law_col.get(include=["documents", "metadatas"])
            entries: list[dict] = []
            for cid, doc, meta in zip(raw["ids"], raw["documents"], raw["metadatas"]):
                if not isinstance(meta, dict):
                    continue
                doc = doc or ""
                ln = meta.get("law_name", "") or ""
                entries.append({
                    "cid": cid,
                    "head": (meta.get("law_ref", "") or "") + " " + doc[:400],
                    "content": doc[:1200],
                    "law_name": ln,
                    "law_ref": meta.get("law_ref", ""),
                    "chunk_level": meta.get("chunk_level", "single"),
                    "jibang": ln.startswith("지방자치단체"),
                })

            def _prio(e: dict, local_q: bool) -> int:
                ln = e["law_name"]
                if local_q:
                    if "지방계약법" in ln and "시행령" in ln: return -3
                    if "지방계약법" in ln and "시행규칙" in ln: return -2
                    if "지방계약법" in ln: return -1
                if "국가계약법" in ln and "시행령" in ln: return 0
                if "국가계약법" in ln and "시행규칙" in ln: return 1
                if "국가계약법" in ln: return 2
                if "공기업" in ln or "준정부기관" in ln: return 3
                if "중소기업제품" in ln or "건설기술" in ln: return 4
                return 5

            cache["national"] = sorted(entries, key=lambda e: _prio(e, False))
            cache["local"] = sorted(entries, key=lambda e: _prio(e, True))
            cache["count"] = count
        return cache["local" if local else "national"]

    def search_all(self, query: str, top_k: int = 5) -> list[dict]:
        """가이드·법령·예규·FAQ 컬렉션 통합 검색. Q&A 용."""
        query = expand_query_abbreviations(query)  # 약어→정식어 확장
        all_chunks: list[dict] = []
        seen: dict[str, float] = {}

        for contract_type in COLLECTION_MAP:
            chunks = self.search(query, contract_type, top_k=top_k)
            for c in chunks:
                c.setdefault("contract_type_searched", contract_type)
                c.setdefault("source_type", _classify_source(c.get("document_id", "")))
                cid = c["chunk_id"]
                if cid not in seen:
                    seen[cid] = c["relevance_score"]
                    all_chunks.append(c)

        # law_articles 직접 벡터 검색
        # 지방계약법 청크는 질문에 지방/지자체 키워드가 있을 때만 포함
        # (기본 관점은 국가계약법 — 없으면 국가계약 케이스에 노이즈로 작용)
        is_local_gov_q = any(kw in query for kw in ("지방", "지자체", "지방자치"))
        try:
            # 2026-07-29: 색인측과 동일한 다국어 EF 필수 — 미지정 시 기본(영어) EF로 질의돼 난수 검색
            law_col = self._client.get_collection(LAW_ARTICLES_COLLECTION, embedding_function=self._ef)

            # 2026-05-31: 도메인 키워드 부스팅 — 한국어 임베딩이 못 잡는 핵심 법령 조문 강제 매칭
            # 예: "수의계약 사유" → 시행령 제26조 (제목에 '수의계약에 의할 수 있는 경우')
            DOMAIN_KEYWORDS = ["수의계약", "적격심사", "협상에 의한 계약", "협상", "제한경쟁",
                               "지명경쟁", "일반경쟁", "낙찰자", "예정가격", "입찰공고",
                               "부정당업자", "중소기업자간", "지역제한"]
            try:
                triggered = [kw for kw in DOMAIN_KEYWORDS if kw in query]
                # 법령 간 용어차 동의어 확장 — 지방계약법령은 "지명경쟁입찰" 대신
                # "지명입찰"을 쓴다(법 제9조·시행령 제22조). 국가 질의어로 지방 조문도 잡히게.
                if "지명경쟁" in triggered:
                    triggered.append("지명입찰")
                if triggered:
                    # (2026-08-15 메모리 수리) 종전엔 여기서 컬렉션 전량(8,953청크)을
                    # get()해 요청당 25~50MB 임시 객체를 만들었다 — RSS 계단식 성장의
                    # 주범. 사전 구축 캐시(정렬 완료·필요 필드만)를 순회하는 것으로 교체.
                    # 정렬 규약(시행령 우선·지자체 모드)은 _kw_boost_items에 그대로 이식.
                    added = 0
                    for e in self._kw_boost_items(law_col, is_local_gov_q):
                        if added >= 6:  # 키워드 부스팅 최대 6건 (시행령 우선 정렬 후)
                            break
                        if e["cid"] in seen:
                            continue
                        if not is_local_gov_q and e["jibang"]:
                            continue
                        # 2026-07-29 교정: child(항 단위, 코퍼스 65%)를 배제하던 필터 제거 —
                        # 정답이 child(예: 제26조 제1항 (계속1)의 공사 4억)에 있는 경우가 많다.
                        # 검사 창도 law_ref+본문 400자로 확대(첫 200자가 무관 내용인 청크 대응).
                        if any(kw in e["head"] for kw in triggered):
                            seen[e["cid"]] = 0.95
                            all_chunks.append({
                                "chunk_id": e["cid"],
                                "document_id": (e["law_name"] or LAW_ARTICLES_COLLECTION),
                                "section_title": e["law_ref"],
                                "chunk_type": "law_article",
                                "chunk_level": e["chunk_level"],
                                "content": e["content"],
                                "relevance_score": 0.95,
                                "contract_type": "law",
                                "source_type": "law",
                                "matched_via": "keyword",
                                "law_refs": [],
                            })
                            added += 1
                # 2026-07-30: 지자체 질문은 행안부 예규(「지방자치단체 입찰시 낙찰자
                # 결정기준」 등, admin_rules 컬렉션)도 대칭 부스팅 — 지방법령 승격이
                # rerank 상위를 채우면서 예규 정답 청크가 밀려나는 회귀 방지(local-award).
                if triggered and is_local_gov_q:
                    ar_col = self._client.get_collection(ADMIN_RULES_COLLECTION,
                                                         embedding_function=self._ef)
                    added_ar = 0
                    for kw in triggered:
                        if added_ar >= 3:
                            break
                        r = ar_col.get(where_document={"$contains": kw},
                                       include=["documents", "metadatas"], limit=50)
                        for cid, doc, meta in zip(r.get("ids") or [],
                                                  r.get("documents") or [],
                                                  r.get("metadatas") or []):
                            if added_ar >= 3 or cid in seen:
                                continue
                            meta = meta or {}
                            name = " ".join(str(meta.get(k) or "") for k in
                                            ("law_name", "rule_name", "source"))
                            if "지방자치단체" not in name:
                                continue
                            seen[cid] = 0.95
                            all_chunks.append({
                                "chunk_id": cid,
                                "document_id": name.strip() or ADMIN_RULES_COLLECTION,
                                "section_title": meta.get("section_title", "") or name.strip(),
                                "chunk_type": "admin_rule",
                                "chunk_level": meta.get("chunk_level", "single"),
                                "content": doc[:1200],
                                "relevance_score": 0.95,
                                "contract_type": "law",
                                "source_type": "admin_rule",
                                "matched_via": "keyword",
                                "law_refs": [],
                            })
                            added_ar += 1
            except Exception:
                pass

            results = law_col.query(
                query_texts=[query],
                # 필터로 줄어들 수 있어 over-fetch
                n_results=min(top_k * 2, law_col.count() or 1),
                include=["documents", "metadatas", "distances"],
            )
            docs = results.get("documents", [[]])[0]
            metas = results.get("metadatas", [[]])[0]
            dists = results.get("distances", [[]])[0]
            ids = results.get("ids", [[]])[0]
            added_law = 0
            parent_refs_to_fetch: set[str] = set()
            # ENV: HIERARCHICAL_LAW_RAG=false 면 parent fetch 비활성 (A/B 비교용)
            import os
            _hier_on = os.environ.get("HIERARCHICAL_LAW_RAG", "true").lower() != "false"
            for i, (doc, meta, dist) in enumerate(zip(docs, metas, dists)):
                if not is_local_gov_q and (meta.get("law_name", "") or "").startswith("지방자치단체"):
                    continue  # 국가계약 질문에 지방계약법 노이즈 제외
                cid = ids[i] if i < len(ids) else f"law_{i}"
                chunk_level = meta.get("chunk_level", "single")
                parent_ref = meta.get("parent_ref", "")
                if cid not in seen:
                    seen[cid] = 0.0
                    all_chunks.append({
                        "chunk_id": cid,
                        "document_id": (meta.get("law_name") or LAW_ARTICLES_COLLECTION),
                        "section_title": meta.get("law_ref", ""),
                        "chunk_type": "law_article",
                        "chunk_level": chunk_level,
                        "content": doc[:600],
                        "relevance_score": round(max(0.0, 1.0 - dist), 3),
                        "contract_type": "law",
                        "source_type": "law",
                        "law_refs": [],
                    })
                    added_law += 1
                    # Hierarchical: child 매칭 시 parent도 큐
                    if _hier_on and chunk_level == "child" and parent_ref:
                        parent_refs_to_fetch.add(parent_ref)
                    if added_law >= top_k:
                        break
            # parent 청크 추가 fetch
            for p_ref in parent_refs_to_fetch:
                try:
                    p_result = law_col.get(
                        where={"law_ref": p_ref},
                        include=["documents", "metadatas"],
                    )
                    for p_doc, p_meta in zip(p_result.get("documents", []), p_result.get("metadatas", [])):
                        p_cid = f"law_parent_{p_ref}"
                        if p_cid in seen: continue
                        seen[p_cid] = 0.0
                        all_chunks.append({
                            "chunk_id": p_cid,
                            "document_id": ((p_meta or {}).get("law_name") or LAW_ARTICLES_COLLECTION),
                            "section_title": f"[조 전체] {p_meta.get('law_ref', p_ref)}",
                            "chunk_type": "law_article",
                            "chunk_level": "parent",
                            "content": p_doc[:1200],  # parent는 더 긴 컨텍스트
                            "relevance_score": 0.85,
                            "contract_type": "law",
                            "source_type": "law",
                            "law_refs": [],
                        })
                except Exception:
                    continue
        except Exception:
            pass

        # doc2query (청크별 가상질문) — 실무 어휘 질문을 법령 어휘 청크로 연결하는 브리지.
        # 가상질문에 매칭되면 원본 청크를 본문으로 가져온다. 컬렉션 부재 시 자동 생략.
        try:
            d2q_col = self._client.get_collection(DOC2QUERY_COLLECTION, embedding_function=self._ef)
            d2q_results = d2q_col.query(
                query_texts=[query],
                n_results=min(top_k, d2q_col.count() or 1),
                include=["documents", "metadatas", "distances"],
            )
            d2q_docs = d2q_results.get("documents", [[]])[0]
            d2q_metas = d2q_results.get("metadatas", [[]])[0]
            d2q_dists = d2q_results.get("distances", [[]])[0]
            for vq, meta, dist in zip(d2q_docs, d2q_metas, d2q_dists):
                orig_cid = meta.get("original_chunk_id")
                orig_col = meta.get("original_collection")
                if not orig_cid or not orig_col or orig_cid in seen:
                    continue
                try:
                    src_col = self._client.get_collection(orig_col, embedding_function=self._ef)
                    r2 = src_col.get(ids=[orig_cid], include=["documents", "metadatas"])
                    if not r2["documents"]:
                        continue
                    seen[orig_cid] = 0.0
                    doc2 = r2["documents"][0]
                    om = r2["metadatas"][0] or {}
                    all_chunks.append({
                        "chunk_id": orig_cid,
                        "document_id": om.get("law_name") or om.get("document_id", orig_col),
                        "section_title": om.get("law_ref") or om.get("section_title", ""),
                        "chunk_type": om.get("chunk_type", "law_article" if orig_col == LAW_ARTICLES_COLLECTION else ""),
                        "chunk_level": om.get("chunk_level", "single"),
                        "content": doc2[:800],
                        "relevance_score": round(max(0.0, 1.0 - dist), 3),
                        "contract_type": om.get("contract_type", "law" if orig_col == LAW_ARTICLES_COLLECTION else ""),
                        "source_type": "law" if orig_col == LAW_ARTICLES_COLLECTION else "guide",
                        "matched_via": "doc2query",
                        "matched_question": (vq or "")[:160],
                        "law_refs": [],
                    })
                except Exception:
                    continue
        except Exception:
            pass

        # faq (공개 가이드 파생 Q&A) 직접 벡터 검색 — 부스팅 없음, 후보 풀에 추가만
        try:
            faq_col = self._client.get_collection(FAQ_COLLECTION, embedding_function=self._ef)
            results = faq_col.query(
                query_texts=[query],
                n_results=min(top_k, faq_col.count() or 1),
                include=["documents", "metadatas", "distances"],
            )
            docs = results.get("documents", [[]])[0]
            metas = results.get("metadatas", [[]])[0]
            dists = results.get("distances", [[]])[0]
            ids = results.get("ids", [[]])[0]
            for i, (doc, meta, dist) in enumerate(zip(docs, metas, dists)):
                cid = ids[i] if i < len(ids) else f"faq_{i}"
                if cid not in seen:
                    seen[cid] = 0.0
                    # FAQ 청크는 dedup이 별개 문서로 인식하도록 document_id에 prefix
                    orig_doc = meta.get("document_id", "") or meta.get("source_collection", FAQ_COLLECTION)
                    all_chunks.append({
                        "chunk_id": cid,
                        "document_id": f"faq_{orig_doc}",
                        "section_title": meta.get("section_title", ""),
                        "chunk_type": "faq",
                        "content": doc[:800],
                        "relevance_score": round(max(0.0, 1.0 - dist), 3),
                        "contract_type": "faq",
                        "source_type": "faq",
                        "is_faq": True,
                        "law_refs": [],
                    })
        except Exception:
            pass

        # admin_rules (행정규칙) 직접 벡터 검색
        try:
            adm_col = self._client.get_collection(ADMIN_RULES_COLLECTION, embedding_function=self._ef)
            results = adm_col.query(
                query_texts=[query],
                n_results=min(top_k, adm_col.count() or 1),
                include=["documents", "metadatas", "distances"],
            )
            docs = results.get("documents", [[]])[0]
            metas = results.get("metadatas", [[]])[0]
            dists = results.get("distances", [[]])[0]
            ids = results.get("ids", [[]])[0]
            for i, (doc, meta, dist) in enumerate(zip(docs, metas, dists)):
                cid = ids[i] if i < len(ids) else f"adm_{i}"
                if cid not in seen:
                    seen[cid] = 0.0
                    # 행정규칙은 권위 자료 + 직접 벡터 검색 결과이므로
                    # 다른 컬렉션의 키워드 부스팅(~0.8)과 형평성 맞추기 위해 +0.35 보정
                    raw = 1.0 - dist
                    score = round(min(0.95, raw + 0.35) if raw > 0.3 else raw, 3)
                    all_chunks.append({
                        "chunk_id": cid,
                        "document_id": ADMIN_RULES_COLLECTION,
                        "section_title": meta.get("section_title") or meta.get("law_ref", ""),
                        "chunk_type": "admin_rule",
                        "content": doc[:800],
                        "relevance_score": score,
                        "contract_type": "admin_rule",
                        "source_type": "guide",
                        "law_refs": [],
                    })
        except Exception:
            pass

        # 키워드 폴백: 조사 제거된 핵심어 쿼리로 벡터 검색 정확도 향상
        kws = self._extract_search_keywords(query)
        if kws:
            clean_q = " ".join(kws)
            extras = self._keyword_fallback(query, kws, top_k, seen, clean_query=clean_q)
            all_chunks.extend(extras)

        # 2026-05-20: Dense + BM25 RRF — 부스팅(인위적 점수 조작) 없이 정렬 순서만 통합.
        # 토픽 부스팅 제거 시 얻은 교훈: 인위적 점수 조작은 효과 미미·부작용.
        # 여기서는 (1) BM25 top 10 중 누락된 청크 후보 추가, (2) RRF로 정렬만 수행.
        # relevance_score(UI 표시용)는 dense 원본 유지.
        if self._bm25_data:
            try:
                q_tokens = _bm25_tokenize(query)
                if q_tokens:
                    bm25 = self._bm25_data["bm25"]
                    bm25_ids = self._bm25_data["ids"]
                    bm25_meta = self._bm25_data["meta"]
                    scores = bm25.get_scores(q_tokens)
                    top_idx = scores.argsort()[-30:][::-1]
                    bm25_rank_map: dict[str, int] = {bm25_ids[i]: r + 1 for r, i in enumerate(top_idx) if scores[i] > 0}

                    # (1) BM25 top 10 중 dense 결과에 없는 청크 후보 추가
                    seen_ids = {c["chunk_id"] for c in all_chunks}
                    for i in top_idx[:10]:
                        cid = bm25_ids[i]
                        if cid in seen_ids:
                            continue
                        cname = bm25_meta[i]["collection"]
                        try:
                            col = self._client.get_collection(cname, embedding_function=self._ef)
                            r = col.get(ids=[cid], include=["documents", "metadatas"])
                            if r["documents"]:
                                doc = r["documents"][0]
                                meta = r["metadatas"][0]
                                # FAQ 컬렉션은 dedup이 별개 문서로 인식하도록 document_id에 prefix
                                is_faq = (cname == FAQ_COLLECTION)
                                doc_id = meta.get("document_id", cname)
                                if is_faq:
                                    doc_id = f"faq_{doc_id}"
                                all_chunks.append({
                                    "chunk_id": cid,
                                    "document_id": doc_id,
                                    "section_title": meta.get("section_title", ""),
                                    "chunk_type": "faq" if is_faq else meta.get("chunk_type", ""),
                                    "content": doc[:800],
                                    # 자연 점수 — 표시용이고 정렬은 RRF가 담당. 부스팅 아님.
                                    "relevance_score": 0.6,
                                    "contract_type": "faq" if is_faq else meta.get("contract_type", ""),
                                    "source_type": "faq" if is_faq else _classify_source(meta.get("document_id", "")),
                                    "is_faq": is_faq,
                                    "law_refs": [],
                                    "topics": meta.get("topics", ""),
                                    "bm25_rank": bm25_rank_map[cid],
                                    "bm25_only": True,
                                })
                        except Exception:
                            continue

                    # (2) RRF 정렬 — 두 ranker 통합. relevance_score는 변경 X
                    K_RRF = 60.0
                    dense_sorted = sorted(all_chunks, key=lambda x: -x["relevance_score"])
                    dense_rank_map: dict[str, int] = {c["chunk_id"]: r + 1 for r, c in enumerate(dense_sorted)}
                    for c in all_chunks:
                        cid = c["chunk_id"]
                        d_rank = dense_rank_map.get(cid, 1000)
                        b_rank = bm25_rank_map.get(cid, 1000)
                        c["_rrf"] = 1.0 / (K_RRF + d_rank) + 1.0 / (K_RRF + b_rank)
                        if cid in bm25_rank_map:
                            c["bm25_rank"] = b_rank

                    all_chunks.sort(key=lambda x: -x.get("_rrf", 0))
                    for c in all_chunks:
                        c.pop("_rrf", None)
                    return _quality_gate(all_chunks[:top_k * 3])
            except Exception:
                pass

        # BM25 미사용 시 — dense 점수로 정렬
        all_chunks.sort(key=lambda x: x["relevance_score"], reverse=True)
        return _quality_gate(all_chunks[:top_k * 3])

    @staticmethod
    def _keyword_match(query: str, doc: str) -> bool:
        keywords = re.findall(r"\d+(?:\.\d+)?억|수의계약|일반경쟁|제한경쟁|적격심사|중소기업", query)
        return any(kw in doc for kw in keywords)

    def build_context(
        self,
        content_chunks: list[dict],
        law_chunks: list[dict] | None = None,
        max_chars: int = 2000,
    ) -> str:
        """청크 리스트를 LLM 컨텍스트 문자열로 변환. 법령 조문은 구분선 뒤에 추가."""
        parts = []
        total = 0

        for c in content_chunks:
            excerpt = f"[출처: {c['section_title']}]\n{c['content']}"
            if total + len(excerpt) > max_chars:
                break
            parts.append(excerpt)
            total += len(excerpt)

        if law_chunks:
            law_budget = max(0, (max_chars * 2 // 3) - total)
            if law_budget > 0:
                law_parts = []
                law_total = 0
                for c in law_chunks:
                    excerpt = f"[법령 조문] {c.get('law_ref', c['section_title'])}\n{c['content']}"
                    if law_total + len(excerpt) > law_budget:
                        break
                    law_parts.append(excerpt)
                    law_total += len(excerpt)
                if law_parts:
                    parts.append("---\n[관련 법령 조문]")
                    parts.extend(law_parts)

        return "\n\n---\n\n".join(parts)
