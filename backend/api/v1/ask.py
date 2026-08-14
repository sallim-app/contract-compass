"""자유형식 Q&A 엔드포인트 — 신입사원 계약 절차 문의."""
import json
import re
import time
from collections import OrderedDict
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from backend.api.deps import get_rag_service, get_llm
from backend.services.rag_service import RAGService
from backend.services.llm.base import LLMProvider
from backend.services.chat_access import chat_access
from backend.services.rate_limiter import rate_limit_llm, record_llm_call

# 2026-07-20 fail-closed: 검색 근거 0건이면 LLM을 호출하지 않고 이 결정론 응답을 반환.
# 인덱스 유실·임베딩 장애 시 빈 컨텍스트로 그럴싸한 오답이 나가는 것을 차단한다(캐시도 안 남김).
_NO_EVIDENCE_ANSWER = (
    "죄송합니다. 현재 지식베이스에서 이 질문과 관련된 근거 문서를 찾지 못했습니다.\n\n"
    "근거 없는 추정 답변은 제공하지 않습니다. 질문을 더 구체적으로 바꾸시거나 "
    "계약 유형·키워드(예: 수의계약, 예정가격)를 포함해 다시 시도해 주세요. "
    "같은 문제가 반복되면 시스템 관리자에게 문의해 주세요."
)

# 2026-07-24: '근거 없음' 답변엔 무관 법령이 relevance 0.9(고신뢰)로 딸려붙던 결함 차단.
# 청크는 검색됐으나(0-hit 아님) 실제 무관 → LLM이 시스템프롬프트 규칙2대로 "현재 검색 범위에
# 해당 내용이 없습니다"류 답변을 내면, _ground_law_sources 보강이 그 무관 청크의 law_refs로
# 법령 청크를 0.9로 주입함. 이런 답변은 근거가 없다는 뜻이므로 출처 첨부 자체를 억제한다.
# 2026-07-24: 조사(에/에는/에서는) 차이만으로 새던 패러프레이즈 구멍을 메움.
# 다만 이 정규식은 2차 방어일 뿐 — 1차 방어는 _ground_law_sources의 non_law 게이트다
# (문구를 사후에 보고 지우는 방식은 원리적으로 계속 뚫린다).
_NO_EVIDENCE_MARKERS = re.compile(
    r"검색\s*범위(?:에|에는|에서|에서는)\s*(?:해당\s*)?내용이\s*없"
    r"|근거\s*문서를\s*찾지\s*못"
    r"|참고\s*자료(?:에|에는|에서|에서는)\s*(?:해당\s*)?내용이\s*(?:없|명시되어\s*있지\s*않)"
    r"|참고\s*자료(?:에|에는|에서|에서는)\s*(?:해당\s*|관련\s*)?내용을\s*찾을\s*수\s*없"
    r"|관련\s*규정을\s*확인할\s*수\s*없"
)


def _is_no_evidence_answer(answer: str | None) -> bool:
    """LLM/결정론 '근거 없음' 응답 판별 — 이런 답변엔 무관 출처를 붙이지 않는다.

    2026-07-24 보강: 조문을 실제로 인용한 답변은 '근거 없음'이 아니다.
    (오탐 실증 — "…참고 자료에 해당 내용이 없는 경우에는… 다만 시행령 제26조는 적용됩니다"류
     정상 답변이 억제되어 출처 전멸 + avg_relevance=None → 프론트 저신뢰 배너 조건까지
     거짓이 되어 '근거 없는 답이 조용히 서빙'되는 회귀가 났음.)
    """
    if not answer:
        return False
    if _LAW_CITATION_PATTERN.search(answer):
        return False
    return bool(_NO_EVIDENCE_MARKERS.search(answer))

# 인메모리 LRU 캐시 — 동일 질문 즉시 응답으로 LLM 호출 절약·체감 속도 향상
# TTL 6시간, 최대 200건. 청크 데이터 변경 시 백엔드 재시작으로 자동 무효화
_CACHE_TTL = 6 * 3600
_CACHE_MAX = 200
_answer_cache: "OrderedDict[str, tuple[float, dict]]" = OrderedDict()

# 2026-07-30 P0: rerank 무산출 시 완화 경로 은폐 차단 — 출처 문턱 상수(비스트림·스트림 공용).
# 배경: BM25-only 후보는 rag_service가 relevance_score=0.6을 하드코딩하는데 dense 문턱
# MIN_RELEVANCE=0.60과 경계가 같아, rerank가 죽어 있으면(키 미설정·호출 실패) 검증 없이
# 항상 통과했다. rerank 무산출이면 (1) dense 문턱 상향 (2) BM25-only 후보 배제로 fail-closed.
_MIN_RELEVANCE = 0.60          # dense fallback (rerank 있을 때의 안전망)
_INTERNAL_LAW_MIN = 0.85       # internal/law dense fallback
_MIN_RELEVANCE_NO_RERANK = 0.80    # rerank 무산출 시 상향 문턱
_INTERNAL_LAW_MIN_NO_RERANK = 0.92
_RERANK_GENERAL = 0.05         # cross-encoder: 명백히 무관만 컷
_RERANK_INTERNAL_LAW = 0.15    # internal/law는 약간 더 엄격 (무관 강제 노출 차단)
_rerank_inactive_warned = False


def _apply_rerank_guard(chunks: list[dict]) -> tuple[list[dict], bool]:
    """rerank 산출물(_rerank_score) 유무로 비활성을 판정해 가드 적용.

    판정을 설정값이 아니라 실제 산출물로 하는 이유: 키가 있어도 한도 초과·네트워크
    오류로 rerank()가 입력을 그대로 반환하는 경로가 있다(무산출 = 동일하게 미검증).
    반환: (BM25-only 배제된 후보, no_rerank 플래그). 경고는 프로세스당 최초 1회.
    """
    global _rerank_inactive_warned
    if not chunks or any(c.get("_rerank_score") is not None for c in chunks):
        return chunks, False
    if not _rerank_inactive_warned:
        import logging
        logging.getLogger("contract_compass").warning(
            "rerank 비활성(COHERE_API_KEY/RERANK_ENDPOINT 미설정 또는 호출 실패) — "
            "dense 문턱 %.2f/%.2f 상향 + BM25-only 후보 배제로 동작",
            _MIN_RELEVANCE_NO_RERANK, _INTERNAL_LAW_MIN_NO_RERANK)
        _rerank_inactive_warned = True
    # BM25-only 후보는 relevance_score가 하드코딩(0.6)이라 rerank 없이는 검증 수단이 없다
    return [c for c in chunks if not c.get("bm25_only")], True


def _chunk_passes(c: dict, no_rerank: bool) -> bool:
    """출처 노출 문턱 — rerank 점수 우선, 무산출 시 상향된 dense 문턱."""
    rs = c.get("_rerank_score")
    is_il = c.get("source_type") in ("internal", "law")
    if rs is not None:
        return rs >= (_RERANK_INTERNAL_LAW if is_il else _RERANK_GENERAL)
    score = c.get("relevance_score", 0.0)
    if no_rerank:
        return score >= (_INTERNAL_LAW_MIN_NO_RERANK if is_il else _MIN_RELEVANCE_NO_RERANK)
    return score >= (_INTERNAL_LAW_MIN if is_il else _MIN_RELEVANCE)


