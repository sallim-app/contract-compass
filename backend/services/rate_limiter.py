"""자체 per-IP rate limiter — SQLite(WAL) 영속, 다중 워커 공유 (외부 서비스 의존 0).

LLM 호출 비용·시간 보호 — Cohere rerank·Gemini complete 호출당 3~10s + API 비용.
무제한 호출 시 비용·서버 부하 노출되므로 IP별 sliding window 제한.

한도 (LLM 엔드포인트):
  - 1분 10회 / 1시간 100회 / 1일 500회
특징:
  - sliding window (정확한 윈도우 기준)
  - 캐시 hit는 caller가 record() 안 부르면 카운트 제외
  - 화이트리스트 ENV (RATE_LIMIT_WHITELIST="127.0.0.1,...")
  - FastAPI Dependency, 초과 시 HTTPException(429)
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from fastapi import HTTPException, Request, status

from backend.config import BASE_DIR

logger = logging.getLogger(__name__)

LIMITS_LLM = {"minute": 10, "hour": 100, "day": 500}
WINDOWS = {"minute": 60, "hour": 3600, "day": 86400}

#: 루프백 = 내부 트래픽(MCP 서버·야간 QA 등 localhost 직결). nginx 경유 외부 요청은
#: 항상 XFF에 실 IP가 실리므로 여기 해당하지 않는다 (_get_raw_ip가 XFF 루프백 스푸핑 차단).
_LOOPBACK_IPS = {"127.0.0.1", "::1", "localhost"}

_WHITELIST = set(
    ip.strip()
    for ip in (os.environ.get("RATE_LIMIT_WHITELIST", "127.0.0.1") or "").split(",")
    if ip.strip()
)


class RateLimiter:
    """IP별 sliding window — SQLite(WAL) 영속 (2026-07-30 P1: 다중 워커 전환).

    기존 인메모리(deque) 구현은 워커 간 상태가 갈라져 workers=1을 강제했다.
    카운터를 SQLite 파일로 옮겨 여러 uvicorn 워커가 같은 한도를 공유한다.
    - WAL + busy_timeout으로 동시 접근 안전. 커넥션은 스레드로컬(FastAPI 스레드풀 대응).
    - 저장소 장애는 fail-open(요청 허용 + warning) — 한도는 보호 장치이지 기능이 아님.
    - 만료 행 정리는 record() 시 수행(일 윈도우 밖 삭제, 인덱스 타서 저렴).
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or str(BASE_DIR / "data" / "rate_limiter.db")
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self._connect() as c:
            c.execute("CREATE TABLE IF NOT EXISTS llm_hits (ip TEXT NOT NULL, ts REAL NOT NULL)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_hits_ip_ts ON llm_hits(ip, ts)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_hits_ts ON llm_hits(ts)")
            c.execute("CREATE TABLE IF NOT EXISTS blocked (ip TEXT PRIMARY KEY, count INTEGER NOT NULL)")

    def _connect(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._db_path, timeout=3.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=3000")
            self._local.conn = conn
        return conn

    def _get_raw_ip(self, request: Request) -> str:
        """레이트리밋 주체 IP. **X-Real-IP만 신뢰한다.**

        2026-08-04 보안 감사 정정 — 기존 구현은 "XFF의 첫 **비루프백** 항목"을 썼다.
        루프백 위조는 막았지만 **비루프백 값도 똑같이 클라이언트가 정한다**:
        `X-Forwarded-For: 8.8.8.8`을 매 요청 바꿔 보내면 버킷이 매번 새로 생겨
        10/분·100/시·500/일이 한 번도 발동하지 않는다.

        nginx는 `X-Real-IP $remote_addr`로 이 헤더를 덮어쓰고, 같은 감사에서
        `set_real_ip_from`(CF 대역)+`real_ip_header CF-Connecting-IP`를 넣어
        `$remote_addr`가 실 클라이언트가 되게 했다 — 이제 위조 불가한 값은 이것뿐이다.
        """
        v = request.headers.get("x-real-ip")
        if v:
            return v.strip()
        if request.client:
            return request.client.host
        return "unknown"

    def check(self, request: Request, limits: dict[str, int] = LIMITS_LLM) -> str:
        ip = self._get_raw_ip(request)
        if ip in _WHITELIST:
            return ip
        now = time.time()
        try:
            c = self._connect()
            for key, max_calls in limits.items():
                window_s = WINDOWS[key]
                cur = c.execute(
                    "SELECT COUNT(*) FROM llm_hits WHERE ip = ? AND ts >= ?",
                    (ip, now - window_s)).fetchone()[0]
                if cur >= max_calls:
                    with c:
                        c.execute(
                            "INSERT INTO blocked(ip, count) VALUES(?, 1) "
                            "ON CONFLICT(ip) DO UPDATE SET count = count + 1", (ip,))
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail={
                            "error": "rate_limit_exceeded",
                            "window": key, "limit": max_calls, "current": cur,
                            "retry_after": window_s,
                        },
                        headers={"Retry-After": str(window_s)},
                    )
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001 — 저장소 장애는 fail-open(한도는 보호 장치)
            logger.warning("rate limiter 저장소 조회 실패 — fail-open", exc_info=True)
        return ip

    def record(self, ip: str) -> None:
        if ip in _WHITELIST:
            return
        now = time.time()
        try:
            c = self._connect()
            with c:
                c.execute("INSERT INTO llm_hits(ip, ts) VALUES(?, ?)", (ip, now))
                # 만료 행 정리 — 최대 윈도우(일) 밖은 어떤 한도 계산에도 안 쓰인다
                c.execute("DELETE FROM llm_hits WHERE ts < ?", (now - max(WINDOWS.values()),))
        except Exception:  # noqa: BLE001
            logger.warning("rate limiter 기록 실패 — 카운트 유실(soft)", exc_info=True)

    def stats(self) -> dict:
        now = time.time()
        try:
            c = self._connect()
            tracked = c.execute("SELECT COUNT(DISTINCT ip) FROM llm_hits").fetchone()[0]
            active = c.execute(
                "SELECT COUNT(DISTINCT ip) FROM llm_hits WHERE ts >= ?",
                (now - WINDOWS["hour"],)).fetchone()[0]
            blocked = dict(c.execute("SELECT ip, count FROM blocked").fetchall())
            return {
                "tracked_ips": tracked,
                "blocked_total": sum(blocked.values()),
                "blocked_per_ip": blocked,
                "active_last_hour": active,
            }
        except Exception:  # noqa: BLE001
            return {"tracked_ips": 0, "blocked_total": 0, "blocked_per_ip": {}, "active_last_hour": 0}


