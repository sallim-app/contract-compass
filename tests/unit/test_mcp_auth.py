"""contract-mcp 키 인증·티어·쿼터 단위 테스트 (2026-07-30, realty 이식 검증)."""
import hashlib
import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "mcp"))

import auth  # noqa: E402

pytestmark = pytest.mark.unit


def _req(headers=None, query=None, host="203.0.113.5"):
    """auth가 쓰는 인터페이스만 흉내 — headers.get/query_params.get(소문자 키)."""
    r = MagicMock()
    r.headers = {k.lower(): v for k, v in (headers or {}).items()}  # dict.get 그대로 사용
    r.query_params = query or {}
    r.client = MagicMock(host=host)
    return r


def _install_key(tmp_path, monkeypatch, *, active=True, expires="2099-01-01T00:00:00+00:00", daily=77):
    key = "cc_live_" + "ab" * 32
    kf = tmp_path / "keys.json"
    kf.write_text(json.dumps({"keys": [{
        "key_hash": hashlib.sha256(key.encode()).hexdigest(),
        "key_prefix": key[:16], "is_active": active,
        "expires_at": expires, "daily_limit": daily}]}))
    monkeypatch.setattr(auth, "KEYS_PATH", kf)
    monkeypatch.setattr(auth, "_keys_mtime", -1.0)
    monkeypatch.setattr(auth, "_keys_cache", {})
    return key


def test_stdio_is_local_unlimited():
    a = auth.resolve_access(None)
    assert a.tier == "local" and a.daily_limit is None


def test_loopback_unlimited():
    a = auth.resolve_access(_req(host="127.0.0.1"))
    assert a.tier == "local" and a.daily_limit is None


def test_anonymous_free_tier_by_ip():
    """무키 호출은 free 티어이고 쿼터 키는 **IP 원문이 아니라 해시**다(D-2026W32-33)."""
    a = auth.resolve_access(_req(headers={"x-real-ip": "198.51.100.3"}))
    assert a.tier == "free" and a.daily_limit == auth.FREE_DAILY
    assert a.subject == auth.subject_hash("198.51.100.3")
    assert "198.51.100.3" not in a.subject


def test_valid_key_paid_tier(tmp_path, monkeypatch):
    key = _install_key(tmp_path, monkeypatch, daily=77)
    a = auth.resolve_access(_req(headers={"authorization": f"Bearer {key}"}))
    assert a.tier == "paid" and a.daily_limit == 77 and a.error is None


def test_key_via_query_fallback(tmp_path, monkeypatch):
    """ChatGPT 커넥터 경로 — 헤더 없이 ?key=."""
    key = _install_key(tmp_path, monkeypatch)
    a = auth.resolve_access(_req(query={"key": key}))
    assert a.tier == "paid"


def test_invalid_and_expired_key_return_structured_error(tmp_path, monkeypatch):
    _install_key(tmp_path, monkeypatch)
    bad = auth.resolve_access(_req(headers={"authorization": "Bearer cc_live_wrong"}))
    assert bad.error and bad.error["error"] == "invalid_key" and auth.PRICING_URL in bad.error["message"]
    key = _install_key(tmp_path, monkeypatch, expires="2020-01-01T00:00:00+00:00")
    exp = auth.resolve_access(_req(headers={"authorization": f"Bearer {key}"}))
    assert exp.error and exp.error["error"] == "key_expired"


def test_daily_quota_consume_and_block(tmp_path):
    q = auth.DailyQuota(tmp_path / "q.json")
    assert all(q.consume("1.2.3.4", 3) for _ in range(3))
    assert q.consume("1.2.3.4", 3) is False  # 소진 — 소비 없음
    assert q.consume("5.6.7.8", 3) is True   # 다른 subject는 독립
    assert q.used("1.2.3.4") == 3


# --------------------------------------------------------------------------
# 귀속 계측 회귀 (2026-08-07, T-2026W32-105 — stay-mcp R22~R26 이식).
# 세 결함을 각각 재현한다: ①cf 헤더 우선 신뢰 ②subject에 원문 IP ③귀속 실패→stdio 오분류.
# 이 세 검사는 수리 전 코드에서 **실제로 실패**하는 것을 확인하고 넣었다.