def _norm_query(q: str) -> str:
    return re.sub(r"\s+", " ", q).strip().lower()


def _cache_get(q: str) -> dict | None:
    key = _norm_query(q)
    entry = _answer_cache.get(key)
    if not entry:
        return None
    ts, payload = entry
    if time.time() - ts > _CACHE_TTL:
        _answer_cache.pop(key, None)
        return None
    # LRU 갱신
    _answer_cache.move_to_end(key)
    return payload


def _cache_set(q: str, payload: dict) -> None:
    key = _norm_query(q)
    _answer_cache[key] = (time.time(), payload)
    _answer_cache.move_to_end(key)
    while len(_answer_cache) > _CACHE_MAX:
        _answer_cache.popitem(last=False)

# 자료 청크에 박혀 있는 괄호 풀이를 컨텍스트 단계에서 제거 → LLM이 모방할 패턴이 사라짐.
# - 한글 뒤 + (한글/공백/쉼표/가운뎃점/곱하기/슬래시/등호로만 구성, 5자 이상)
# - 숫자·따옴표·법령번호 등은 보존 (예: "(이하 '갑')", "제65조 제3항", "100분의 86")
_INLINE_GLOSS = re.compile(r'(?<=[가-힣])\s*\(([가-힣\s,·×/=]{5,})\)')


def _strip_inline_glosses(text: str) -> str:
    return _INLINE_GLOSS.sub('', text)

router = APIRouter(prefix="/ask", tags=["ask"])


class AskRequest(BaseModel):
    question: str = Field(..., min_length=2, max_length=500)
    top_k: int = Field(default=8, ge=1, le=30)  # 2026-05-31: 레이턴시 개선 — 20→8 (분석 결과 top 5만 노출되어 풀 작아도 충분)
    # 2026-05-31: PC 사이드 채팅 — 현재 화면 컨텍스트(step·project_name·suggested_method 등)
    # 시스템 프롬프트 prefix로 주입돼 "이 사업에 PQ 필요?" 같은 화면 맥락 질문 정확 답변
    context: dict | None = None


class AskSource(BaseModel):
    chunk_id: str
    section_title: str
    excerpt: str          # 200자 미리보기
    content: str = ""     # 전체 본문 (사용자 검증용)
    relevance_score: float
    source_type: str = "textbook"
    document_id: str = "" # 어느 문서에서 왔는지
    chunk_level: str = "single"  # parent | child | single (Hierarchical RAG)
    matched_via: str = "vector"  # vector | bm25 | doc2query | hybrid
    matched_question: str = ""    # Doc2Query 매칭 시 가상질문 텍스트 (UX 검증력)


class AskResponse(BaseModel):
    answer: str
    sources: list[AskSource]
    timing: dict[str, float] | None = None  # search/rerank/llm 단계별 (ms)
    unverified_citations: list[str] = []  # 본문에 인용된 조문 중 sources에 매칭 안 된 것 (환각 의심)
    avg_relevance: float | None = None     # sources 평균 관련도 — 답변 신뢰도 지표 (UI에서 0.6 미만이면 경고)


