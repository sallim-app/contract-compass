"""세션 스토어 — SQLite 영속(기본) 또는 인메모리(폴백). TTL 1시간.

운영 하드닝(B3): 인메모리는 프로세스 재시작 시 사용자 진행(step1→step2→form)이 유실된다.
SQLite로 영속화해 재시작에도 세션을 유지한다. 같은 DB 파일을 공유하면 다중 워커에서도
일관(추후 실서버 다중워커 배포 대비). 인터페이스는 기존과 동일(create/get/set/exists).
"""
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any


class SessionStore:
    def __init__(self, ttl_seconds: int = 3600, db_path: str | None = None):
        self._ttl = ttl_seconds
        self._db_path = db_path
        self._lock = threading.Lock()
        if db_path:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            # check_same_thread=False: FastAPI 스레드풀에서 공유. 쓰기는 _lock으로 직렬화.
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")  # 동시 읽기 + 쓰기 안정성
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS sessions ("
                "session_id TEXT PRIMARY KEY, created_at REAL, data TEXT)"
            )
            self._conn.commit()
            self._store = None
            # (2026-08-15) 만료행 일괄 청소 — 종전엔 "그 세션을 다시 읽을 때만" 지워서
            # 접근되지 않은 세션(사업명·사업개요 포함)이 무기한 잔존했다(실측 3,231행 전부
            # 만료 상태). 기동 시 1회 + 세션 생성 때마다 쓸어, 개인정보처리방침의
            # "위저드 입력 24시간 내 파기" 문구가 실제와 일치하게 한다.
            self._db_sweep()
        else:
            self._conn = None
            self._store = {}

    def _db_sweep(self) -> None:
        """TTL 지난 세션 전부 삭제(수천 행 수준이라 밀리초 단위)."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM sessions WHERE created_at < ?", (time.time() - self._ttl,)
            )
            self._conn.commit()

    # ── 영속(SQLite) 경로 ───────────────────────────────────────
    def _db_get(self, session_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT created_at, data FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if row is None:
                return None
            created_at, data = row
            if time.time() - created_at > self._ttl:
                self._conn.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
                self._conn.commit()
                return None
            return json.loads(data)

    def _db_create(self) -> str:
        self._db_sweep()  # 새 세션이 생길 때마다 만료분 청소 — 사용량에 비례해 자연 유지
        session_id = str(uuid.uuid4())
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions(session_id, created_at, data) VALUES (?,?,?)",
                (session_id, time.time(), "{}"),
            )
            self._conn.commit()
        return session_id

    def _db_set(self, session_id: str, key: str, value: Any):
        with self._lock:
            row = self._conn.execute(
                "SELECT created_at, data FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            created_at = row[0] if row else time.time()
            data = json.loads(row[1]) if row else {}
            data[key] = value
            self._conn.execute(
                "INSERT INTO sessions(session_id, created_at, data) VALUES (?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET data=excluded.data",
                (session_id, created_at, json.dumps(data, ensure_ascii=False)),
            )
            self._conn.commit()

    # ── 공개 인터페이스 (기존과 동일) ───────────────────────────
    def create(self) -> str:
        if self._conn is not None:
            return self._db_create()
        session_id = str(uuid.uuid4())
        self._store[session_id] = {"created_at": time.time(), "data": {}}
        return session_id

    def get(self, session_id: str) -> dict | None:
        if self._conn is not None:
            return self._db_get(session_id)
        entry = self._store.get(session_id)
        if entry is None:
            return None
        if time.time() - entry["created_at"] > self._ttl:
            del self._store[session_id]
            return None
        return entry["data"]

    def set(self, session_id: str, key: str, value: Any):
        if self._conn is not None:
            return self._db_set(session_id, key, value)
        if session_id not in self._store:
            self._store[session_id] = {"created_at": time.time(), "data": {}}
        self._store[session_id]["data"][key] = value

    def exists(self, session_id: str) -> bool:
        return self.get(session_id) is not None
