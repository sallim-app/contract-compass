"""contract-mcp 키 대장(keystore) 단위 테스트 (2026-07-30 — 판매 파이프라인)."""
import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "mcp"))

import keystore  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(keystore, "KEYS_PATH", tmp_path / "keys.json")
    monkeypatch.setattr(keystore, "CALL_LOG", tmp_path / "calls.jsonl")


def test_issue_self_returns_plaintext_and_extended_fields():
    key, rec = keystore.issue("크몽#1", 30, 2000, channel="kmong",
                              amount_krw=9900, contact="a@b.c", order_id="K1")
    assert key.startswith("cc_live_") and rec["key_hash"] == hashlib.sha256(key.encode()).hexdigest()
    assert rec["channel"] == "kmong" and rec["amount_krw"] == 9900
    assert rec["contact"] == "a@b.c" and rec["source"] == "self"


def test_issue_requires_owner():
    """T-2026W32-85: 소유자 미상 키 발급 금지 — owner·contact·order_id 셋 다 비면 거부.

    realty-mcp 실사고 재발 방지: name 자유 메모뿐이면 QA 키가 유료 분모에 섞인다."""
    with pytest.raises(ValueError, match="owner"):
        keystore.issue("QA용")
    assert keystore.list_keys() == []   # 거부된 발급이 대장에 남으면 안 된다


def test_issue_owner_derivation_and_internal_flag():
    """owner 미명시 시 contact → order_id 순 파생(웹훅 호출부 무수정 귀속)."""
    _, by_contact = keystore.issue("LS", key="K-c", contact="buyer@x.com", order_id="ls-1")
    assert by_contact["owner"] == "buyer@x.com"
    assert by_contact["is_internal"] is False       # 판매분 기본값 = 외부(분모 포함)
    _, by_order = keystore.issue("Creem", key="K-o", order_id="creem-77")
    assert by_order["owner"] == "creem-77"
    _, qa = keystore.issue("야간QA", owner="naru-qa", purpose="QA", internal=True)
    assert qa["owner"] == "naru-qa" and qa["is_internal"] is True and qa["purpose"] == "QA"


def test_issue_mirror_no_plaintext():
    """LS 미러 — 외부 키를 해시 등록, 평문 반환 없음."""
    key, rec = keystore.issue("LS 30일", 30, 2000, channel="lemonsqueezy",
                              key="ABCD-1234-EFGH-5678", source="ls_mirror", order_id="ls-77")
    assert key is None
    assert rec["key_hash"] == hashlib.sha256(b"ABCD-1234-EFGH-5678").hexdigest()
    assert rec["source"] == "ls_mirror"


def test_order_idempotency():
    """웹훅 재전송 — 같은 order_id는 재발급하지 않고 기존 레코드 반환."""
    _, first = keystore.issue("LS", key="K-1", order_id="ls-9", source="ls_mirror")
    key2, again = keystore.issue("LS", key="K-DIFFERENT", order_id="ls-9", source="ls_mirror")
    assert key2 is None and again["key_hash"] == first["key_hash"]
    assert len(keystore.list_keys()) == 1


def test_revoke_by_order_for_refund():
    keystore.issue("LS", key="K-2", order_id="ls-10", source="ls_mirror")
    rec = keystore.revoke_by_order("ls-10")
    assert rec and rec["is_active"] is False and rec["revoke_reason"] == "refund"
    assert keystore.revoke_by_order("ls-없음") is None


def test_report_aggregates_revenue_and_expiring():
    keystore.issue("A", days=2, channel="kmong", amount_krw=9900, owner="크몽#A")  # D-7 임박
    keystore.issue("B", days=60, channel="lemonsqueezy", amount_krw=24900, owner="ls#B")
    out = keystore.report()
    assert "만료임박(D-7) 1" in out
    assert "9,900원" in out and "24,900원" in out and "34,800원" in out
    assert len(keystore.expiring_within(3)) == 1