_SYSTEM_PROMPT = """당신은 공공계약 실무 전문 AI 어시스턴트입니다.
사용자가 계약 절차, 법령, 실무에 관해 질문하면 아래 참고 자료를 바탕으로 명확하고 친절하게 답변하세요.
특정 기관을 전제하지 말고 국가계약법·지방계약법 등 공공계약 법령 관점에서 답변하세요.

[현재 검색 가능한 자료]
- 공공계약 실무 가이드 (용역·물품·공사)
- 건설엔지니어링·SW 구매 가이드
- 감사원 공공계약 Q&A
- 국가계약법·시행령·시행규칙 등 공공계약 법령·예규 조문

[답변 원칙]
1. 위 참고 자료에 있는 내용만 답변하세요.
2. 참고 자료에 없으면 "현재 검색 범위에 해당 내용이 없습니다"라고만 말하세요.
   - 어느 부서에 문의하라거나, 어떤 내부 규정에 있을 것이라는 추측을 절대 하지 마세요.
   - 제공되지 않은 문서(기관 내규, 지침 등)에 내용이 있다고 가정하지 마세요.
3. 법령 조문 번호(예: 시행령 제26조)를 명시하면 신뢰도가 높아집니다.
4. 절차를 설명할 때는 번호 목록(1. 2. 3.)으로 단계별 작성하세요.
5. 답변은 핵심만 **2~3문단 이내, 약 200~300자** 로 작성하세요. 불필요한 인사·맥락 반복·중복 설명을 제거하세요.
6. 한국어로만 답변하세요.
6-1. **수치·기간·비율은 반드시 구체적으로 답하세요** (예: "2년", "0.5/1000", "90일", "100분의 70"). "별도 규정에 따른다", "특수조건 참조" 같은 회피 표현으로만 답하지 말고, 참고 자료에 명시된 수치를 인용하세요.
6-2. **질문에 여러 항목(공사·물품·용역 등)이 명시되면 각 항목별로 구체적으로 답하세요.** 일부 항목만 답하고 나머지를 "기타" 처리하지 마세요.
7. **호칭 금지**: 답변을 시작할 때 "신입사원님", "담당자님", "안녕하세요" 같은 인사·호칭을 절대 쓰지 마세요. 질문의 핵심 답변으로 바로 시작하세요.
   - 잘못된 예: "신입사원님, 질문에 답변드립니다. 수의계약은..."
   - 올바른 예: "수의계약은..."
8. **중요**: 답변 본문에 괄호로 용어 정의를 절대 추가하지 마세요. 별도 용어사전 UI가 자동으로 정의를 표시합니다.
   - 잘못된 예: "낙찰률(예정가격에 대한 계약금액 비율)을 곱하여..."
   - 올바른 예: "낙찰률을 곱하여..."
   - 괄호는 법령 조문 번호(예: 시행령 제65조), 수치 단위, 정확한 인용에만 사용하세요.
9. **별표·표 번호는 참고 자료에 명시된 것만 그대로 인용하세요.** 자료에 별표 번호가 없으면 별표 번호를 지어내지 말고(예: "[별표 0]", "별표 0" 같은 표기 절대 금지) 통과점수·배점·금액구간 등 실제 기준 내용만 설명하세요. 적격심사 별표는 계약유형(공사·물품·용역)과 기준종류(일반 적격심사·중기간 계약이행능력심사)에 따라 별표 체계가 다르므로, 자료에 적힌 정확한 별표만 인용하세요.
10. **법령 조문 번호 환각 금지**: "시행령 제N조", "법 제N조", "제N조 제N항 제N호" 같은 조문 번호는 **반드시 참고 자료에 명시된 조문만 그대로** 인용하세요. 참고 자료에 없는 조문 번호는 **절대 만들지 마세요**. 조문 번호가 확실하지 않으면 번호 없이 "관련 시행령 조항" 같은 일반 표현을 쓰세요.
   - 잘못된 예: 자료에 "시행령 제26조"만 있는데 "시행령 제42조에 따라..."로 답변 (환각)
   - 올바른 예: 자료에 조문이 명시된 경우만 그대로 인용. 없으면 "관련 시행령 규정에 따라..."

10-1. **[확정 사실] 블록이 있으면 그 값이 최우선입니다.** 그 블록은 검색이 아니라 룰엔진이
   금액에서 계산한 결정론 값입니다. 참고 자료(검색 문서)에 다른 수치가 보이더라도 개정 전
   자료일 수 있으므로, [확정 사실]의 수치를 그대로 답하고 검색 문서의 상충 수치는 쓰지 마세요.

11. **부적절 요청 거절**: 낙찰 확률을 인위적으로 높이는 방법, 특정 업체에 유리하게 조건을 설계하는 방법, 담합·입찰 방해에 해당할 수 있는 요청에는 검색 여부와 무관하게 **명시적으로 거절**하세요 — "해당 요청은 공정경쟁 원칙(국가계약법 제7조 일반경쟁 원칙)에 반할 수 있어 안내할 수 없습니다"라고 답하고, 적법한 대안(제한·지명경쟁의 법정 요건 등)이 있으면 그것만 안내하세요.

[핵심 임계값 표 — 검증된 확정 값. 금액·한도 질문은 이 표가 검색 결과·사전지식보다 우선한다]
※ 아래는 국가계약법령(국가기관·공기업 공통 국가계약 체계) 기준. 학습된 옛 수치(예: 고시금액 2.1억)를 절대 쓰지 말 것.
- 수의계약 가능 한도(시행령 제26조 제1항 제5호 가목): 종합공사 4억 이하 / 전문공사 2억 이하 / 전기·정보통신·소방 등 그 밖의 공사 1.6억 이하 / 물품·용역 일반 2천만 이하, **소기업·소상공인, 여성·장애인기업, 학술연구 등 요건 충족 시 1억 이하**
- 1인 견적 허용 한도(시행령 제30조, 위와 다른 개념): 2천만 이하 (여성기업·장애인기업 등 5천만 이하). 2천만 초과는 전자조달 2인 이상 견적
- 고시금액: **2.3억** (기획재정부고시 제2024-42호, 2025.1.1.~) — 물품·용역 중소기업자간 경쟁 원칙 경계
- 공기업·준정부기관 국제입찰: 물품·용역 **7.1억** 이상, 공사 **265억** 이상 (기획재정부고시 제2024-42호)
- 지방자치단체 국제입찰(행정안전부고시 제2024-95호, 2025.1.1.~): 공사 **265억** 이상, 물품·용역 **3억 5천만** 이상(자치구·군은 7억 1천만 이상)
- 국가기관 국제입찰 고시금액은 위와 다름 — 기재부 고시 확인 안내
- 종합심사낙찰제: 공사 **100억** 이상 (시행령 제42조 제4항) / PQ 대상: 300억 이상 (발주기관 세부기준 확인)
- 지방자치단체 수의계약 한도(지방계약법 시행령 제25조 제1항 제5호): 종합공사 **4억** 이하 / 전문공사 **2억** 이하 / 그 밖의 공사(전기 등) **1.6억** 이하 — **공사 한도 구조는 국가계약법령과 동일**(차이라고 말하지 말 것) / 물품·용역 일반 2천만 이하
- 지자체 여성기업·장애인기업 수의계약 한도(지방계약법 시행령 제25조 제1항 제5호 바목, 물품·용역): **1억 이하** — 5천만은 수의 한도가 아니라 1인 견적 한도(제30조)임을 혼동 금지
- 소액수의 최저투찰율(조달청 기준): 일반 용역 87.995% / **단순노무용역 89.995%**
- 핵심 근거 조문 매핑: 수의계약 사유=시행령 제26조 / **지역제한 경쟁=시행령 제21조 제1항 제6호** / 실적제한=제21조 제1항 제1호 / 적격심사=시행령 제42조 / 종합심사낙찰제=제42조 제4항 / PQ=시행령 제13조 / 재공고입찰=시행령 제20조
- 다년도 계약 구분: **장기계속계약**=국가계약법 제21조 — 총액으로 낙찰하되 각 회계연도 예산 범위에서 연차별 계약·이행(예산은 매년 확보) / **계속비계약**=국가재정법 제23조의 계속비(총액·연부액을 미리 국회 의결) 기반으로 수년도 지출이 사전 확정된 계약 — 차이의 핵심은 **예산의 사전 확정 여부**
※ 임계값 답변 시 반드시 "이하/미만" 구분을 명시하고, 수의계약 한도와 1인 견적 한도를 혼동하지 말 것.
※ **검색 결과의 수치가 이 표와 다르면 그 수치는 구(舊) 고시값이다**(예: 고시금액 2.1억, 국제입찰 6.5억·6.36억 등) — 반드시 이 표의 현행값으로 답하고, 필요하면 "과거 기준은 달랐다"고 부기하라."""


_CT_LABEL = {"service": "용역", "product": "물품", "construction": "공사"}


# 2026-06-01: 법령 조문 환각 후처리 검증
# 본문에서 "시행령 제42조", "법 제7조", "제26조 제1항 제5호" 같은 조문 인용을 추출하고
# sources(RAG 매칭)·content 어디에도 안 나오면 unverified로 표시 → UI에서 경고.
import re

_LAW_CITATION_PATTERN = re.compile(
    r"(?:국가계약법|지방계약법|공공기관운영법|중소기업제품구매촉진법|법|시행령|시행규칙|규칙|규정)\s*"
    r"제\s*\d+\s*조"
    r"(?:\s*제\s*\d+\s*항)?(?:\s*제\s*\d+\s*호)?",
)


def _verify_citations(answer: str, sources: list) -> list[str]:
    """답변 본문의 조문 인용 중 sources 어디에도 안 나오는 것 반환 (환각 의심)."""
    cites = set(_LAW_CITATION_PATTERN.findall(answer))
    if not cites:
        return []
    # sources에 있는 모든 텍스트 합쳐서 검사 (excerpt + section_title)
    haystack = " ".join(
        (getattr(s, "section_title", "") or "") + " " + (getattr(s, "excerpt", "") or "")
        for s in sources
    )
    haystack_norm = re.sub(r"\s+", "", haystack)
    unverified = []
    for c in cites:
        # 2026-07-29 정정: 조문 번호만 비교하면 무관 법령의 동일 조번호가 '검증됨'으로
        # 통과했다 — 인용에 법령명이 있으면 법령명+조번호가 함께 있어야 검증 인정.
        article_match = re.search(r"제\s*\d+\s*조", c)
        if not article_match:
            continue
        article = re.sub(r"\s+", "", article_match.group())
        law_part = re.sub(r"\s+", "", c[: article_match.start()]).rstrip("ㆍ·,")
        if article not in haystack_norm:
            unverified.append(c)
        elif law_part and law_part not in haystack_norm:
            unverified.append(c)
    return unverified


