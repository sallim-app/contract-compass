"""contract-mcp 키 대장 — 발급·회수·미러·매출 리포트의 단일 모듈 (2026-07-30).

issue_key.py(CLI)와 server.py의 구매 웹훅이 공유한다. 저장소는 data/mcp_keys.json
(sha256 해시만 — 평문은 자체 발급 순간 1회 노출, LS 미러는 평문을 아예 안 받아도
됨(웹훅이 주는 키를 해시 후 폐기)).

레코드 필드(2026-07-30 확장, 기존 레코드와 하위호환):
  key_hash·key_prefix·name·is_active·created_at·expires_at·daily_limit (기존)
  + channel("manual"|"kmong"|"lemonsqueezy"…) · amount_krw · contact · order_id
  + source("self"=cc_live_ 자체 발급 | "ls_mirror"=Lemon Squeezy 라이선스 키 미러)
  + owner(누구 것인가 — 필수) · purpose(왜 발급했나) · is_internal(우리 것인가,
    2026-08-09 T-2026W32-85 realty-mcp 이식 — 분모 오염 방지: 종전엔 name 자유 메모뿐이라
    QA 키와 구매자 키를 로그만 보고 구분할 수 없었다. realty-mcp에서 유료 호출 52건 전량이
    우리 QA 키인데 집계가 '유료 순사용자 3명'으로 읽은 실사고의 재발 방지다.
    auth.resolve_access가 paid Access에 owner·is_internal을 그대로 싣는다.)
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import secrets as _secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

KEYS_PATH = Path(__file__).resolve().parents[1] / "data" / "mcp_keys.json"
CALL_LOG = Path(__file__).resolve().parents[1] / "logs" / "mcp_calls.jsonl"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load(f) -> dict:
    f.seek(0)
    raw = f.read()
    return json.loads(raw) if raw.strip() else {"keys": []}


def _save(f, data: dict) -> None:
    f.seek(0)
    f.truncate()
    f.write(json.dumps(data, ensure_ascii=False, indent=1))


def _locked_update(fn):
    """keys.json을 배타 락으로 읽고-변형-저장. fn(data)->result 반환."""
    KEYS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(KEYS_PATH, "a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        data = _load(f)
        result = fn(data)
        _save(f, data)
    KEYS_PATH.chmod(0o600)
    return result


def issue(name: str, days: int = 30, daily: int = 2000, *,
          channel: str = "manual", amount_krw: int = 0, contact: str = "",
          order_id: str = "", key: str | None = None, source: str = "self",
          owner: str = "", purpose: str = "", internal: bool = False) -> tuple[str | None, dict]:
    """키 등록. key=None이면 cc_live_ 신규 생성(평문 반환), 지정 시 미러(평문 미반환).

    order_id가 이미 대장에 있으면 발급하지 않고 기존 레코드 반환(웹훅 재전송 멱등).

    **owner는 필수다** (T-2026W32-85). 명시가 없으면 order_id → contact 순으로 파생한다 —
    웹훅 판매분은 주문번호·이메일이 항상 있어 호출부 수정 없이 구매자로 귀속된다.
    셋 다 비면 ValueError: 소유자 미상 키를 조용히 만들면 분모가 다시 오염된다.

    **contact(이메일) 파생분은 해시로 줄여 담는다** — owner는 auth.access_fields를 타고
    키 대장(0600)보다 넓은 호출 로그(mcp_calls.jsonl, 0644)에 매 호출 기록되므로,
    원문 이메일을 owner에 복사하면 개인정보가 보호 경계 밖으로 샌다(2026-08-09 codex 지적).
    원문은 기존 contact 필드(대장 안)에만 남는다 — 운영자는 report()에서 둘 다 본다.
    """
    if order_id:
        existing = find_by_order(order_id)
        if existing:
            return None, existing
    owner = owner or order_id or (
        f"c:{hashlib.sha256(contact.encode()).hexdigest()[:12]}" if contact else "")
    if not owner:
        raise ValueError("owner는 필수다 — 구매자 식별자(주문번호·연락처) 또는 "
                         "내부 키면 owner='naru-qa', internal=True로 발급하라")
    plaintext = None
    if key is None:
        plaintext = key = f"cc_live_{_secrets.token_hex(32)}"
    now = _now()
    rec = {
        "key_hash": hashlib.sha256(key.encode()).hexdigest(),
        "key_prefix": key[:16],
        "name": name,
        "owner": owner,           # 누구 것인가 — 구매자 식별(주문번호·연락처 등)
        "purpose": purpose,       # 왜 발급했나 — 판매/QA/데모
        "is_internal": internal,  # 우리 것인가 — 집계 분모에서 제외되는 유일한 근거
        "is_active": True,
        "created_at": now.isoformat(timespec="seconds"),
        "expires_at": (now + timedelta(days=days)).isoformat(timespec="seconds"),
        "daily_limit": daily,
        "channel": channel,
        "amount_krw": int(amount_krw or 0),
        "contact": contact,
        "order_id": order_id,
        "source": source,
    }
    _locked_update(lambda data: data["keys"].append(rec))
    return plaintext, rec


def revoke(prefix: str) -> dict:
    """prefix로 활성 키 1건 회수. 0건/다건이면 ValueError."""
    def _do(data):
        hit = [r for r in data["keys"]
               if r.get("key_prefix", "").startswith(prefix) and r.get("is_active")]
        if not hit:
            raise ValueError(f"활성 키 중 prefix '{prefix}' 매칭 없음")
        if len(hit) > 1:
            raise ValueError("매칭 다건 — prefix를 더 길게: "
                             + ", ".join(r["key_prefix"] for r in hit))
        hit[0]["is_active"] = False
        hit[0]["revoked_at"] = _now().isoformat(timespec="seconds")
        return hit[0]
    return _locked_update(_do)


def revoke_by_order(order_id: str) -> dict | None:
    """주문번호로 회수(환불 웹훅용). 없으면 None."""
    def _do(data):
        for r in data["keys"]:
            if r.get("order_id") == order_id and r.get("is_active"):
                r["is_active"] = False
                r["revoked_at"] = _now().isoformat(timespec="seconds")
                r["revoke_reason"] = "refund"
                return r
        return None
    return _locked_update(_do)


def find_by_order(order_id: str) -> dict | None:
    for r in list_keys():
        if r.get("order_id") == order_id:
            return r
    return None


def list_keys() -> list[dict]:
    try:
        return json.loads(KEYS_PATH.read_text(encoding="utf-8")).get("keys", [])
    except OSError:
        return []


# ---------------------------------------------------------------- 리포트

def _parse_dt(s: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def usage_last_days(days: int = 7) -> dict[str, int]:
    """mcp_calls.jsonl에서 subject(key_prefix/IP)별 최근 N일 호출수."""
    cutoff = _now() - timedelta(days=days)
    counts: dict[str, int] = {}
    try:
        for line in CALL_LOG.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                ts = _parse_dt(r.get("ts", ""))
                if ts and ts >= cutoff:
                    counts[r.get("subject", "?")] = counts.get(r.get("subject", "?"), 0) + 1
            except Exception:
                continue
    except OSError:
        pass
    return counts


def report() -> str:
    """사람이 읽는 대장 요약 — 활성/만료임박/월·채널 매출/키별 사용량."""
    keys = list_keys()
    now = _now()
    active = [r for r in keys if r.get("is_active")]
    expiring = [r for r in active
                if (d := _parse_dt(r.get("expires_at", ""))) and d <= now + timedelta(days=7)]
    revenue: dict[str, int] = {}   # "YYYY-MM/채널" → 원
    for r in keys:
        amt = int(r.get("amount_krw") or 0)
        if amt:
            month = str(r.get("created_at", ""))[:7]
            k = f"{month}/{r.get('channel', 'manual')}"
            revenue[k] = revenue.get(k, 0) + amt
    usage = usage_last_days(7)
    lines = [f"활성 {len(active)} / 총 {len(keys)} / 만료임박(D-7) {len(expiring)}", ""]
    if revenue:
        lines.append("매출(월/채널):")
        lines += [f"  {k}: {v:,}원" for k, v in sorted(revenue.items())]
        lines.append(f"  합계: {sum(revenue.values()):,}원")
    else:
        lines.append("매출 기록 없음 (발급 시 --amount, 웹훅은 자동 기록)")
    lines.append("")
    lines.append("활성 키:")
    for r in sorted(active, key=lambda r: r.get("expires_at", "")):
        u = usage.get(r.get("key_prefix", ""), 0)
        exp = str(r.get("expires_at", ""))[:10]
        flag = " ⚠️D-7" if r in expiring else ""
        # 내부 키를 눈에 띄게 — 이 목록을 보고 '유료 고객 N명'으로 오독하지 않게.
        kind = "내부" if r.get("is_internal") else ("외부" if "is_internal" in r else "미상")
        lines.append(f"  {r.get('key_prefix')}  {kind}  ~{exp}{flag}  {r.get('daily_limit')}콜/일  "
                     f"7일사용 {u}  [{r.get('channel', '?')}/{r.get('source', 'self')}] "
                     f"{r.get('owner') or r.get('name', '')} {r.get('contact', '')}")
    if not active:
        lines.append("  (없음)")
    return "\n".join(lines)


def expiring_within(days: int = 3) -> list[dict]:
    """만료 D-N 이내 활성 키 (알림 크론용)."""
    now = _now()
    return [r for r in list_keys() if r.get("is_active")
            and (d := _parse_dt(r.get("expires_at", ""))) and now <= d <= now + timedelta(days=days)]
