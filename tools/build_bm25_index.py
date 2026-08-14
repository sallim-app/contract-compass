"""BM25 인덱스 구축 — Dense + BM25 하이브리드 검색용.

모든 청크를 토큰화해서 BM25Okapi 인덱스 구축 후 pickle 저장.
RAG 검색 시 dense(임베딩) + BM25(키워드) RRF 결합.

토큰화: 한글 단어 + 영문 + 숫자 패턴 (KoNLPy 없이 정규식 기반 — 도메인 용어 보존)

⚠️ **코퍼스를 바꾼 뒤에는 백엔드를 재시작해야 한다**(2026-08-14 실측): 실행 중인
   uvicorn이 이전 BM25 pickle과 chroma 핸들을 물고 있어, 삭제된 청크 id를 참조하다
   검색 경로가 500을 낸다(회귀 2건이 backend_error로 떨어져 발견). 절차:
   재색인 → BM25 재구축 → `sudo systemctl restart contract-compass` → 회귀 확인.
"""
import pickle
import re
import sys
from pathlib import Path

import chromadb
from rank_bm25 import BM25Okapi

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config import get_settings  # noqa: E402

_settings = get_settings()
CHROMA_PATH = _settings.chroma_path
INDEX_PATH = Path(CHROMA_PATH) / "bm25_index.pkl"
# 공개 코퍼스 3원 체제(법령·행정규칙·공개 간행물).
# faq는 제외(2026-07-29): public_guides 본문 그대로의 복제라 BM25 top-10 슬롯을
# 자기복제가 잠식했음 — faq는 dense 전용으로 유지.
TARGET_COLLECTIONS = [
    _settings.collection_public_guides,
    _settings.collection_admin_rules,
    _settings.collection_law_articles,
]

# 한글 명사/단어 + 영문 + 숫자. 조사·어미 제거 위해 단어 단위로
_TOKEN_RE = re.compile(r"[가-힣]{2,}|[A-Za-z]{2,}|\d+")
# 자주 등장하는 조사·어미 제거 (단순 휴리스틱)
_STOPWORDS = {
    "이다", "하다", "있다", "없다", "되다", "그것", "이것", "저것",
    "그리고", "하지만", "그러나", "또한", "또는", "따라", "위해",
    "대한", "관한", "에서", "에게", "으로", "라고", "이라", "한다",
    "있는", "없는", "되는", "하는", "같은", "다른", "모든", "각각",
}


def tokenize(text: str) -> list[str]:
    tokens = _TOKEN_RE.findall(text or "")
    return [t for t in tokens if t not in _STOPWORDS and len(t) >= 2]


def main() -> int:
    client = chromadb.PersistentClient(path=CHROMA_PATH)

    # 모든 컬렉션 청크를 한 BM25 인덱스로 통합 (검색 시 컬렉션 구분 메타로 추적)
    all_corpus: list[list[str]] = []
    all_ids: list[str] = []
    all_meta: list[dict] = []  # {chunk_id, collection}

    for cname in TARGET_COLLECTIONS:
        try:
            col = client.get_collection(cname)
        except Exception:
            print(f"⚠️  {cname} 없음")
            continue
        r = col.get(include=["documents"])
        ids = r.get("ids") or []
        docs = r.get("documents") or []
        for cid, doc in zip(ids, docs):
            if not doc or len(doc.strip()) < 50:
                continue
            tokens = tokenize(doc)
            if not tokens:
                continue
            all_corpus.append(tokens)
            all_ids.append(cid)
            all_meta.append({"chunk_id": cid, "collection": cname})
        print(f"  [{cname}] 청크 {len(ids)}개 → 인덱스 {sum(1 for m in all_meta if m['collection']==cname)}개")

    print(f"\n총 {len(all_corpus)} 청크 BM25 인덱싱 중...")
    bm25 = BM25Okapi(all_corpus)
    print(f"  완료. 어휘 크기: {len(bm25.idf)}")

    # 저장
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(INDEX_PATH, "wb") as f:
        pickle.dump({
            "bm25": bm25,
            "ids": all_ids,
            "meta": all_meta,
        }, f)
    size_mb = INDEX_PATH.stat().st_size / (1024 * 1024)
    print(f"  저장: {INDEX_PATH} ({size_mb:.1f}MB)")

    # 샘플 검증
    print("\n=== 샘플 검색 — '수의계약 가능한 경우' ===")
    q = tokenize("수의계약 가능한 경우 1인 견적")
    scores = bm25.get_scores(q)
    top = scores.argsort()[-5:][::-1]
    for idx in top:
        print(f"  score={scores[idx]:.2f} | [{all_meta[idx]['collection']}] {all_ids[idx]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