def _avg_relevance(sources: list) -> float | None:
    """sources 평균 관련도 — 답변 신뢰도 지표.

    2026-07-24: law_refs 조회로 주입된 조문은 검색 점수가 아니라 하드코드 0.9라
    평균에 넣으면 신뢰도가 인플레된다(무관 조문이 '고신뢰'로 표시되던 사고).
    실제 검색으로 매칭된 출처만 평균에 반영하고, 주입 조문뿐이면 지표 없음(None).
    """
    if not sources:
        return None
    scored = [s for s in sources if getattr(s, "matched_via", "") != "law_refs"]
    if not scored:
        return None
    scores = [getattr(s, "relevance_score", 0.0) or 0.0 for s in scored]
    return round(sum(scores) / len(scores), 3) if scores else None


def _sv(s, key: str, default=""):
    """AskSource 객체·dict 양쪽에서 필드 읽기 (두 엔드포인트가 타입이 달라 통일)."""
    if isinstance(s, dict):
        return s.get(key, default)
    return getattr(s, key, default)


def _ref_pair(ref: str):
    """법령 참조 문자열 → (법령명, 조문토큰). '[조 전체]' 등 브래킷 프리픽스 제거. 조문 없으면 None."""
    if not ref:
        return None
    ref = re.sub(r"\[[^\]]*\]", "", ref).strip()
    m = re.search(r"제\s*\d+\s*조(?:의\s*\d+)?", ref)
    if not m:
        return None
    art = re.sub(r"\s+", "", m.group())
    law = re.sub(r"\s+", "", ref[: m.start()]).rstrip("ㆍ·,")
    return (law, art)


# 2026-07-09: 법령 인용 환각 정정 (사용자 제보 — 설계변경 질의에 무관한 제111조의6[소위원회] 등이
# 출처로 붙던 문제). law 출처를 "의미유사도로 딸려온 조문"이 아니라 "답변에 실제로 쓰인 근거 청크가
# law_refs로 지목한 조문" + "답변 본문이 명시 인용한 조문"에만 종속시킨다.
# 결정론적·오프라인 안전(임베딩/LLM/Cohere 불필요) — 폐쇄망 내부망에서도 동작.
# 참고: rag_service.search_all의 DOMAIN_KEYWORDS 부스팅(첫200자 키워드 매칭)·parent 하드코딩
#   relevance(0.85)가 취약원이나, 그 수리 대신 상류에서 근거로 걸러내는 정공법을 택함.
def _ground_law_sources(answer: str, chunks: list[dict], sources: list, as_dict: bool = False, max_law: int = 3) -> list:
    # 1) 허용집합 (법령명, 조문토큰) — 근거 청크 law_refs + 답변 인용 조문
    allow_pairs: set = set()
    content_refs: list[str] = []  # 정답 조문 보강 조회용 (정식 ref 문자열)
    content_used = 0
    for c in chunks:
        if _sv(c, "source_type") == "law":
            continue
        refs = c.get("law_refs") or [] if isinstance(c, dict) else []
        if not refs:
            continue
        for r in refs:
            content_refs.append(r)
            for v in RAGService._ref_lookup_variants(r):
                p = _ref_pair(v)
                if p:
                    allow_pairs.add(p)
        content_used += 1
        if content_used >= 3:  # 상위 근거 청크 3개까지만 채택 (정밀도)
            break

    try:
        from backend.api.v1.law import _LAW_ALIASES
    except Exception:
        _LAW_ALIASES = {}
    answer_cite_refs: list[str] = []  # 사후 접지 조회용 (실존 조문만 sources에 붙는다)
    for cite in set(_LAW_CITATION_PATTERN.findall(answer or "")):
        p = _ref_pair(cite)
        if not p:
            continue
        law, art = p
        law = re.sub(r"\s+", "", _LAW_ALIASES.get(law, law)) if law else ""
        if law:
            allow_pairs.add((law, art))
            answer_cite_refs.append(cite)
        else:
            # 2026-07-29 정정: 법령명 없는 인용("제17조에 따라")을 전 법령 와일드카드로
            # 풀면 16개 무관 법령의 동일 조번호 66청크가 통과했다(Codex 적대검증) —
            # 국가계약 계열(코퍼스 기본 관점)로만 한정 확장한다.
            for base in ("국가계약법시행령", "국가계약법시행규칙", "국가계약법"):
                allow_pairs.add((base, art))
            answer_cite_refs.append(f"국가계약법 시행령 {art}")

    def _keep_law(s) -> bool:
        p = _ref_pair(_sv(s, "section_title"))
        if not p:
            return False  # 파싱 불가한 법령 제목 → 검증 불가로 제거
        law, art = p
        # ("", art) 와일드카드 disjunct 제거(2026-07-29) — 법령명까지 일치해야 통과
        return (law, art) in allow_pairs

    non_law = [s for s in sources if _sv(s, "source_type") != "law"]
    kept_law = [s for s in sources if _sv(s, "source_type") == "law" and _keep_law(s)]
    have_pairs = {_ref_pair(_sv(s, "section_title")) for s in kept_law}
    have_pairs.discard(None)

    # 2) 보강 — 근거 청크가 지목한 조문 중 아직 없는 것을 law 컬렉션에서 전문 조회해 추가
    added: list = []
    try:
        from backend.api.v1.law import _get_collection as _law_col
        col = _law_col()
    except Exception:
        col = None
    # 2026-07-29 정정: 종전엔 비-law 근거 0건이면 보강을 막았지만(orphan 우려), 그 결과
    # 조문 번호까지 든 실질 답변이 sources 0건으로 나갔다(적대검증 6건). 이제 답변이
    # 인용한 조문도 조회 대상에 포함하되, **law_articles에 실존하는 조문만** 붙는다 —
    # 실존하지 않는 인용은 여전히 unverified_citations로 남아 환각 우회는 차단된다.
    if col is not None:
        for r in content_refs + answer_cite_refs:
            if len(kept_law) + len(added) >= max_law:
                break
            pr = _ref_pair(r)
            if not pr or pr in have_pairs:
                continue
            # 2026-07-17 정정: 보강은 '답변이 실제 인용한 조문'에 한정 — 검색 청크의
            # 잘못된 자체 참조(예: 감사원 FAQ가 시행령 27조를 '법 27조'로 오기)가
            # 무관한 법 전문을 답변 출처로 밀어넣던 문제 차단.
            _law_n = re.sub(r"\s+", "", _LAW_ALIASES.get(pr[0], pr[0])) if pr[0] else ""
            if (_law_n, pr[1]) not in allow_pairs and ("", pr[1]) not in allow_pairs:
                continue
            doc = meta = None
            for v in RAGService._ref_lookup_variants(r):
                try:
                    res = col.get(where={"law_ref": v}, include=["documents", "metadatas"])
                    docs = res.get("documents") or []
                    metas = res.get("metadatas") or []
                    if docs:
                        doc, meta = docs[0], metas[0]
                        break
                except Exception:
                    continue
            if doc is None:
                continue
            law_ref = (meta or {}).get("law_ref", r)
            added.append({
                "chunk_id": (meta or {}).get("chunk_id") or f"law_ground_{re.sub(r'[^0-9가-힣]', '_', law_ref)}",
                "section_title": law_ref,
                "excerpt": (doc or "")[:200],
                "content": doc or "",
                "relevance_score": 0.9,
                "source_type": "law",
                "document_id": law_ref,
                "matched_via": "law_refs",
            })
            have_pairs.add(pr)

    law_out = (added + kept_law)[:max_law]

    if as_dict:
        law_out = [s if isinstance(s, dict) else s.model_dump() for s in law_out]
        non_law = [s if isinstance(s, dict) else s.model_dump() for s in non_law]
    else:
        conv = []
        for s in law_out:
            if isinstance(s, dict):
                try:
                    conv.append(AskSource(**{k: v for k, v in s.items() if k in AskSource.model_fields}))
                except Exception:
                    pass
            else:
                conv.append(s)
        law_out = conv

    return law_out + non_law


