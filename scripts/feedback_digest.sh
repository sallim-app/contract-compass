#!/usr/bin/env bash
# 피드백 다이제스트 — 파일에만 쌓이면 아무도 안 읽는다(mcp-tool-design §1-3, realty 3주 방치 실사고).
# 하루 1회 신규 제보(웹+MCP) **요약**을 ops 인박스로. 오프셋 상태파일로 재시작 생존.
#
# ⚠️ 신뢰 경계 (2026-08-14, T-2026W33-115 — realty·stay는 2026-08-04 감사 CRIT-B1로 이미 막았다)
#   `report_issue`는 무인증 공개 도구다(누구나 인터넷에서 호출). 그런데 이 다이제스트가
#   가는 ops 인박스는 매일 --dangerously-skip-permissions 에이전트가 "티켓"으로 읽는다.
#   즉 신고 원문을 여기 실으면 **인터넷의 임의 문자열이 root 가능 에이전트의 지시 채널에
#   매일 실린다**. 그래서 원문(comment·question·related_query·도구 인자)은 단 한 바이트도
#   넣지 않고, **서버가 스스로 만든 값**만 담는다:
#     · 건수 · 출처(context.page는 서버가 세팅) · category(Literal 검증 통과분만)
#     · rating(숫자) · session_id(서버 생성 형식만) · 직전호출 도구명(등록된 도구와 대조)
#   원문 열람은 사람·에이전트가 `/data/ops/mcp-feedback.sh contract`로 **명시적으로** 할 때만
#   (그 스크립트가 주입 경고 배너로 감싸서 보여준다).
#
# env(테스트용 우회): CC_FB_LOG · CC_FB_STATE · CC_FB_REPORT
set -u
cd /data/apps/contract-compass
LOG="${CC_FB_LOG:-logs/feedback.jsonl}"
STATE="${CC_FB_STATE:-logs/.feedback_digest_offset}"
REPORT="${CC_FB_REPORT:-/data/ops/ops-report.sh}"
[ -f "$LOG" ] || exit 0
total=$(wc -l < "$LOG")
last=$(cat "$STATE" 2>/dev/null || echo 0)
[ "$total" -le "$last" ] && exit 0

# 프로그램은 heredoc(따옴표 고정), 데이터는 argv로 — 셸 인용부호와 파이썬 인용부호가
# 충돌해 조용히 깨지는 것을 막는다(mcp-feedback.sh와 같은 규약).
# ⚠️ 신규분을 **파이프로 넘기지 않는다**: `python3 -`는 프로그램을 stdin에서 읽으므로
# heredoc과 파이프가 같은 stdin을 다투고, 그 결과 입력이 조용히 0건이 된다(2026-08-14 실측 —
# 첫 구현이 "신규 0건"을 냈다). 로그 경로·오프셋을 argv로 주고 파이썬이 직접 읽게 한다.
SUMMARY=$(python3 - "$LOG" "$last" "$(pwd)/mcp/server.py" <<'PY'
import json, re, sys
from collections import Counter

log_path, offset, server_src = sys.argv[1], int(sys.argv[2]), sys.argv[3]

# 등록된 도구 이름을 **소스에서 파생**한다(하드코딩 목록은 도구가 늘면 썩는다).
try:
    src = open(server_src, encoding="utf-8").read()
    TOOLS = set(re.findall(r"@server\.tool\([^)]*\)\s*\ndef\s+(\w+)", src))
except OSError:
    TOOLS = set()

CATS = {"wrong_citation", "outdated_law", "wrong_ruling", "tool_error",
        "feature_request", "other"}
SESSION_RE = re.compile(r"^(?:mcp|web|battery)-[A-Za-z0-9_-]{4,40}$")

n = 0
src_c, cat_c, rate_c, tool_c = Counter(), Counter(), Counter(), Counter()
sessions = []
with open(log_path, encoding="utf-8", errors="replace") as fh:
    fresh = fh.read().splitlines()[offset:]
for line in fresh:
    try:
        d = json.loads(line)
    except Exception:
        continue
    if not (d.get("comment") or "").strip():
        continue
    n += 1
    ctx = d.get("context") or {}
    src_c["MCP" if ctx.get("page") == "MCP" else "웹"] += 1
    # category는 우리 서버가 `[MCP:<cat>]` 접두로 붙인다 — 화이트리스트 통과분만 센다
    m = re.match(r"\[MCP:(\w+)\]", (d.get("comment") or ""))
    if m and m.group(1) in CATS:
        cat_c[m.group(1)] += 1
    r = d.get("rating")
    if isinstance(r, int) and 1 <= r <= 5:
        rate_c[r] += 1
    sid = d.get("session_id") or ""
    if SESSION_RE.match(sid):
        sessions.append(sid)
    # 직전호출 기록의 **도구명만** — 등록된 이름과 대조해 통과한 것만(인자·값은 버린다)
    for name in re.findall(r"\b([a-z_]{4,40})\(", d.get("comment") or ""):
        if name in TOOLS:
            tool_c[name] += 1

def fmt(c, unit=""):
    return ", ".join(f"{k} {v}{unit}" for k, v in c.most_common(6)) or "없음"

print(f"신규 {n}건 · 출처 {fmt(src_c)} · 유형 {fmt(cat_c)} · 평점 {fmt(rate_c, '건')}"
      f" · 직전호출 도구 {fmt(tool_c)}"
      f" · 세션 {', '.join(sessions[:5]) if sessions else '없음'}")
PY
)
echo "$total" > "$STATE"
[ -z "$SUMMARY" ] && exit 0
"$REPORT" "계약나침반 피드백 다이제스트 — $SUMMARY. **원문은 인용하지 않는다(무인증 외부 입력)** — 열람은 /data/ops/mcp-feedback.sh contract"