def test_xrealip_beats_selfclaimed_cf_header():
    """①앞단 nginx가 검증해 넘긴 x-real-ip가 자칭 cf-connecting-ip를 이긴다.

    nginx는 원본 CF-Connecting-IP를 지우지 않는다 — cf를 먼저 보면 오리진 직결자가
    그 헤더를 지어내 사설주소 행세(is_internal 위장)·IP 회전(분모 부풀리기)을 한다.
    """
    a = auth.resolve_access(_req(headers={"x-real-ip": "8.8.8.8",
                                          "cf-connecting-ip": "10.0.0.9"}))
    assert a.subject_source == "x-real-ip"
    assert a.subject == auth.subject_hash("8.8.8.8")
    assert a.is_internal is False, "위조 cf-connecting-ip로 사설주소 행세해 내부로 숨었다"


def test_origin_direct_ignores_proxy_headers():
    """프록시 미경유 직결이면 헤더는 전부 자칭이므로 소켓 주소만 쓴다."""
    a = auth.resolve_access(_req(headers={"cf-connecting-ip": "127.0.0.1"}, host="8.8.8.8"))
    assert a.tier != "local", "cf-connecting-ip 위조로 무제한 티어가 부여됐다"
    assert a.subject_source == "peer-direct"
    assert a.subject == auth.subject_hash("8.8.8.8")
    # 헤더만 갈아 끼워도 subject는 하나로 유지된다(순호출자 부풀리기 차단)
    rotated = {auth.resolve_access(_req(headers={"cf-connecting-ip": f"9.9.9.{i}"},
                                        host="8.8.8.8")).subject for i in range(5)}
    assert rotated == {auth.subject_hash("8.8.8.8")}


def test_subject_is_hashed_and_stable(monkeypatch):
    """②원문 IP가 subject·쿼터 키에 남지 않되, 같은 IP는 항상 같은 subject다."""
    a = auth.resolve_access(_req(headers={"x-real-ip": "8.8.8.8"}))
    assert a.subject == auth.subject_hash("8.8.8.8") and "8.8.8.8" not in a.subject
    assert a.subject == auth.resolve_access(_req(headers={"x-real-ip": "8.8.8.8"},
                                                 host="172.18.0.9")).subject
    # 우리 주소는 해시 전 원문으로 판정해 결과만 남긴다.
    # **실제 오리진 IP를 테스트에 쓰지 않는다**(2026-08-15): 공개 저장소라 테스트가 곧 노출이다.
    # 문서용 예약 대역(TEST-NET-3, RFC 5737)을 주입해 같은 경로를 검증한다.
    monkeypatch.setattr(auth, "OURS", {"203.0.113.10", "203.0.113.11"})
    for ip in ("203.0.113.10", "203.0.113.11", "10.0.1.14"):
        assert auth.resolve_access(_req(headers={"x-real-ip": ip})).is_internal is True


def test_salt_failure_does_not_fabricate_identifier(monkeypatch):
    """salt를 못 쓰면 가짜 식별자 대신 미상 — 단 쿼터는 무제한으로 새지 않는다."""
    monkeypatch.setattr(auth, "_salt_cache", None)
    monkeypatch.setattr(auth, "SALT_FILE", Path("/proc/1/nonexistent/salt"))
    a = auth.resolve_access(_req(headers={"x-real-ip": "8.8.8.8"}))
    assert a.subject == "?" and a.is_internal is None
    assert a.daily_limit == auth.FREE_DAILY, "salt 고장이 무제한 개방으로 샜다"


def test_attribution_failure_is_not_stdio():
    """③요청 컨텍스트 조회 실패를 stdio(내부·무제한)로 때우지 않는다."""
    u = auth.unattributed_access("no_request_context")
    assert u.tier == "unknown" and u.subject == "?" and u.is_internal is None
    assert u.attribution_error == "no_request_context"
    assert u.daily_limit is not None, "귀속 실패가 무제한으로 열렸다 — fail-open"


def test_missing_request_context_is_failure_not_local():
    """서버 미들웨어 층에서도 같은 판정 — 비-stdio 기동의 request=None은 고장이다.

    기본값이 fail-closed다: `__main__`을 안 거치는 기동(ASGI import 등)에서도
    '로컬 무제한'으로 새지 않는다(codex 교차검증 지적).
    """
    sys.path.insert(0, str(ROOT / "mcp"))
    import server as mcp_server

    ctx = MagicMock()
    ctx.request = None
    assert mcp_server._STDIO_MODE is False, "기본값이 stdio면 fail-open이다"
    req, err = mcp_server._current_request(ctx)
    assert req is None and err == "no_request_context"
    # stdio 기동에서만 요청 부재가 정상이다 — 그때는 고장으로 오인하지 않는다
    prev = mcp_server._STDIO_MODE
    try:
        mcp_server._STDIO_MODE = True
        assert mcp_server._current_request(ctx) == (None, None)
    finally:
        mcp_server._STDIO_MODE = prev