# ── 낙찰하한율은 결정론 값이 진실이다 (2026-08-05 P0 수리) ─────────────────────
# 계기: 70억 종합공사를 물으면 챗봇이 85.495%를 답했다. 출처는 RAG 코퍼스의
# `(감사원)공공계약 실무가이드.pdf` — **개정 전 세대의 표**다(그 문서엔 85.495%가 14회
# 나오고 현행 값 87.495/88.745/89.745는 한 번도 안 나온다). 청크 메타에 발간연도·시행일
# 필드가 없어 신선도로 거를 방법도 없고, 어느 청크가 rerank 1위를 먹느냐에 답이 달렸다
# (같은 질문에 회차마다 85.495%·89.745%가 번갈아 나온 이유).
#
# 요율은 검색으로 알아낼 것이 아니라 **금액에서 결정론적으로 계산되는 값**이다.
# 그래서 룰엔진 답을 [확정 사실]로 문맥 맨 앞에 넣고, 시스템 프롬프트가 이 블록을
# 참고 자료보다 우선하게 한다. 검색이 낡은 표를 물어와도 답을 덮지 못한다.
#
# 보수적 설계: 금액을 **명확히** 못 읽으면 아무것도 주입하지 않는다(빈 문자열).
# 틀린 주입은 지금보다 나쁘다 — 잘못 읽은 금액으로 확정 사실을 만들면 안 된다.
_RATE_INTENT = re.compile(r"낙찰\s*하한\s*율|하한\s*율")
_CONSTRUCTION_HINT = re.compile(r"공사|건설|시설")
# "70억", "70억원", "7,000,000,000원", "1,500백만원"은 다루지 않는다(모호) — 억/원만.
_AMOUNT_EOK = re.compile(r"(\d+(?:\.\d+)?)\s*억")
_AMOUNT_WON = re.compile(r"([\d,]{7,})\s*원")


def _parse_amount(q: str) -> int | None:
    """질문에서 추정가격을 읽는다. 애매하면 None(주입 안 함)."""
    eok = _AMOUNT_EOK.findall(q)
    won = [w for w in _AMOUNT_WON.findall(q) if w.replace(",", "").isdigit()]
    # 금액이 둘 이상 나오면 어느 것이 추정가격인지 알 수 없다 → 포기
    if len(eok) + len(won) != 1:
        return None
    if eok:
        return int(float(eok[0]) * 100_000_000)
    return int(won[0].replace(",", ""))


def _deterministic_rate_block(question: str) -> str:
    """공사 낙찰하한율 질문이면 룰엔진 값을 [확정 사실]로 만든다."""
    if not _RATE_INTENT.search(question) or not _CONSTRUCTION_HINT.search(question):
        return ""
    price = _parse_amount(question)
    if not price:
        return ""
    try:
        from backend.api.deps import get_rule_engine
        engine = get_rule_engine()
        rates = set()
        byeolpyo = None
        for rule in engine.match({"estimated_price": price, "contract_type": "construction",
                                  "construction_specialty": "general"}):
            info = engine.get_pass_score(rule, price)
            r = info.get("lower_limit_rate")
            if r is not None:
                rates.add(r)
                byeolpyo = byeolpyo or info.get("byeolpyo")
        # 룰끼리 값이 갈리면 주입하지 않는다 — 확정이 아닌 것을 확정이라 부르지 않는다.
        # (이 상태 자체가 결함이고 tests/unit/test_rate_single_source.py가 잡는다.)
        if len(rates) != 1:
            return ""
        rate = rates.pop()
        eok = price / 100_000_000
        amt = f"{eok:.1f}억원" if eok < 100 else f"{eok:,.0f}억원"
        bp = f" (적격심사 {byeolpyo})" if byeolpyo else ""
        return (f"[확정 사실 — 룰엔진 계산값, 아래 참고 자료보다 우선]\n"
                f"추정가격 {amt} 종합공사의 적격심사 낙찰하한율: "
                f"{rate * 100:.3f}%{bp}\n"
                f"※ 낙찰하한율은 금액 구간에 따라 결정되는 값이며, 검색 문서에 다른 수치가 "
                f"보이더라도 그것은 개정 전 자료일 수 있습니다. 위 값을 사용하세요.\n\n")
    except Exception:
        return ""


def _screen_context_prefix(ctx: dict | None) -> str:
    """PC 사이드 채팅에서 전달한 현재 화면 정보를 LLM 시스템 prompt prefix로 변환.

    빈 컨텍스트면 빈 문자열 반환 → 모달 AskPage 기존 동작 유지.
    """
    if not ctx or not isinstance(ctx, dict):
        return ""
    parts = []
    step = ctx.get("step")
    if step:
        parts.append(f"Step {step}")
    name = ctx.get("project_name")
    ct_raw = ctx.get("contract_type")
    price = ctx.get("estimated_price")
    if name or price:
        bits = [name] if name else []
        if price:
            try:
                eok = float(price) / 100_000_000
                bits.append(f"{eok:.1f}억" if eok < 100 else f"{int(eok)}억")
            except (TypeError, ValueError):
                pass
        if ct_raw:
            bits.append(_CT_LABEL.get(ct_raw, ct_raw))
        if bits:
            parts.append(f"분석 대상: {' · '.join(bits)}")
    sug = ctx.get("suggested_method")
    rule_id = ctx.get("suggested_rule_id")
    if sug:
        sug_text = f"AI 추천: {sug}" + (f" (rule {rule_id})" if rule_id else "")
        parts.append(sug_text)
    final = ctx.get("final_method")
    if final and final != sug:
        parts.append(f"최종 선택: {final}")
    desc = ctx.get("description")
    if desc and isinstance(desc, str) and desc.strip():
        d = desc.strip()
        parts.append(f"사업개요: {d[:200]}" + ("…" if len(d) > 200 else ""))
    is_sme = ctx.get("is_sme_competition_product")
    if is_sme is True:
        parts.append("중기간 경쟁제품: 적용 (소기업·소상공인 또는 중소기업자 제한 가능)")
    elif is_sme is False:
        parts.append("중기간 경쟁제품: 미해당")
    sme_codes = ctx.get("sme_product_codes")
    sme_names = ctx.get("sme_product_names")
    if sme_codes and isinstance(sme_codes, list):
        items = []
        for i, code in enumerate(sme_codes[:5]):
            name = sme_names[i] if sme_names and i < len(sme_names) else ""
            items.append(f"{code}{f' ({name})' if name else ''}")
        if items:
            parts.append(f"중기간 품목: {', '.join(items)}")
    add_cond = ctx.get("additional_conditions")
    if add_cond and isinstance(add_cond, dict):
        active = [k for k, v in add_cond.items() if v]
        if active:
            parts.append(f"Step2 추가 조건: {', '.join(active)}")
    laws = ctx.get("selected_law_keys")
    if laws and isinstance(laws, list):
        parts.append(f"선택 법령: {', '.join(str(x) for x in laws[:5])}" + (f" 외 {len(laws)-5}건" if len(laws) > 5 else ""))
    if not parts:
        return ""
    return "[현재 사용자 화면]\n" + "\n".join("- " + p for p in parts) + "\n\n"


