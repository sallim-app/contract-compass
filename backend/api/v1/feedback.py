import base64
import binascii
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from backend.config import BASE_DIR
from backend.api.deps import require_admin
from backend.services.rate_limiter import get_rate_limiter

router = APIRouter(prefix="/feedback", tags=["feedback"])
_LOG_PATH = BASE_DIR / "logs" / "feedback.jsonl"
_UPLOAD_DIR = BASE_DIR / "logs" / "uploads"

_MAX_FILE_BYTES = 10 * 1024 * 1024  # 10MB
_ALLOWED_EXT = {".csv", ".xlsx", ".xls", ".pdf", ".jpg", ".jpeg", ".png", ".txt", ".json", ".docx", ".doc"}

# ── 원클릭 화면 캡처(F-shot, 2026-07-18) ────────────────────────────────────
# FeedbackBox가 html2canvas로 찍은 현재 화면 PNG를 base64로 받아 파일로 저장한다.
# 저장 목적: 헤드리스 Claude가 나중에 Read로 열어 디자인 버그를 진단(의견+화면 매칭).
# → 열기 쉬운 고정 경로 data/feedback_shots/<id>.png, 파일명에 시각·세션꼬리 포함.
_SHOTS_DIR = BASE_DIR / "data" / "feedback_shots"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_MAX_SHOT_BYTES = 8 * 1024 * 1024          # 8MB — 풀페이지 PNG 1장 상한
_SHOTS_DIR_CAP_BYTES = 1024 * 1024 * 1024  # 1GB — 디스크 용량 방어(초과 시 신규 저장 거부)
# 스크린샷 첨부는 텍스트 의견보다 무거우므로 별도 per-IP 슬라이딩 윈도우 한도(의견 자체는 무제한).
_SHOT_LIMITS = {"minute": 5, "hour": 40}


def _decode_screenshot(b64: str) -> bytes:
    """data URL(또는 raw base64) → PNG bytes. 비-PNG·대용량·손상은 400으로 거부.

    프론트(FeedbackBox)는 캡처 성공 시에만 전송하고 실패 시 의견만 보내므로,
    여기서의 400은 손상/위조/초과 페이로드 방어용(정상 사용자 의견은 잃지 않음)."""
    s = (b64 or "").strip()
    if not s:
        raise HTTPException(400, "빈 스크린샷")
    if s.startswith("data:"):
        header, _, s = s.partition(",")
        if "image/png" not in header:
            raise HTTPException(400, "PNG 스크린샷만 허용됩니다")
    # 디코드 전 대략 상한(base64는 원본의 약 4/3배) — 거대 페이로드 조기 차단
    if len(s) > (_MAX_SHOT_BYTES // 3) * 4 + 4096:
        raise HTTPException(400, f"스크린샷이 너무 큽니다 (최대 {_MAX_SHOT_BYTES // (1024 * 1024)}MB)")
    try:
        raw = base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(400, "스크린샷 base64 디코드 실패")
    if len(raw) == 0:
        raise HTTPException(400, "빈 스크린샷")
    if len(raw) > _MAX_SHOT_BYTES:
        raise HTTPException(400, f"스크린샷이 너무 큽니다 (최대 {_MAX_SHOT_BYTES // (1024 * 1024)}MB)")
    if not raw.startswith(_PNG_MAGIC):
        raise HTTPException(400, "PNG 형식이 아닙니다")
    return raw


def _shots_dir_size() -> int:
    if not _SHOTS_DIR.exists():
        return 0
    return sum(f.stat().st_size for f in _SHOTS_DIR.glob("*.png"))


def _save_screenshot(raw: bytes, session_id: str) -> dict:
    """검증된 PNG bytes를 data/feedback_shots/<id>.png로 저장. 메타 dict 반환."""
    if _shots_dir_size() + len(raw) > _SHOTS_DIR_CAP_BYTES:
        raise HTTPException(507, "스크린샷 저장 공간이 가득 찼습니다 (관리자 확인 필요)")
    _SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    sid_tail = (session_id.split("-")[-1] or "anon")[-8:] or "anon"
    shot_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{sid_tail}_{uuid.uuid4().hex[:8]}"
    out = _SHOTS_DIR / f"{shot_id}.png"
    out.write_bytes(raw)
    return {"id": shot_id, "saved_path": str(out), "size_bytes": len(raw)}


def _save_attachment(upload: UploadFile) -> tuple[str, int]:
    """업로드 파일 검증 후 저장. (저장경로, 바이트수) 반환."""
    ext = Path(upload.filename or "").suffix.lower()
    if ext not in _ALLOWED_EXT:
        raise HTTPException(
            400,
            f"허용되지 않은 파일 형식입니다 ({ext or '확장자 없음'}). "
            f"허용: {', '.join(sorted(_ALLOWED_EXT))}",
        )

    data = upload.file.read()
    if len(data) > _MAX_FILE_BYTES:
        raise HTTPException(400, f"파일이 너무 큽니다 (최대 {_MAX_FILE_BYTES // (1024*1024)}MB)")
    if len(data) == 0:
        raise HTTPException(400, "빈 파일은 업로드할 수 없습니다")

    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}{ext}"
    out = _UPLOAD_DIR / safe_name
    out.write_bytes(data)
    return str(out), len(data)