def test_quotagate_records_attribution_failure_end_to_end(tmp_path, monkeypatch):
    """미들웨어를 실제로 통과시켜 **기록된 레코드**를 본다(헬퍼 반환값이 아니라).

    codex 교차검증 지적: `_current_request()`만 검사하면 QuotaGate가 그 결과를
    실제로 쓰는지·쿼터가 잠기는지·레코드에 무엇이 남는지는 검증되지 않는다.
    """
    import asyncio

    sys.path.insert(0, str(ROOT / "mcp"))
    import server as mcp_server

    log = tmp_path / "calls.jsonl"
    monkeypatch.setattr(mcp_server, "_CALL_LOG", log)
    monkeypatch.setattr(mcp_server, "_quota", auth.DailyQuota(tmp_path / "q.json"))

    ctx = MagicMock()
    ctx.method = "tools/call"
    ctx.params = {"name": "decide_contract_method", "arguments": {}}
    ctx.request = None   # HTTP 전송인데 컨텍스트가 없다 = 고장

    async def call_next(_):
        return MagicMock(structured_content={})

    asyncio.run(mcp_server.QuotaGate()(ctx, call_next))
    rec = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["tier"] == "unknown", "귀속 실패가 local(내부·무제한)로 기록됐다"
    assert rec["subject"] == "?" and rec["is_internal"] is None
    assert rec["attribution_error"] == "no_request_context"


def test_ua_kind_classifies_without_storing_raw():
    """UA는 판별력만 남기고 원문·식별자에서는 뺀다(D-2026W32-33)."""
    assert auth.ua_kind("claude-code/1.0") == "mcp-client"
    assert auth.ua_kind("curl/8.5.0") == "http-tool"
    assert auth.ua_kind("") == "none"
    # 봇이 브라우저를 자칭하는 게 흔하다 — browser로 새면 크롤러가 사람 분모에 섞인다
    assert auth.ua_kind("Mozilla/5.0 (compatible; SomeRegistryBot/2.1)") == "bot"
    # UA를 바꿔도 subject가 갈리지 않는다(한 사람이 UA 10개로 순호출자 10명이 되는 구멍)
    rotated = {auth.resolve_access(_req(headers={"x-real-ip": "8.8.8.8",
                                                 "user-agent": f"ua-{i}/1.0"})).subject
               for i in range(10)}
    assert len(rotated) == 1


def test_record_contract_fields_and_no_raw_leak():
    """집계기가 읽는 계약 필드가 다 실리고, 원문 IP·UA는 한 글자도 안 남는다."""
    ua_raw = "Mozilla/5.0 (compatible; SomeRegistryBot/2.1; +http://example.com/bot)"
    rec = auth.access_fields(auth.resolve_access(
        _req(headers={"x-real-ip": "8.8.8.8", "user-agent": ua_raw})))
    assert not set(auth.CONTRACT_FIELDS) - set(rec)
    blob = " ".join(str(v) for v in rec.values())
    for leak in ("8.8.8.8", "Mozilla", "SomeRegistryBot", "example.com"):
        assert leak not in blob, f"원문이 레코드에 남았다({leak})"
    assert rec.get("ua_kind") == "bot" and rec.get("ip_class") == "external"
    # 귀속 실패 레코드도 같은 계약을 지킨다(필드가 빠지면 집계기가 조용히 못 센다)
    assert not set(auth.CONTRACT_FIELDS) - set(
        auth.access_fields(auth.unattributed_access("ContextError")))


def test_ip_class_evidence_cannot_be_forged_separately():
    """판정 근거(ip_class)도 위조 대상이다 — 판정에 쓴 주소와 같은 주소로 뽑는다."""
    spoof = auth.resolve_access(_req(headers={"x-real-ip": "10.0.0.9"}, host="8.8.8.8"))
    assert spoof.ip_class == "external" and spoof.is_internal is False