def _build_context(chunks: list[dict], max_chars: int = 3000) -> str:
    LABELS = {"textbook": "📘 실무 가이드", "guide": "📋 가이드", "law": "⚖️ 법령"}
    parts: list[str] = []
    total = 0
    for c in chunks:
        stype = c.get("source_type", "textbook")
        label = LABELS.get(stype, "📄 참고")
        title = c.get("section_title", "")
        content = _strip_inline_glosses(c.get("content", ""))
        excerpt = f"[{label} — {title}]\n{content}"
        if total + len(excerpt) > max_chars:
            break
        parts.append(excerpt)
        total += len(excerpt)
    return "\n\n---\n\n".join(parts)


_EXPAND_SYSTEM = """당신은 한국 공공계약 도메인의 검색 질의 확장기입니다.
주어진 질문에 대해 검색 정확도를 높이기 위한 보조 쿼리를 생성합니다.

[규칙]
1. hyde: 질문에 답하는 50~150자 한국어 가상 답변 (자연스러운 문장체, 실제 정답일 필요 X — 검색 임베딩용)
2. variants: 같은 의도를 다른 표현·동의어·축약/풀이로 표현한 한국어 변형 3개 (각 20~50자)
3. 반드시 유효한 JSON만 출력. 추가 설명 X."""


async def _expand_query(query: str, llm: LLMProvider) -> tuple[str, list[str]]:
    """LLM이 HyDE 가상답변 + Multi-query 변형 3개를 한 번에 생성."""
    prompt = f"""질문: "{query}"

JSON으로 출력:
{{
  "hyde": "이 질문에 답변하는 50~150자 한국어 가상답변",
  "variants": ["변형1", "변형2", "변형3"]
}}"""
    try:
        resp = await llm.complete(_EXPAND_SYSTEM, prompt, json_mode=True)
        data = json.loads(resp)
        return data.get("hyde", "") or "", [v for v in (data.get("variants") or [])[:3] if v]
    except Exception:
        return "", []


# section_title 노이즈 패턴 — 인덱스에 잔존하는 무의미 청크(목차/Contents/삭제 조문/공란)
# 2026-05-31: ask sources에 0.60 score로 노출되던 무의미 청크 제거
import re as _re
_NOISE_TITLE_PATTERNS = [
    _re.compile(r"^\s*목차"),
    _re.compile(r"Contents", _re.I),
    _re.compile(r"<\s*본조\s*삭제"),
    _re.compile(r"\(\s*공란\s*\)"),
    _re.compile(r"^[ \t]*$"),
]


def _is_noise_chunk(c: dict) -> bool:
    title = (c.get("section_title", "") or "").strip()
    if not title:
        return True
    for p in _NOISE_TITLE_PATTERNS:
        if p.search(title):
            return True
    content = (c.get("content", "") or "").strip()
    if len(content) < 40:  # 너무 짧은 청크
        return True
    return False


def _dedup_diverse(chunks: list[dict], max_per_doc: int = 2) -> list[dict]:
    """chunk_id 중복 제거 + document_id 다양성 강제 + 노이즈 청크 필터.

    2026-05-26: 같은 청크 중복/한 문서 독점 보정.
    2026-05-31: 노이즈 청크 사전 제거 (목차/Contents/삭제 조문/짧은 청크).
       max_per_doc=2 유지 — 1로 줄였더니 LLM 컨텍스트 부족으로 kw recall -13%p 회귀, 복원.
    """
    seen_chunk: set[str] = set()
    seen_doc: dict[str, int] = {}
    out: list[dict] = []
    for c in chunks:
        if _is_noise_chunk(c):
            continue
        cid = c.get("chunk_id", "")
        if cid and cid in seen_chunk:
            continue
        doc = c.get("document_id", "") or "_"
        if seen_doc.get(doc, 0) >= max_per_doc:
            continue
        out.append(c)
        if cid:
            seen_chunk.add(cid)
        seen_doc[doc] = seen_doc.get(doc, 0) + 1
    return out


def _merge_search_results(results_per_query: list[list[dict]], top_k: int = 15) -> list[dict]:
    """여러 쿼리 검색 결과를 RRF로 통합."""
    K = 60.0
    score_map: dict[str, float] = {}
    chunk_map: dict[str, dict] = {}
    for chunks in results_per_query:
        for rank, c in enumerate(chunks):
            cid = c["chunk_id"]
            score_map[cid] = score_map.get(cid, 0.0) + 1.0 / (K + rank)
            if cid not in chunk_map:
                chunk_map[cid] = c
    ranked = sorted(chunk_map.values(), key=lambda c: -score_map.get(c["chunk_id"], 0))
    return ranked[:top_k]


async def _retrieve_with_expansion(query: str, rag: RAGService, llm: LLMProvider, top_k: int = 10) -> list[dict]:
    """HyDE + Multi-query 통합 검색.
    1) LLM이 가상답변 + 3변형 생성 (1회 호출)
    2) 원본·HyDE·변형 3개 = 최대 5개 쿼리로 RAG 검색
    3) RRF로 통합
    """
    hyde, variants = await _expand_query(query, llm)
    queries = [query] + ([hyde] if hyde else []) + variants
    results = []
    for q in queries:
        if not q.strip():
            continue
        results.append(rag.search_all(q, top_k=top_k))
    if not results:
        return []
    return _merge_search_results(results, top_k=top_k * 3)


