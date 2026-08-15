"""contract-mcp 키 인증·티어·일일 쿼터 (2026-07-30 — realty-mcp auth.py 이식).

구조: 키 없는 호출 = free 티어(IP당 일한도) / `cc_live_*` 키 = paid 티어(키당 일한도).
키 저장은 data/mcp_keys.json — sha256 해시만 저장, 평문은 발급(issue_key.py) 1회만 노출.
SDK 내장 auth층은 OAuth 전제로 전송층 401을 강제해 "키 없는 무료 티어"와 양립하지
않는다 — 그래서 미들웨어(server.py QuotaGate)에서 tools/call 단위로 게이트한다.

IP 신뢰 순서는 **x-real-ip > cf-connecting-ip > xff 첫 항목**이다(2026-08-07 정정,
T-2026W32-105). 첫 이식본은 cf 우선이었는데 이 토폴로지에서 그게 틀렸다 —
`client_ip()` 헤더 주석에 근거를 적어 뒀다. (같은 순서를 먼저 적용했던
backend/services/chat_access.py는 웹 Q&A 폐지로 삭제됨 — D-2026W33-22.)
free 티어 subject는 원문 IP가 아니라 `sha256(salt|IP)[:12]`다(D-2026W32-33 계측 규약:
집계 산출물·상태파일에 원문 IP·UA 미저장).
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import secrets
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parents[1]
KEYS_PATH = Path(os.environ.get("CONTRACT_MCP_KEYS_FILE", str(_ROOT / "data" / "mcp_keys.json")))
FREE_DAILY = int(os.environ.get("CONTRACT_MCP_FREE_DAILY", "50"))
PAID_DAILY_DEFAULT = int(os.environ.get("CONTRACT_MCP_PAID_DAILY", "2000"))

# 구매·갱신 안내의 단일 진실원 — 한도·키 거부 메시지가 이 주소를 가리킨다.
PRICING_URL = os.environ.get("CONTRACT_MCP_PRICING_URL", "https://contract.sallim.app/mcp/pricing")

# 루프백 = 운영자 로컬·야간 QA·codexw 하네스. 무료 쿼터를 태우면 회귀가 스스로
# 막히므로 무제한. 외부 트래픽은 전부 nginx 경유라 x-real-ip가 실IP로 덮인다.
UNLIMITED_IPS = {"127.0.0.1", "::1"}

# 우리 서버 공인 IP — naru / quant. subject를 해시로 바꾸면 집계기가 주소 형태로
# 내외부를 판정할 수 없으므로, **해시 전 원문 IP로 여기서 판정해 결과(is_internal)만
# 남긴다** (/data/ops/mcp_growth.py의 OURS와 같은 집합).
OURS = {"168.107.47.60", "152.69.232.84"}


# ------------------------------------------------- free 티어 subject 해시
# (stay-mcp access.py → realty-mcp auth.py 이식, 2026-08-07 T-2026W32-105)

SALT_FILE = Path(os.environ.get(
    "CONTRACT_MCP_SUBJECT_SALT", str(_ROOT / "logs" / ".subject_salt")))

_salt_cache: Optional[str] = None
_salt_lock = threading.Lock()


def _salt() -> Optional[str]:
    """호출자 해시용 salt. 없거나 비었으면 1회 생성(600).

    **프로세스 메모리에만 두는 임의값으로 폴백하지 않는다** — 재시작마다 salt가 바뀌면
    같은 사람이 매번 새 subject가 되어 순호출자가 재시작 횟수만큼 부풀려진다.
    파일을 못 쓰면 salt 없음(None)을 돌려주고, 호출자는 그 호출을 **미상**으로 남긴다.
    빈 파일을 조용히 '없음'으로 넘기지 않는 이유도 같다 — 조용히 계측이 꺼진다.
    """
    global _salt_cache
    if _salt_cache:
        return _salt_cache
    with _salt_lock:
        if _salt_cache:
            return _salt_cache
        try:
            v = SALT_FILE.read_text(encoding="utf-8").strip() if SALT_FILE.exists() else ""
            if not v:
                v = secrets.token_hex(16)
                SALT_FILE.parent.mkdir(parents=True, exist_ok=True)
                SALT_FILE.write_text(v, encoding="utf-8")
            # 기존 파일도 매번 권한을 맞춘다(생성 때만 하면 느슨한 파일이 그대로 남는다).
            SALT_FILE.chmod(0o600)
            _salt_cache = v
        except OSError:
            traceback.print_exc()   # 조용히 꺼지지 않게 — 계측 실패는 로그에 남긴다
            return None
    return _salt_cache


def subject_hash(ip: str) -> Optional[str]:
    """`sha256(salt|IP)[:12]`. **UA를 식별자에 넣지 않는다** — UA만 바꿔 가며 호출하면
    한 사람이 순호출자 여럿이 되어 GROWTH.md 게이트 분모를 부풀린다(stay-mcp R25).
    NAT 뒤 여러 사람이 1명으로 뭉치는 과소계상은 감수한다 — 게이트에서 안전한 방향이다.
    """
    salt = _salt()
    if not salt:
        return None
    return hashlib.sha256(f"{salt}|{ip}".encode()).hexdigest()[:12]


def classify_ip(ip: str) -> str:
    """loopback | private | ours | external | unknown — 판정 근거를 원문 없이 남기려는 것."""
    if ip in UNLIMITED_IPS:
        return "loopback"
    if ip in OURS:
        return "ours"
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return "unknown"
    if addr.is_loopback:
        return "loopback"
    if addr.is_private or addr.is_reserved:
        return "private"
    return "external"


# ------------------------------------------------- UA 굵은 분류 (D-2026W32-33)
# 원문 User-Agent는 레코드에 남기지 않는다(계측 규약). 그렇다고 UA를 통째로 버리면
# 봇·크롤러가 사람과 같은 얼굴로 분모에 섞인다 — 그래서 **판별력만 남기고 원문은 버린다**.
# subject(사람 수 식별자)에는 절대 넣지 않는다: UA만 바꿔 호출하면 한 사람이 순호출자
# 여럿이 되어 게이트를 거짓 통과시킨다(stay-mcp R25 실증 → D-2026W32-33).
# 순서가 규칙이다 — "bot"을 먼저 본다. `Mozilla/5.0 (compatible; SomeBot/2.1)`처럼
# 봇이 브라우저를 자칭하는 경우가 흔해서, browser를 먼저 보면 봇이 사람으로 샌다.
UA_KINDS = (
    ("bot", ("bot", "crawler", "spider", "scanner", "scrape", "monitor", "uptime")),
    ("mcp-client", ("claude", "modelcontextprotocol", "mcp", "cursor", "chatgpt", "openai")),
    ("http-tool", ("curl", "wget", "httpie", "python-requests", "httpx", "node", "axios")),
    ("browser", ("mozilla", "chrome", "safari", "edge")),
)


def ua_kind(ua: str) -> str:
    """bot | mcp-client | http-tool | browser | other | none. **원문은 반환하지 않는다.**

    `none`은 "UA 헤더가 없었다"이지 "사람이었다"가 아니다 — 정상 MCP 클라이언트는 UA를
    보내므로 UA 부재는 오히려 봇 쪽 신호다. 집계기는 이 값을 `other`처럼 사람으로 세면
    안 된다(mcp_growth.py가 `ua_untagged`로 따로 담는다).
    """
    low = (ua or "").lower()
    if not low:
        return "none"
    for label, needles in UA_KINDS:
        if any(n in low for n in needles):
            return label
    return "other"


def _header(request, name: str) -> str:
    """헤더 조회는 전송 구현에 따라 던질 수 있다 — 계측 실패가 조회를 막지 않게 감싼다."""
    try:
        return (request.headers.get(name) or "").strip()
    except Exception:
        return ""


@dataclass
class Access:
    tier: str                    # "free" | "paid" | "local" | "unknown"(귀속 실패)
    subject: str                 # 쿼터 키: free=sha256(salt|IP)[:12], paid=key_prefix
    daily_limit: Optional[int]   # None = 무제한
    error: Optional[dict] = None  # 키가 제시됐으나 무효·만료 — 구조화 거부 응답
    # 이 호출이 우리 것인가. **집계 분모에서 제외되는 유일한 근거**다.
    # None = 미상(구 레코드·귀속 실패) — 0이나 False로 때우지 않는다.
    is_internal: Optional[bool] = None
    owner: Optional[str] = None
    # subject가 어디서 왔나(header 이름 또는 "peer"/"peer-direct"/"key"/"none").
    # 조작 가능한 x-forwarded-for발 subject를 집계에서 나중에 걸러낼 수 있어야 한다.
    subject_source: str = "none"
    # 요청 컨텍스트 조회 실패 사유 — 실패를 stdio(내부)로 때우지 않기 위한 필드.
    attribution_error: Optional[str] = None
    # 봇 판별용 굵은 분류(**원문 UA 미저장**). subject에는 들어가지 않는다 — D-2026W32-33.
    ua_kind: str = "none"
    # is_internal이 왜 그 값인지 사람이 읽을 수 있게. 원문 IP는 남기지 않는다.
    ip_class: str = "unknown"


# ---------------------------------------------------------------- 키 저장소

_keys_cache: dict[str, dict] = {}
_keys_mtime: float = -1.0
_keys_lock = threading.Lock()


def _load_keys() -> dict[str, dict]:
    """mcp_keys.json을 mtime 캐시로 읽는다. 손상 시 마지막 정상 캐시 유지 —
    파일 한 줄 깨졌다고 유료 사용자 전체를 잠그면 안 된다."""
    global _keys_cache, _keys_mtime
    try:
        mtime = KEYS_PATH.stat().st_mtime
    except OSError:
        return {}
    with _keys_lock:
        if mtime != _keys_mtime:
            try:
                data = json.loads(KEYS_PATH.read_text(encoding="utf-8"))
                _keys_cache = {r["key_hash"]: r for r in data.get("keys", []) if r.get("key_hash")}
                _keys_mtime = mtime
            except Exception:
                pass
    return _keys_cache


# ---------------------------------------------------------------- 요청 판정

def client_ip(request) -> tuple[str, str]:
    """(IP, 출처). **x-real-ip를 cf-connecting-ip보다 먼저 본다.**

    첫 이식본은 cf 우선이었는데 이 토폴로지에서는 그게 틀렸다(2026-08-06 stay-mcp
    codex 2차 → 2026-08-07 여기 이식, T-2026W32-105): 앞단 nginx는 CF 대역에서 온
    연결에 한해 `CF-Connecting-IP`를 검증해 `$remote_addr`로 삼고, 그 **검증된 값**을
    `X-Real-IP`로 넘긴다. 그런데 원본 `CF-Connecting-IP` 헤더는 삭제하지 않으므로,
    cf를 먼저 보면 오리진에 직접 붙어 그 헤더를 지어내는 쪽의 값을 믿게 된다
    (호출마다 IP를 바꿔 순호출자 부풀리기·사설주소 행세로 is_internal 위장).
    검증을 거친 `X-Real-IP`가 항상 먼저다.

    **폴백으로 `127.0.0.1`을 지어내지 않는다.** 종전 코드는 소켓 주소조차 없으면
    루프백을 반환했는데, 그러면 귀속 불가 호출이 '우리 것'으로 둔갑해 분모에서 사라진다.
    미상 토큰 `?`로 남기면 mcp_growth.py가 '셀 수 없음'으로 정직하게 센다.
    """
    for h in ("x-real-ip", "cf-connecting-ip"):
        v = _header(request, h)
        if v:
            return v, h
    xff = _header(request, "x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip(), "x-forwarded-for"
    peer = peer_ip(request)
    return (peer, "peer") if peer else ("?", "none")


def peer_ip(request) -> str:
    """TCP 소켓의 실제 접속자 — HTTP 헤더로 위조할 수 없는 유일한 값.

    client_ip()는 쿼터 subject 산정용이라 프록시 헤더를 신뢰하지만,
    무제한 티어 부여처럼 권한이 걸린 판정에는 이 값만 쓴다.

    실증(2026-08-03 적대적 QA): quant에서 `CF-Connecting-IP: 127.0.0.1` 한 줄을
    붙여 오리진 443에 직결하니 tier=local(무제한)이 그대로 떨어졌고, 서버 로그에
    `{"tier":"local","subject":"127.0.0.1"}`로 기록됐다. realty-mcp는 같은 결함을
    같은 날 peer_ip로 봉합했는데 이쪽에 이식되지 않아 열려 있었다.
    지금은 유료 전용 도구가 없어 피해가 '무료 한도 면제'까지지만, 유료 도구가
    하나라도 생기는 순간 헤더 한 줄로 전면 개방이 된다.
    """
    try:
        return request.client.host if request.client else ""
    except Exception:
        return ""


def trusted_ip(request) -> tuple[str, str]:
    """귀속에 **실제로 쓰는** 주소와 그 출처.

    오리진 직결(peer가 공인 외부 주소)이면 프록시 헤더는 전부 자칭이므로 버리고
    소켓 주소를 쓴다 — resolve_access가 예전부터 하던 판단을 한 곳으로 모은 것이다.
    `ip_class`가 이 함수를 같이 쓰는 게 요점이다: 판정에 쓴 주소와 판정 근거로 남기는
    분류가 갈리면, 위조된 헤더로 `ip_class`만 바꿔 로그를 오염시킬 수 있다.
    """
    peer = peer_ip(request)
    if peer and classify_ip(peer) == "external":
        return peer, "peer-direct"
    return client_ip(request)


def _extract_key(request) -> Optional[str]:
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    # ChatGPT 커넥터는 커스텀 헤더를 못 붙인다 — ?key= 쿼리 폴백
    try:
        return request.query_params.get("key")
    except Exception:
        return None


def resolve_access(request) -> Access:
    """요청 → 티어 판정. request=None은 stdio 로컬 경로(HTTP 요청 자체가 없음)."""
    if request is None:
        return Access(tier="local", subject="stdio", daily_limit=None,
                      is_internal=True, owner="local", ip_class="stdio")

    # 원문을 버리기 전에 분류만 뽑아 둔다(D-2026W32-33). 모든 경로가 같은 값을 쓰므로
    # 거부 응답(무효키·만료·쿼터)도 봇/사람이 구분된다 — 거부만 골라 오는 트래픽이
    # 봇인지 사람인지가 곧 "수요가 있는가"의 근거다.
    kind = ua_kind(_header(request, "user-agent"))
    ip, source = trusted_ip(request)
    ipc = classify_ip(ip) if ip and ip != "?" else "unknown"

    key = _extract_key(request)
    if key:
        rec = _load_keys().get(hashlib.sha256(key.encode()).hexdigest())
        if rec is None or not rec.get("is_active", False):
            return Access("free", "invalid", None, error={
                "error": "invalid_key",
                "message": "API 키가 유효하지 않습니다(회수됐거나 오타). "
                           f"키를 빼면 무료 티어로 계속 쓸 수 있습니다. 구매·재발급: {PRICING_URL}",
            }, ua_kind=kind, ip_class=ipc)
        expires = rec.get("expires_at")
        if expires:
            try:
                exp_dt = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > exp_dt:
                    return Access("free", rec.get("key_prefix", "?"), None, error={
                        "error": "key_expired",
                        "message": f"API 키가 {str(expires)[:10]}에 만료됐습니다. 갱신: {PRICING_URL}",
                    }, ua_kind=kind, ip_class=ipc)
            except ValueError:
                pass  # 만료일이 못 읽히면 키를 잠그지 않는다 — 발급 CLI가 포맷을 보장
        return Access("paid", rec.get("key_prefix", "paid"),
                      int(rec.get("daily_limit") or PAID_DAILY_DEFAULT),
                      is_internal=rec.get("is_internal"), owner=rec.get("owner"),
                      subject_source="key", ua_kind=kind, ip_class=ipc)

    # local(무제한) 판정은 위조 불가한 소켓 주소로만. 헤더(client_ip)를 쓰면
    # `CF-Connecting-IP: 127.0.0.1` 한 줄로 무제한이 된다(2026-08-03 실증).
    if peer_ip(request) in UNLIMITED_IPS:
        return Access("local", "peer-loopback", None,
                      is_internal=True, owner="local", subject_source="peer",
                      ua_kind=kind, ip_class="loopback")
    # source == "peer-direct"이면 **프록시를 거치지 않은 직결이다.** 그러면 프록시 헤더는
    # 전부 자칭이므로 trusted_ip()가 이미 버리고 소켓 주소를 골라 놨다.
    if ip == "?":
        return Access("free", "?", FREE_DAILY, is_internal=None, subject_source=source,
                      ua_kind=kind, ip_class="unknown")
    return _free_access(ip, source, kind, ipc)


def _free_access(ip: str, source: str, kind: str = "none",
                 ipc: str = "unknown") -> Access:
    """free 티어 Access. **해시 전 원문 IP로 내외부를 판정하고 결과만 남긴다** —
    subject가 해시라 집계기(mcp_growth.py)가 주소 형태를 다시 볼 수 없기 때문이다."""
    internal = classify_ip(ip) in ("loopback", "private", "ours")
    sub = subject_hash(ip)
    if sub is None:
        # salt를 못 쓰면 **가짜 식별자를 만들지 않는다** — 미상(?)으로 남긴다.
        # 쿼터는 공유 버킷 `?`에 걸린다: salt 고장이 무제한 개방으로 새지 않고,
        # 하루 한도에서 시끄럽게 막혀 고장이 드러난다(fail-closed).
        return Access("free", "?", FREE_DAILY, is_internal=None, subject_source=source,
                      ua_kind=kind, ip_class=ipc)
    return Access("free", sub, FREE_DAILY, is_internal=internal, subject_source=source,
                  ua_kind=kind, ip_class=ipc)


def unattributed_access(reason: str) -> Access:
    """요청 컨텍스트 조회 실패의 레코드. **stdio(내부·무제한)로 때우지 않는다.**

    계기(2026-08-06 stay-mcp codex 2차 → 2026-08-07 이식): SDK가 요청 컨텍스트를 못
    실어 주면 `ctx.request`가 `None`이 되고, 이 파일에서 `None`은 'stdio 로컬'로
    해석된다. 즉 SDK가 바뀌기만 해도 **모든 외부 HTTP 호출이 tier=local(무제한·쿼터
    면제)로 기록돼 분모에서 통째로 사라지는데**, 필드는 멀쩡히 채워져 있어 집계기가
    이상을 감지할 방법이 없다. 그래서 실패를 실패로 적고, 게이트는 free와 같게
    잠근다(fail-closed).
    """
    return Access("unknown", "?", FREE_DAILY, is_internal=None,
                  subject_source="none", attribution_error=reason)


# 집계기(/data/ops/mcp_growth.py)가 읽는 **계약 필드의 단일 진실원**.
# server.py가 손으로 하나씩 담으면 필드 하나가 조용히 빠져도 아무도 모른다 —
# 여기 한 곳에 모아 두고 selftest가 누락·원문유출을 함께 본다.
CONTRACT_FIELDS = ("tier", "subject", "subject_source", "is_internal",
                   "owner", "ua_kind", "ip_class")


def access_fields(access: Access) -> dict:
    """레코드에 실을 귀속 필드. **원문 IP·UA는 이 dict에 절대 들어가지 않는다.**"""
    out = {
        "tier": access.tier,
        "subject": access.subject,
        "subject_source": access.subject_source,
        # None(미상)을 False로 때우지 않는다 — 구 레코드와 구별돼야 한다.
        "is_internal": access.is_internal,
        "owner": access.owner,
        "ua_kind": access.ua_kind,
        "ip_class": access.ip_class,
    }
    if access.attribution_error:
        out["attribution_error"] = access.attribution_error
    return out


# ---------------------------------------------------------------- 일일 쿼터

class DailyQuota:
    """subject(IP 또는 key_prefix)별 일일 카운터 — 파일 영속, UTC 날짜 리셋.

    realty-mcp DailyQuota 동일 이식(조상은 backend AnonDailyQuota).
    soft cap(경쟁 시 소폭 초과 허용) — 남용 방지 목적엔 충분하다.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _read(self) -> dict:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if str(data.get("date", "")) != self._today():
                return {}
            return {str(k): int(v) for k, v in (data.get("counts") or {}).items()}
        except Exception:
            return {}

    def _write(self, counts: dict) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({"date": self._today(), "counts": counts}), encoding="utf-8")
        except Exception:
            pass  # 영속 실패가 조회를 막지 않는다

    def consume(self, subject: str, limit: int) -> bool:
        """한도 내면 1 소비하고 True, 소진이면 False (소비 없음)."""
        with self._lock:
            counts = self._read()
            if counts.get(subject, 0) >= limit:
                return False
            counts[subject] = counts.get(subject, 0) + 1
            self._write(counts)
            return True

    def used(self, subject: str) -> int:
        with self._lock:
            return self._read().get(subject, 0)