@router.post("")
async def submit_feedback(
    request: Request,
    session_id: str = Form(...),
    rating: int = Form(...),
    comment: str | None = Form(None),
    feedback_type: Literal["qna", "general", "recommendation"] = Form("general"),  # 2026-06-02: recommendation 추가
    question: str | None = Form(None),
    answer: str | None = Form(None),
    # 2026-06-02: 사용자가 의견 줄 때 화면 컨텍스트 자동 기록
    page: str | None = Form(None),                  # Step1Page / Step2Page / Step3Page / NoticeDraftPage
    step: str | None = Form(None),                  # 1 | 2 | 3 | 4
    project_name: str | None = Form(None),          # 의견 작성 당시 사업명
    contract_type: str | None = Form(None),         # service|product|construction
    estimated_price: int | None = Form(None),       # 원
    description: str | None = Form(None),           # 사업 개요
    suggested_method: str | None = Form(None),      # AI 추천 계약방법
    final_method: str | None = Form(None),          # 사용자가 Phase 2에서 선택한 method
    rule_id: str | None = Form(None),               # 매칭된 룰 ID
    # 2026-07-18: 원클릭 화면 캡처 컨텍스트(어느 화면·해상도·브라우저에서 봤는지)
    url: str | None = Form(None),                   # window.location.href
    viewport: str | None = Form(None),              # "WxH@dpr" (예: 1440x900@2)
    user_agent: str | None = Form(None),            # navigator.userAgent
    screenshot: str | None = Form(None),            # data:image/png;base64,... (html2canvas 캡처)
    attachment: UploadFile | None = File(None),
):
    entry: dict = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "rating": rating,
        "comment": comment,
        "feedback_type": feedback_type,
        "question": question,
        "answer": answer,
        # 컨텍스트 (있을 때만 의미 있음; None은 dict에 남기되 보드에서 skip)
        "context": {
            "page": page, "step": step,
            "project_name": project_name, "contract_type": contract_type,
            "estimated_price": estimated_price, "description": description,
            "suggested_method": suggested_method, "final_method": final_method,
            "rule_id": rule_id,
            # 2026-07-18: 캡처 화면 컨텍스트
            "url": url, "viewport": viewport, "user_agent": user_agent,
        },
    }

    if attachment and attachment.filename:
        path, size = _save_attachment(attachment)
        entry["attachment"] = {
            "original_name": attachment.filename,
            "saved_path": path,
            "size_bytes": size,
            "content_type": attachment.content_type,
        }

    # 원클릭 캡처: 스크린샷이 붙어온 경우만 별도 rate-limit + 검증 후 파일 저장.
    # 검증 실패(비-PNG·초과·손상)는 400으로 거부(프론트가 캡처 성공 시에만 보냄).
    if screenshot:
        ip = get_rate_limiter().check(request, _SHOT_LIMITS)
        raw = _decode_screenshot(screenshot)
        entry["screenshot"] = _save_screenshot(raw, session_id)
        get_rate_limiter().record(ip)

    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    _notify_ops(entry)
    return {
        "status": "ok",
        "has_attachment": "attachment" in entry,
        "has_screenshot": "screenshot" in entry,
    }


# ── 사용자 의견 보드 (공개) ──────────────────────────────────────────────
# 작성자에게 "내 의견이 어떻게 처리됐는지" 보여주고 추가 의견 유도.
# 익명화: session_id 끝 4자리만 노출. 코멘트는 그대로 (이미 작성자가 공개 의도로 입력).

# 의견 → 반영 상태 수동 매핑 (운영하며 채움).
_FEEDBACK_STATUS: dict[str, str] = {}


def _mask_sid(sid: str) -> str:
    if not sid:
        return "익명"
    last = sid.split("-")[-1] if "-" in sid else sid
    return f"…{last[-4:]}" if len(last) >= 4 else "…" + last


def _summarize(comment: str) -> str:
    """첫 줄 + 첫 60자."""
    if not comment:
        return ""
    first_line = comment.strip().split("\n")[0]
    return first_line[:80] + ("…" if len(first_line) > 80 else "")


def _notify_ops(entry: dict) -> None:
    """운영 알림 훅 — 공개판은 파일 적재만 하고 별도 알림 없음."""
    return