def _send_telegram_alert(text: str) -> None:
    """운영자 텔레그램 경보 발신 (외부 의존 0 — 표준 urllib).

    - env `TELEGRAM_BOT_TOKEN`·`TELEGRAM_CHAT_ID` 둘 다 있어야 발송, 없으면 조용히 스킵.
    - 발송 실패(네트워크·4xx 등)는 절대 요청 처리에 전파하지 않음 — 삼키고 warning 1줄.
    - 동기 호출 + 3s 타임아웃: 하루 최대 2회(80%·100%)라 요청 지연 영향 미미.
    """
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        logger.debug("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID 미설정 — 비용가드 경보 스킵")
        return
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3):
            pass
    except Exception:  # noqa: BLE001 — 경보 실패가 본 요청을 죽이면 안 됨
        logger.warning("텔레그램 비용가드 경보 발송 실패 (요청 처리엔 무영향)")


class DailyCallCap:
    """전역 일일 호출 상한(서킷브레이커) — 유료 LLM 비용 폭주 방지.

    IP당 한도(RateLimiter)와 별개로 **모든 LLM 경로 합산** 일일 상한을 강제한다.
    - 날짜별 영속 카운터(파일): 프로세스 재시작에도 유지 (UTC 날짜 기준, 날짜 바뀌면 자동 리셋).
    - 상한 초과 시 과금 유발 대신 우아하게 429 반환(앱 크래시 금지).
    - 카운터 조회 실패(파일 손상·권한 등)가 정상 요청을 막지 않도록 방어(fail-open on read).
    - 화이트리스트 무관하게 전역 예산으로 카운트(비용은 IP를 가리지 않음).
    - 운영자 경보(2026-07-18): 캡의 80%·100%를 처음 넘는 순간 텔레그램 1회씩 발송.
      발송 여부는 카운터 파일 `alerted` 필드에 마킹 — 같은 날 중복 발송 없음, 날짜 리셋과 함께 리셋.

    주의(멀티워커): 파일 잠금은 프로세스 내 Lock만 — 다중 uvicorn 워커 동시 증가 시 미세한
    경쟁으로 실제 카운트가 상한을 소폭 초과할 수 있음(soft cap). 비용 폭주 방지 목적엔 충분.
    """

    #: 텔레그램 경보 임계(캡 대비 %). 각 임계는 하루 1회만 발송.
    ALERT_THRESHOLDS_PCT: tuple[int, ...] = (80, 100)

    def __init__(self, cap: int, path: str, label: str = "공개") -> None:
        self._cap = cap
        self._path = Path(path)
        self._lock = Lock()
        self._label = label

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    @staticmethod
    def _seconds_to_reset() -> int:
        """UTC 자정(카운터 리셋 시각 = KST 09:00)까지 남은 초."""
        now = datetime.now(timezone.utc)
        return max(60, 86400 - (now.hour * 3600 + now.minute * 60 + now.second))

    def _read(self) -> tuple[str, int]:
        data = json.loads(self._path.read_text(encoding="utf-8"))
        return str(data.get("date", "")), int(data.get("count", 0))

    def _read_alerted(self) -> list[int]:
        """오늘자 파일에 기록된 발송 완료 임계(%) 목록. 실패·과거 날짜 → []."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if str(data.get("date", "")) != self._today():
                return []
            return [int(x) for x in data.get("alerted", [])]
        except Exception:  # noqa: BLE001 — 손상/없음은 미발송으로 간주
            return []

    def _write(self, date: str, count: int, alerted: list[int] | None = None) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload: dict = {"date": date, "count": count}
            if alerted:
                payload["alerted"] = sorted(alerted)
            self._path.write_text(json.dumps(payload), encoding="utf-8")
        except Exception:  # noqa: BLE001 — 영속 실패가 요청을 막지 않음
            pass

    def _notify(self, pct: int, count: int, date: str) -> None:
        """임계 도달 텔레그램 경보 — 어떤 예외도 record() 경로로 전파 금지."""
        icon = "🚨" if pct >= 100 else "⚠️"
        text = (
            f"{icon} 계약나침반 LLM 비용가드[{self._label}]: 오늘 OpenAI 호출 "
            f"{count}/{self._cap} ({pct}%) 도달 — {date}"
        )
        try:
            _send_telegram_alert(text)
        except Exception:  # noqa: BLE001 — 경보는 부수 기능, 본 요청 보호가 우선
            logger.warning("비용가드 경보 처리 중 예외 (요청 처리엔 무영향)")

    def current(self) -> int:
        """오늘(UTC) 누적 호출 수. 파일 없음/손상/날짜 불일치 → 0."""
        try:
            date, count = self._read()
        except Exception:  # noqa: BLE001 — 조회 실패는 0으로 간주(요청 허용)
            return 0
        return count if date == self._today() else 0

    def exhausted(self) -> bool:
        """오늘 예산 소진 여부. cap<=0(비활성)·조회 실패는 False(fail-open)."""
        if self._cap <= 0:
            return False
        try:
            return self.current() >= self._cap
        except Exception:  # noqa: BLE001 — 방어: 카운터 조회 실패가 정상요청을 막지 않음
            return False

    def check(self) -> None:
        """상한 초과 시 429. cap<=0이면 비활성(무제한). 조회 실패는 통과(fail-open)."""
        if self.exhausted():
            retry = self._seconds_to_reset()
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": "daily_cap_exceeded",
                    "message": "오늘의 AI 이용량을 모두 사용했습니다. "
                               "매일 오전 9시(한국시간)에 초기화됩니다.",
                    "limit": self._cap,
                    "retry_after": retry,
                },
                headers={"Retry-After": str(retry)},
            )

    def record(self) -> int:
        """실제 LLM 호출 1건 반영(원자적 read-modify-write). 날짜 넘어가면 리셋 후 1.

        임계(80%·100%)를 처음 넘는 호출이면 발송 마킹 후 텔레그램 경보 1회 발송.
        마킹을 먼저 영속(락 안)하고 발송은 락 밖에서 — 중복 발송 방지 + 락 점유 최소화.
        """
        pending_alerts: list[int] = []
        with self._lock:
            date, count = self._today(), 0
            alerted: list[int] = []
            try:
                pdate, pcount = self._read()
                if pdate == date:
                    count = pcount
                    alerted = self._read_alerted()
            except Exception:  # noqa: BLE001 — 파일 없음/손상 시 오늘 0부터
                pass
            count += 1
            if self._cap > 0:
                for pct in self.ALERT_THRESHOLDS_PCT:
                    if pct not in alerted and count * 100 >= self._cap * pct:
                        alerted.append(pct)
                        pending_alerts.append(pct)
            self._write(date, count, alerted)
        for pct in pending_alerts:
            self._notify(pct, count, date)
        return count


_limiter: RateLimiter | None = None
_daily_caps: dict[bool, DailyCallCap] = {}

# 기본 상한·카운터 경로 (env로 조정). 카운터는 data/ 아래 날짜별 JSON.
_DEFAULT_CAP_FILE = str(BASE_DIR / "data" / "openai_daily_cap.json")
# 내부(루프백) 트래픽 전용 카운터 — MCP·야간 QA가 공개 예산을 소진해 실사용자를
# 막던 사고(2026-07-29, 228호출) 재발 방지. env INTERNAL_LLM_DAILY_CALL_CAP로 조정.
_DEFAULT_INTERNAL_CAP = 300
_DEFAULT_INTERNAL_CAP_FILE = str(BASE_DIR / "data" / "openai_daily_cap_internal.json")


def get_rate_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter()
    return _limiter


def is_internal_ip(ip: str) -> bool:
    """루프백 = 내부 자동화(MCP·QA). nginx 경유 외부는 XFF 실 IP라 해당 없음."""
    return ip in _LOOPBACK_IPS


def get_daily_cap(internal: bool = False) -> DailyCallCap:
    cached = _daily_caps.get(internal)
    if cached is not None:
        return cached
    if internal:
        try:
            cap = int(os.environ.get("INTERNAL_LLM_DAILY_CALL_CAP", _DEFAULT_INTERNAL_CAP))
        except (TypeError, ValueError):
            cap = _DEFAULT_INTERNAL_CAP
        inst = DailyCallCap(cap, _DEFAULT_INTERNAL_CAP_FILE, label="내부")
    else:
        cap, path = 500, _DEFAULT_CAP_FILE
        # 우선순위: 명시 env(테스트·운영 오버라이드) → 앱 설정(.env, pydantic) → 기본값.
        env_cap = os.environ.get("OPENAI_DAILY_CALL_CAP")
        env_path = os.environ.get("OPENAI_DAILY_CAP_FILE")
        if env_cap is not None or env_path is not None:
            try:
                cap = int(env_cap) if env_cap is not None else cap
            except (TypeError, ValueError):
                pass
            path = env_path or path
        else:
            try:
                from backend.config import get_settings
                s = get_settings()
                cap = s.openai_daily_call_cap
                path = s.openai_daily_cap_file or path
            except Exception:  # noqa: BLE001 — 설정 로드 실패 시 안전 기본값
                pass
        inst = DailyCallCap(cap, path, label="공개")
    _daily_caps[internal] = inst
    return inst


def rate_limit_llm(request: Request) -> str:
    """FastAPI Dependency — LLM이 필수인 경로(ask·classify) 공용.

    IP당 한도(sliding window) + 스코프별(공개/내부) 일일 상한 둘 다 검사.
    통과 시 raw IP 반환 — 실제 LLM 호출(캐시 미스) 시점에 caller가 record_llm_call(ip) 호출.
    """
    ip = get_rate_limiter().check(request, LIMITS_LLM)
    get_daily_cap(internal=is_internal_ip(ip)).check()
    return ip


def rate_limit_llm_soft(request: Request) -> tuple[str, bool]:
    """FastAPI Dependency — 결정론 응답이 가능한 경로(step1·step2) 전용.

    IP당 한도는 동일하게 강제하되, 일일 캡 소진은 429 대신 (ip, llm_allowed=False)로
    신호만 보낸다 — 룰엔진 판정은 LLM 없이도 유효하므로 캡이 결정론 기능까지 죽이면
    안 된다(2026-07-30, 캡 소진 시 step1 전면 429 나던 결함 수정).
    """
    ip = get_rate_limiter().check(request, LIMITS_LLM)
    return ip, not get_daily_cap(internal=is_internal_ip(ip)).exhausted()


def record_llm_call(ip: str) -> None:
    """실제 LLM 호출 1건 기록 — IP별 카운터 + 스코프별(공개/내부) 일일 카운터 반영.

    캐시 히트는 caller가 이 함수를 부르지 않으므로 카운트 제외(과금 없는 호출은 예산 미차감).
    """
    get_rate_limiter().record(ip)
    get_daily_cap(internal=is_internal_ip(ip)).record()
