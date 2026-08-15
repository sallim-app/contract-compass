"""메모리 구조 수리 회귀 (2026-08-15, plans/mcp-replicated-meteor.md).

배경: 요청마다 law_articles 전량 물질화(rag_service)·전 컬렉션 전수 스캔(status)이
workers=1 장수 프로세스에서 RSS +1.0GB/7h 성장의 주범이었다(전역 OOM 실증 09:14).
이 가드가 깨지면 "요청당 전량 get"이 부활한 것이다.
"""
import time

from backend.services.rag_service import RAGService
from backend.services.session_store import SessionStore


class _FakeLawCol:
    """law_articles 흉내 — get() 호출 횟수로 '요청당 전량 조회' 부활을 감지한다."""

    def __init__(self):
        self.get_calls = 0

    def count(self):
        return 3

    def get(self, include=None):  # noqa: ARG002
        self.get_calls += 1
        return {
            "ids": ["a", "b", "c"],
            "documents": ["국가 본문", "지방 본문", "기타 본문"],
            "metadatas": [
                {"law_name": "국가계약법 시행령", "law_ref": "시행령 제26조"},
                {"law_name": "지방계약법", "law_ref": "법 제9조"},
                {"law_name": "건설기술 진흥법", "law_ref": "제1조"},
            ],
        }


def test_kw_boost_cache_builds_once_and_sorts_both_modes():
    col = _FakeLawCol()
    rs = RAGService.__new__(RAGService)  # __init__(chroma 연결) 우회
    rs._kw_boost_cache = {}

    national = rs._kw_boost_items(col, local=False)
    local = rs._kw_boost_items(col, local=True)
    rs._kw_boost_items(col, local=False)

    # 전량 get은 캐시 구축 1회뿐 — 요청 경로에서 반복되면 수리가 무효화된 것
    assert col.get_calls == 1
    # 정렬 규약: 국가 모드는 시행령 최우선, 지자체 모드는 지방계약법 최우선
    assert national[0]["law_name"] == "국가계약법 시행령"
    assert local[0]["law_name"] == "지방계약법"
    # count 변화 시 재구축
    col.count = lambda: 4
    rs._kw_boost_items(col, local=False)
    assert col.get_calls == 2


def test_session_store_sweeps_expired_rows(tmp_path):
    db = str(tmp_path / "sessions.db")
    store = SessionStore(ttl_seconds=1, db_path=db)
    sid = store.create()
    assert store.exists(sid)
    time.sleep(1.2)
    # 새 세션 생성이 만료행을 쓸어낸다 — lazy 삭제(접근 시에만)로 돌아가면 실패
    store.create()
    n = store._conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE session_id=?", (sid,)
    ).fetchone()[0]
    assert n == 0, "만료 세션이 sweep되지 않았다(사업명·개요 무기한 잔존 재발)"


def test_status_scans_are_cached_by_corpus_mtime():
    from backend.api.v1 import status as st

    st._chunk_counts_at.cache_clear()
    st._topic_stats_at.cache_clear()
    a = st._chunk_counts_at(123.0)
    b = st._chunk_counts_at(123.0)
    assert a is b, "같은 코퍼스 mtime인데 재계산 — 요청당 전수 스캔 부활"
    t1 = st._topic_stats_at(123.0)
    t2 = st._topic_stats_at(123.0)
    assert t1 is t2