def _classify_feedback(comment: str, feedback_type: str = "general") -> tuple[str, str]:
    """코멘트 키워드 → (카테고리, 상태) 매핑. 정확도는 안 중요, 보드 그루핑용."""
    # 2026-06-02: feedback_type='recommendation' 우선 매핑
    if feedback_type == "recommendation":
        return ("추천 결과 평가", "inline")
    c = (comment or "").lower()
    if "금액" in c or "쉼표" in c or "부가세" in c:
        return ("입력 UX", "금액 입력")
    if "중기간" in c or "중소기업" in c or "plc" in c.lower() or "sme" in c.lower():
        return ("중기간 경쟁제품", "분류·매칭")
    if "법령" in c or "조" in c or "시행령" in c:
        return ("법령", "조문 인용")
    if "분류" in c or "카테고리" in c or "공사" in c or "용역" in c:
        return ("계약유형", "분류")
    if "ai" in c or "추천" in c:
        return ("추천 정확도", "AI")
    return ("기타", "기타")


@router.get("/board", dependencies=[Depends(require_admin)])
async def feedback_board() -> dict:
    """의견 보드(관리자 전용) — 익명화된 의견 + 카테고리 + 반영 상태.

    F2 의견(2차)은 자동 매칭 시도, 그 외 의견은 카테고리만 분류.

    2026-07-27 P0 근본수리: 과거엔 무인증 공개였다. 익명화(sid 마스킹)에도 불구하고
    **의견 원문(full_comment)과 작성 시점 화면 컨텍스트**가 그대로 실려, 사내 업무 맥락이
    외부에 통째로 노출됐다(아침 트리아지 실측 107,805 bytes). nginx IP 허용은 임시 봉합이라
    백엔드 자체를 fail-closed로 전환 — X-Admin-Token 없으면 401(ADMIN_TOKEN 미설정이면 503).
    """
    if not _LOG_PATH.exists():
        return {"total": 0, "items": [], "stats": {"reflected": 0, "reviewing": 0, "deferred": 0}}

    items = []
    stats = {"reflected": 0, "reviewing": 0, "deferred": 0, "open": 0}

    for line in _LOG_PATH.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        comment = (d.get("comment") or "").strip()
        if not comment or len(comment) < 5:
            continue  # 빈 의견 제외

        ts = d.get("ts", "") or d.get("timestamp", "")
        sid = _mask_sid(d.get("session_id", ""))
        cat, sub = _classify_feedback(comment, d.get("feedback_type", "general"))

        # 상태 — 키워드 매칭 점수화 (가장 많이 매칭되는 키 선택)
        # F123: feedback_type=recommendation → 자동 reflected
        ft = d.get("feedback_type", "")
        if ft == "recommendation":
            status, status_detail = "reflected", "inline 추천 피드백 — 즉시 cycle 반영"
        else:
            status = "open"
            status_detail = "검토 예정"
            best_score = 0
            for key, val in _FEEDBACK_STATUS.items():
                tokens = key.split("_")[1:]
                if not tokens:
                    continue
                # 매칭 점수: comment에 포함된 토큰 수. 모두 일치 = full match
                matches = sum(1 for t in tokens if t in comment)
                # 점수 = 완전 일치 시 길이 비례 큰 보너스 (구체적 키 > 일반 키)
                if matches == len(tokens):
                    score = 100 + len(tokens) * 50  # 완전 일치 + 길이 보너스 강화
                elif matches >= 2 and matches / len(tokens) >= 0.5:
                    score = matches * 10
                else:
                    score = 0
                if score > best_score:
                    best_score = score
                    status, status_detail = val.split(":", 1) if ":" in val else (val, "")

        # KST 변환 표시 (서버는 UTC라 +9h)
        ts_kst = ""
        if ts and "T" in ts:
            try:
                d_part, t_part = ts.split("T")[:2]
                hh = int(t_part[:2]) + 9
                if hh >= 24:
                    hh -= 24
                ts_kst = f"{d_part} {hh:02d}:{t_part[3:5]}"
            except Exception:
                ts_kst = ts[:16]
        else:
            ts_kst = (ts or "")[:16]

        stats[status] = stats.get(status, 0) + 1
        # 컨텍스트 추출 (있을 때만)
        ctx = d.get("context") or {}
        ctx_clean = {k: v for k, v in ctx.items() if v not in (None, "", 0)}
        items.append({
            "ts_kst": ts_kst,
            "sid": sid,
            "category": cat,
            "subcategory": sub,
            "summary": _summarize(comment),
            "full_comment": comment,
            "status": status,         # reflected | reviewing | deferred | open
            "status_detail": status_detail,
            "context": ctx_clean if ctx_clean else None,  # 2026-06-02: 작성 시점 화면 컨텍스트
            # 2026-07-18: 화면 캡처 첨부 여부만 노출(로컬 파일 경로는 공개 보드에 비노출)
            "has_screenshot": bool(d.get("screenshot")),
        })

    items.sort(key=lambda x: x["ts_kst"], reverse=True)
    return {"total": len(items), "stats": stats, "items": items}
