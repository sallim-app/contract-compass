from pathlib import Path
from functools import lru_cache

from pydantic_settings import BaseSettings

# 저장소 루트 — 모든 파일 경로의 단일 기준점(절대경로 하드코딩 금지).
BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    llm_provider: str = "openai"  # "openai" | "gemini"
    openai_api_key: str = ""
    gemini_api_key: str = ""
    # gemini-3.1-flash-lite (500 RPD 무료) | gemini-3-flash-preview (20 RPD) | gemini-3.1-pro-preview (유료) | gemini-2.5-flash
    gemini_model: str = "gemini-3.1-flash-lite"
    chroma_path: str = str(BASE_DIR / "chroma_db")
    rules_path: str = str(BASE_DIR / "rules" / "contract_rules.json")
    session_ttl_seconds: int = 3600
    # 세션 영속화(SQLite) — 재시작/다중워커에도 사용자 진행 유지. 빈 문자열이면 인메모리 폴백.
    session_db_path: str = str(BASE_DIR / "data" / "sessions.db")
    admin_token: str = ""  # 비어있으면 admin 엔드포인트 503(fail-closed) — .env에서 설정
    law_api_key: str = ""  # law.go.kr API 키
    cohere_api_key: str = ""  # Rerank API 키 — 미설정 시 rerank 미사용(무료 운영 기본)
    # 자체 GPU 서빙 연동 — 전부 미설정 시 공식 API 경로 그대로.
    openai_base_url: str = ""      # vLLM 등 OpenAI 호환 LLM 서버 (예: http://gpu:8000/v1)
    openai_model: str = "gpt-5.6-luna"
    # 전역 일일 LLM 호출 상한(서킷브레이커) — 초과 시 429(과금 방지). 0 이하면 비활성.
    openai_daily_call_cap: int = 200
    openai_daily_cap_file: str = str(BASE_DIR / "data" / "openai_daily_cap.json")
    # SDK 내장 지수 백오프 재시도(429/5xx/타임아웃)와 요청 타임아웃 — env로 조정 가능.
    openai_timeout: float = 60.0     # 단일 요청 타임아웃(초)
    openai_max_retries: int = 3      # 429/5xx/네트워크 오류 자동 재시도 횟수(SDK 지수 백오프)
    embedding_endpoint: str = ""   # TEI/Infinity 등 OpenAI 호환 임베딩 서버 (예: http://gpu:8080/v1)
    embedding_model: str = ""      # 빈 값이면 embedding.py 기본(MiniLM). 교체 시 전 컬렉션 재임베딩 필수
    rerank_endpoint: str = ""      # TEI reranker 서버 — 지정 시 Cohere 대신 사용
    gemini_rpm_limit: int = 12   # Free tier 안전 마진 (실한도 15)
    gemini_rpd_limit: int = 400  # Free tier 안전 마진 (flash-lite 실한도 500)
    # RAG 컬렉션명 — 공개 코퍼스 3원 체제 (P5에서 재인덱싱 시 사용)
    collection_law_articles: str = "law_articles"
    collection_admin_rules: str = "admin_rules"
    collection_public_guides: str = "public_guides"
    collection_faq: str = "faq"
    collection_doc2query: str = "doc2query"  # 청크별 가상질문 — 실무 어휘↔법령 어휘 브리지
    cors_origins: list[str] = [
        "http://localhost:3000", "http://localhost:5173",
    ]
    # 채팅(ask) 접근 게이팅 (2026-07-29): 익명은 IP당 1일 chat_free_daily회 무료,
    # 이후 Supabase(GoTrue) Google 로그인 필요. 비어있으면 로그인 검증 불가 →
    # 익명 무료 한도만 적용되고 로그인 요구 시 503 (fail-closed, admin_token과 동일 철학).
    chat_free_daily: int = 2
    chat_quota_file: str = str(BASE_DIR / "data" / "chat_anon_quota.json")

    class Config:
        env_file = str(BASE_DIR / ".env")
        env_file_encoding = "utf-8"
        # .env에 백엔드 미정의 키가 있어도 기동 실패하지 않도록 무시.
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    return Settings()