@router.post("", response_model=AskResponse)
async def ask_question(
    req: AskRequest,
    rag: RAGService = Depends(get_rag_service),
    llm: LLMProvider = Depends(get_llm),
    client_ip: str = Depends(rate_limit_llm),
    access: dict = Depends(chat_access),  # 익명 2회/일 → 로그인 (2026-07-29)
) -> AskResponse:
    # 캐시 조회 — 동일 질문 즉시 응답 (rate limit 카운트 안 함)
    cached = _cache_get(req.question)
    if cached:
        return AskResponse(**cached)
    # 2026-07-24: record_llm_call은 실제 LLM 호출 직전으로 이동(아래).
    # (기존엔 여기서 선호출 → RAG 0-hit fail-closed로 LLM 미호출한 요청까지 비용/호출 계상되던 결함.)

    import time as _time
    _t0 = _time.time()
    chunks = rag.search_all(req.question, top_k=req.top_k)
    chunks = _dedup_diverse(chunks, max_per_doc=2)
    _t_search = _time.time() - _t0
    # 2026-05-26: Cohere Rerank — Dense+BM25+RRF 후보를 cross-encoder가 1:1 재평가
    from backend.services.reranker import rerank as _cohere_rerank
    _t1 = _time.time()
    chunks = _cohere_rerank(req.question, chunks, top_n=8)  # 10→8
    _t_rerank = _time.time() - _t1
    # 2026-07-30 P0: rerank 무산출이면 BM25-only 배제 + 문턱 상향(전량 배제 시 0-hit fail-closed로 합류)
    chunks, _no_rerank = _apply_rerank_guard(chunks)
    if not chunks:
        import logging
        logging.getLogger("contract_compass").warning("RAG 0-hit — LLM 호출 생략: %s", req.question[:80])
        # 2026-07-24: LLM 과금은 없지만 임베딩·검색 비용은 발생한다(RAGService._ef = Gemini 임베딩).
        # 전역 일일 캡(과금 예산)은 미차감하되 IP별 sliding window에는 계상 — 인덱스 장애로
        # 0-hit가 지속될 때 무제한 재시도가 증폭되던 구멍을 막는다.
        from backend.services.rate_limiter import get_rate_limiter as _grl
        _grl().record(client_ip)
        return AskResponse(
            answer=_NO_EVIDENCE_ANSWER, sources=[],
            timing={"search_ms": round(_t_search * 1000, 1), "rerank_ms": round(_t_rerank * 1000, 1),
                    "llm_ms": 0.0, "total_ms": round((_time.time() - _t0) * 1000, 1)},
            unverified_citations=[], avg_relevance=0.0)
    context = _build_context(chunks, max_chars=10000)
    screen_prefix = _screen_context_prefix(req.context)
    rate_block = _deterministic_rate_block(req.question)

    user_msg = f"""{screen_prefix}{rate_block}[참고 자료]
{context}

[질문]
{req.question}"""

    # LLM 실제 호출 시점에만 비용/호출 카운트 (IP별 + 전역 일일 상한). 0-hit는 여기 도달 못함.
    record_llm_call(client_ip)
    _t2 = _time.time()
    answer = await llm.complete(_SYSTEM_PROMPT, user_msg, json_mode=False)
    _t_llm = _time.time() - _t2
    timing = {
        "search_ms": round(_t_search * 1000, 1),
        "rerank_ms": round(_t_rerank * 1000, 1),
        "llm_ms": round(_t_llm * 1000, 1),
        "total_ms": round((_time.time() - _t0) * 1000, 1),
    }

    # 문턱 상수·판정은 모듈 공용(_chunk_passes) — 스트림 경로와 사본 분화 금지 (2026-07-30)
    _il_min = _INTERNAL_LAW_MIN_NO_RERANK if _no_rerank else _INTERNAL_LAW_MIN

    def _passes_filter(c: dict) -> bool:
        return _chunk_passes(c, _no_rerank)

    sources = [
        AskSource(
            chunk_id=c["chunk_id"],
            section_title=c.get("section_title", ""),
            excerpt=c.get("content", "")[:200],
            content=c.get("content", ""),
            relevance_score=c.get("relevance_score", 0.0),
            source_type=c.get("source_type", "textbook"),
            document_id=c.get("document_id", ""),
            chunk_level=c.get("chunk_level", "single"),
            matched_via=c.get("matched_via", "vector"),
            matched_question=c.get("matched_question", ""),
        )
        for c in chunks[:8]
        if _passes_filter(c)
    ][:5]
    # 기관 내규가 답변 컨텍스트에 쓰였는데 출처에서 누락되면 1건 보장(0.85+ 진짜 매칭만)
    if not any(s.source_type == "internal" for s in sources):
        _i = next((c for c in chunks if c.get("source_type") == "internal" and c.get("relevance_score", 0) >= _il_min), None)
        if _i:
            sources = sources[:4] + [AskSource(
                chunk_id=_i["chunk_id"], section_title=_i.get("section_title", ""),
                excerpt=_i.get("content", "")[:200], content=_i.get("content", ""),
                relevance_score=_i.get("relevance_score", 0.0), source_type="internal",
                document_id=_i.get("document_id", ""),
                chunk_level=_i.get("chunk_level", "single"),
                matched_via=_i.get("matched_via", "vector"),
                matched_question=_i.get("matched_question", ""))]

    # 2026-07-09: law 출처를 근거 청크 law_refs + 답변 인용 조문에 종속(환각 정정).
    # (구 _inject_law_chunks 대체 — 근거 없는 orphan 조문 제거 + 정답 조문 보강)
    sources = _ground_law_sources(answer, chunks, sources, as_dict=False)
    # 2026-07-24: '근거 없음' 답변엔 무관 출처(0.9 고신뢰 법령 등) 첨부 억제.
    if _is_no_evidence_answer(answer):
        sources = []
    unverified = _verify_citations(answer, sources)
    avg_rel = _avg_relevance(sources)
    payload = {
        "answer": answer, "sources": [s.model_dump() for s in sources], "timing": timing,
        "unverified_citations": unverified, "avg_relevance": avg_rel,
    }
    _cache_set(req.question, payload)
    return AskResponse(answer=answer, sources=sources, timing=timing,
                       unverified_citations=unverified, avg_relevance=avg_rel)


@router.post("/stream")
async def ask_stream(
    req: AskRequest,
    rag: RAGService = Depends(get_rag_service),
    llm: LLMProvider = Depends(get_llm),
    client_ip: str = Depends(rate_limit_llm),
    access: dict = Depends(chat_access),  # 익명 2회/일 → 로그인 (2026-07-29)
):
    """SSE 스트리밍 응답 — 글자 단위 실시간 출력. sources는 마지막에 전송."""
    # 캐시 hit 시 한 번에 반환 (스트리밍 불필요, rate limit 카운트 안 함)
    # 2026-07-24: record_llm_call은 생성기 내부의 0-hit 판정 이후로 이동
    # (0-hit fail-closed로 LLM 미호출한 요청이 비용/호출로 계상되던 결함 차단).
    cached = _cache_get(req.question)

    async def event_stream():
        if cached:
            yield f"data: {json.dumps({'type': 'token', 'text': cached['answer']}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'sources', 'sources': cached['sources']}, ensure_ascii=False)}\n\n"
            yield "data: {\"type\": \"done\"}\n\n"
            return

        chunks = rag.search_all(req.question, top_k=req.top_k)
        # 2026-07-09: 스트림 경로도 비스트림과 동일하게 dedup + Cohere rerank 적용.
        # (기존엔 생략돼 하드코딩 relevance 0.85 parent 조문이 무조건 통과 → 오인용 원인)
        # Cohere 미가용(폐쇄망)이면 rerank가 입력 그대로 반환 → 안전하게 degrade.
        chunks = _dedup_diverse(chunks, max_per_doc=2)
        try:
            from backend.services.reranker import rerank as _cohere_rerank
            chunks = _cohere_rerank(req.question, chunks, top_n=8)
        except Exception:
            pass
        # 2026-07-30 P0: rerank 무산출이면 BM25-only 배제 + 문턱 상향 (비스트림 경로와 동일)
        chunks, _no_rerank = _apply_rerank_guard(chunks)
        if not chunks:                       # fail-closed: 근거 0건 → LLM 생략 (비스트림 경로와 동일)
            import logging
            logging.getLogger("contract_compass").warning("RAG 0-hit(stream) — LLM 호출 생략: %s", req.question[:80])
            from backend.services.rate_limiter import get_rate_limiter as _grl
            _grl().record(client_ip)   # 과금 없는 요청도 IP 스로틀엔 계상(전역 캡 미차감)
            yield f"data: {json.dumps({'type': 'sources', 'sources': []}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'token', 'text': _NO_EVIDENCE_ANSWER}, ensure_ascii=False)}\n\n"
            yield "data: {\"type\": \"done\"}\n\n"
            return
        # 근거 확보(0-hit 통과) → 실제 LLM 스트림 직전에만 비용/호출 카운트
        record_llm_call(client_ip)
        context = _build_context(chunks, max_chars=10000)
        user_msg = f"{_screen_context_prefix(req.context)}[참고 자료]\n{context}\n\n[질문]\n{req.question}"

        # 소스 먼저 전송 (UI가 빨리 출처 박스 준비) — 문턱은 모듈 공용 _chunk_passes (2026-07-30)
        _il_min = _INTERNAL_LAW_MIN_NO_RERANK if _no_rerank else _INTERNAL_LAW_MIN

        def _pass(c: dict) -> bool:
            return _chunk_passes(c, _no_rerank)

        sources = [
            {
                "chunk_id": c["chunk_id"],
                "section_title": c.get("section_title", ""),
                "excerpt": c.get("content", "")[:200],
                "content": c.get("content", ""),
                "relevance_score": c.get("relevance_score", 0.0),
                "source_type": c.get("source_type", "textbook"),
                "document_id": c.get("document_id", ""),
                "matched_via": c.get("matched_via", "vector"),
                "matched_question": c.get("matched_question", ""),
            }
            for c in chunks[:8]
            if _pass(c)
        ][:5]
        # 기관 내규 출처 1건 보장 (0.85+ 진짜 매칭만)
        if not any(s.get("source_type") == "internal" for s in sources):
            _i = next((c for c in chunks if c.get("source_type") == "internal" and c.get("relevance_score", 0) >= _il_min), None)
            if _i:
                sources = sources[:4] + [{
                    "chunk_id": _i["chunk_id"], "section_title": _i.get("section_title", ""),
                    "excerpt": _i.get("content", "")[:200], "content": _i.get("content", ""),
                    "relevance_score": _i.get("relevance_score", 0.0), "source_type": "internal",
                    "document_id": _i.get("document_id", ""),
                }]
        # 2026-07-09: 답변 생성 전에도 근거 청크 law_refs로 law 출처 정제(orphan 조문 즉시 제거)
        sources = _ground_law_sources("", chunks, sources, as_dict=True)
        yield f"data: {json.dumps({'type': 'sources', 'sources': sources}, ensure_ascii=False)}\n\n"

        # 스트리밍 LLM 응답 — Gemini provider의 stream() 메서드 사용
        full_text = ""
        if hasattr(llm, "stream"):
            try:
                async for token in llm.stream(_SYSTEM_PROMPT, user_msg):
                    full_text += token
                    yield f"data: {json.dumps({'type': 'token', 'text': token}, ensure_ascii=False)}\n\n"
            except Exception:
                import logging
                logging.getLogger("contract_compass").exception("ask SSE stream 오류")
                _msg = "답변 생성 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
                yield f"data: {json.dumps({'type': 'error', 'message': _msg}, ensure_ascii=False)}\n\n"
                return
        else:
            # fallback: 비스트리밍
            full_text = await llm.complete(_SYSTEM_PROMPT, user_msg, json_mode=False)
            yield f"data: {json.dumps({'type': 'token', 'text': full_text}, ensure_ascii=False)}\n\n"

        # 후처리·캐시 + 환각 검증
        full_text = _strip_inline_glosses(full_text) if "_strip_inline_glosses" in globals() else full_text

        # 2026-06-01: 조문 환각 검증 (stream 응답에도 적용)
        class _SrcShim:
            def __init__(self, d):
                self.section_title = d.get("section_title", "")
                self.excerpt = d.get("excerpt", "") or d.get("content", "")[:200]
                self.relevance_score = d.get("relevance_score", 0.0)
                self.source_type = d.get("source_type", "")
                self.matched_via = d.get("matched_via", "")   # 주입 조문 평균 제외용(2026-07-24)
        src_shims = [_SrcShim(s) for s in sources]

        # 2026-07-09: 답변 본문까지 반영해 law 출처 재정제 (답변이 명시 인용한 조문 보강 포함).
        # 근거 청크 law_refs + 답변 인용 조문에만 종속 → 무관 조문 배제, 정답 조문 추가.
        try:
            # 2026-07-24: '근거 없음' 답변이면 앞서 보낸 출처를 빈 목록으로 덮어써 억제
            # (무관 법령 0.9 고신뢰 오첨부 방지 — 비스트림 경로와 동일 규약).
            grounded = [] if _is_no_evidence_answer(full_text) else _ground_law_sources(full_text, chunks, sources, as_dict=True)
            if [s.get("chunk_id") for s in grounded] != [s.get("chunk_id") for s in sources]:
                sources = grounded
                yield f"data: {json.dumps({'type': 'sources', 'sources': sources}, ensure_ascii=False)}\n\n"
                src_shims = [_SrcShim(s) for s in sources]
        except Exception:
            pass

        unverified = _verify_citations(full_text, src_shims)
        avg_rel = _avg_relevance(src_shims)
        if unverified or avg_rel is not None:
            yield f"data: {json.dumps({'type': 'verify', 'unverified_citations': unverified, 'avg_relevance': avg_rel}, ensure_ascii=False)}\n\n"

        _cache_set(req.question, {
            "answer": full_text, "sources": sources,
            "unverified_citations": unverified, "avg_relevance": avg_rel,
        })
        yield "data: {\"type\": \"done\"}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
