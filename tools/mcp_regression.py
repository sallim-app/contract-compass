#!/usr/bin/env python3
"""MCP 도구 결정론 회귀 — LLM 없이 도구를 직접 두들겨 구조 성질을 검사한다.

mcp-tool-design §1-4의 2층 평가 중 '크론 가능한 층': LLM 평가(codexw 배터리)는
합성 품질을, 이 하네스는 **수리한 결함의 재발**을 잡는다. 전 케이스가 2026-07-30
실측으로 발견·수리된 결함의 R* 회귀다(미검증 문항 금지 — 스킬 §1-4 함정).

exit 0=전부 PASS / 1=회귀 존재 / 2=수집 실패(MCP 미도달)
사용: python3 tools/mcp_regression.py   (localhost:8403 — 루프백 무제한 티어)

**외부 재현**(2026-08-09, T-2026W32-83): 평가셋이 우리 몫이라면 남이 우리 판정을 되짚을 수
있어야 한다. 엔드포인트를 바꿔 공개 서버에 그대로 돌릴 수 있다 —

    python3 tools/mcp_regression.py --endpoint https://contract.sallim.app/mcp

주의: 공개 엔드포인트는 무료 IP당 50콜/일 한도가 걸리고 이 스위트가 케이스당 1콜씩
쓰므로(현재 28콜) 하루 1회가 한계다. 한도를 넘기면 도구가 구조화 오류를 반환해 FAIL이
아니라 **수집 실패로 보이지 않는 FAIL**이 되니, 판정 전 출력의 오류 메시지를 확인하라.
사람이 읽는 문항지는 저장소 루트 `evaluation.xml`이다.
"""
from __future__ import annotations

import json
import os
import sys

import httpx

MCP = os.environ.get("CC_MCP_ENDPOINT", "http://localhost:8403/mcp")
H = {"Content-Type": "application/json",
     "Accept": "application/json, text/event-stream"}


class Session:
    def __init__(self) -> None:
        self.c = httpx.Client(timeout=60)
        r = self.c.post(MCP, headers=H, content=json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "mcp-regression", "version": "1"}}}))
        r.raise_for_status()
        self.h = dict(H)
        sid = r.headers.get("mcp-session-id")
        if sid:
            self.h["mcp-session-id"] = sid
        self.c.post(MCP, headers=self.h, content=json.dumps(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self._id = 10

    def call(self, tool: str, args: dict) -> dict:
        self._id += 1
        r = self.c.post(MCP, headers=self.h, content=json.dumps({
            "jsonrpc": "2.0", "id": self._id, "method": "tools/call",
            "params": {"name": tool, "arguments": args}}))
        r.raise_for_status()
        line = [l for l in r.text.splitlines() if l.startswith("data:")]
        d = json.loads(line[0][5:]) if line else json.loads(r.text)
        return json.loads(d["result"]["content"][0]["text"])


CASES = [
    # (id, tool, args, 검사 함수 — 2026-07-30 수리 결함과 1:1)
    ("R1-지역제한-150억",       # 룰 3분할·시행규칙 2026.4.24 개정 반영 회귀
     "search_law", {"query": "지방계약법 시행규칙 제24조", "top_k": 3},
     lambda d: any("150억" in h.get("content", "") for h in d.get("hits", []))),
    ("R2-부정당별표-개월수",     # 별표 PDF 적재(fetch_law_tables) 회귀
     "search_references", {"query": "담합한 자 제재기간", "top_k": 6},
     lambda d: any("개월" in h.get("excerpt", "") and "제한기준" in h.get("source", "") + h.get("section", "")
                   for h in d.get("hits", []))),
    ("R3-판례본문",             # 판례 라이브 프록시(law.go.kr·lawproxy) 회귀
     "get_case", {"kind": "prec", "case_id": "204256"},
     lambda d: "우수조달물품" in d.get("issue", "") and "제27조" in d.get("referenced_laws", "")),
    ("R4-긴쿼리-422회귀",        # search_law max_length 50→200 회귀
     "search_law", {"query": "적격심사 낙찰하한율 100억 미만 공사는 몇 퍼센트인지 아주 길게 묻는 검증용 질의문", "top_k": 3},
     lambda d: "error" not in d),
    ("R5-룰엔진-소액수의",       # decide 결정론(skip_llm) 경로 회귀
     "decide_contract_method", {"contract_type": "product", "estimated_price": 15000000,
                                "org_type": "local", "project_name": "회귀검사"},
     lambda d: any("수의" in (c.get("method") or "") for c in d.get("candidates", []))),
    ("R6-지명입찰-동의어",       # 지방계약법령 '지명입찰' 용어차 동의어 확장 회귀
     "search_references", {"query": "지자체 용역 지명경쟁 금액", "top_k": 8},
     lambda d: any("지명입찰" in h.get("excerpt", "") or "제22조" in h.get("excerpt", "")
                   for h in d.get("hits", []))),
    ("R7-절단-가시화",           # search_law 조용한 절단 금지(total_found·note)
     "search_law", {"query": "수의계약", "top_k": 3},
     lambda d: d.get("total_found", 0) >= d.get("count", 0) and
               (d.get("total_found") == d.get("count") or "note" in d)),
    ("R8-지방판정-국가근거혼입",   # decide 지방 3중 결함(국가룰 SVC_002 혼입·왜절 모순·
     "decide_contract_method",   # 20,000,001→"2,000만원" 반올림) 수리 회귀 — 2026-07-30
     {"contract_type": "service", "estimated_price": 20000001,
      "org_type": "local", "project_name": "회귀검사"},
     # 검사 범위는 룰 계층(후보·설명·법령 키)만 — R25(2026-08-12)로 조문 본문(articles)이
     # 배달되면서 지방계약법 시행령 제25조 '원문 자체'가 국가령을 인용하는 게 걸렸는데,
     # 그건 혼입이 아니라 원문 충실이다(원문 인용을 지우는 쪽이 오히려 왜곡).
     lambda d: (lambda t: "국가계약법" not in t and "국가를 당사자" not in t
                and "2,000만원" not in t
                and any((c.get("rule_id") or "").startswith("LOCAL")
                        for c in d.get("candidates", [])))(
                    json.dumps({**d, "laws_applied": [
                        {"key": l.get("key"), "law_name": l.get("law_name")}
                        for l in d.get("laws_applied", [])]}, ensure_ascii=False))),
    ("R9-판례본문미제공-가시화",   # 검색엔 뜨나 본문 미제공 판례가 빈 필드로 침묵하던
     "get_case", {"kind": "prec", "case_id": "417684"},   # 결함(배터리 제보) 수리 회귀
     lambda d: d.get("error") == "case_body_unavailable" and "hint" in d),
    ("R10-전자조달법-수록",       # 나라장터 투찰 질문에서 404였던 전자조달법 3종
     "get_law_article", {"ref": "전자조달법 제7조"},        # 법령팩 수록(배터리 업체-059) 회귀
     lambda d: "전자입찰" in d.get("content", "") and d.get("law_name") == "전자조달법"),
    ("R11-별표-무공백-계약미체결",  # 별표 PDF 무공백 추출로 '계약 미체결' 행이 검색
     "search_references",         # 불능이던 결함(배터리 업체-062) — 공백 복원+700자
     {"query": "계약을 체결 또는 이행하지 않은 자 부정당업자 제재기간", "top_k": 10},  # 청크 회귀
     lambda d: any("별표" in h.get("section", "") and "계약을 체결" in h.get("excerpt", "")
                   for h in d.get("hits", []))),
    ("R12-공사-지방판정-국가룰혼입",  # R8의 공사판 — CST general·PRO·FIRE 12개 룰
     "decide_contract_method",     # national/public_corp 게이트(LOCAL 쌍둥이 완전 커버
     {"contract_type": "construction", "estimated_price": 350000000,  # 검증 후) 회귀
      "org_type": "local", "construction_specialty": "general", "project_name": "회귀검사"},
     lambda d: (lambda rids: any(r.startswith("LOCAL_CST") for r in rids)
                and not any(r.startswith("CST_") for r in rids))(
                    [(c.get("rule_id") or "") for c in d.get("candidates", [])])),
    ("R14-인접법령-하도급60일",    # 인접법령 확장(2026-07-30 12종) 회귀 — 하도급법
     "get_law_article", {"ref": "하도급법 제13조"},  # 제13조 대금지급 60일이 앵커
     lambda d: "60일" in d.get("content", "") and d.get("law_name") == "하도급법"),
    ("R13-전기공사-지방룰",        # 전기·정보통신은 LOCAL 룰 부재로 국가룰(CST_ELEC_*)
     "decide_contract_method",     # 폴백이었음 — LOCAL_CST_*_ELEC/ICT 신설(지방령
     {"contract_type": "construction", "estimated_price": 100000000,  # 25조①5호가목
      "org_type": "local", "construction_specialty": "electrical",    # 1.6억) 회귀
      "project_name": "회귀검사"},
     lambda d: (lambda rids: "LOCAL_CST_NEGO_ELEC" in rids
                and not any(r.startswith("CST_") for r in rids))(
                    [(c.get("rule_id") or "") for c in d.get("candidates", [])])),
    ("R15-지방-물품경쟁-국가수치",  # R8/R12가 통과하는데도 남아 있던 구멍 —
     "decide_contract_method",     # 물품·용역 '경쟁입찰' 대역(고시금액 이상)에서 PRD_001·
     {"contract_type": "product", "estimated_price": 300000000,  # SVC_001 등 국가 적격심사
      "org_type": "local", "project_name": "회귀검사"},          # 룰이 org_type 미지정이라
     lambda d: (lambda rids, kps:                                # 지자체 후보에 혼입, 국가
                any(r.startswith("LOCAL_") for r in rids)        # 통과점수·낙찰하한율이
                and not any(r in {"PRD_001", "PRD_003B", "PRD_006", "PRD_INTL_001"}
                            for r in rids)
                and not any("pass_score" in k or "lower_limit_rate" in k for k in kps))(
                    [(c.get("rule_id") or "") for c in d.get("candidates", [])],
                    [(c.get("key_params") or {}) for c in d.get("candidates", [])])),
    ("R16-지방-용역경쟁-국가수치",  # R15의 용역판 (SVC_001·SVC_007·SVC_GEN_* 게이트)
     "decide_contract_method",
     {"contract_type": "service", "estimated_price": 300000000,
      "org_type": "local", "project_name": "회귀검사"},
     lambda d: (lambda rids, kps:
                any(r.startswith("LOCAL_") for r in rids)
                and not any(r.startswith("SVC_") and not r.startswith("SVC_SME")
                            for r in rids)
                and not any("pass_score" in k or "lower_limit_rate" in k for k in kps))(
                    [(c.get("rule_id") or "") for c in d.get("candidates", [])],
                    [(c.get("key_params") or {}) for c in d.get("candidates", [])])),
    ("R17-국가수치-회귀",          # 위 게이트가 국가·공기업 판정까지 죽이지 않았는지
     "decide_contract_method",     # (과잉 차단 방지 — 국가는 수치가 그대로 나와야 함)
     {"contract_type": "product", "estimated_price": 300000000,
      "org_type": "national", "project_name": "회귀검사"},
     lambda d: any((c.get("key_params") or {}).get("pass_score")
                   and (c.get("key_params") or {}).get("lower_limit_rate")
                   for c in d.get("candidates", []))),
    ("R18-검색순위-근거표시",      # rerank 무산출을 조용히 넘기면 에이전트가 1위 청크를
     "search_references",        # 정답으로 단정한다 — ranking/ranked_by로 근거를 밝히고
     {"query": "적격심사 낙찰하한율", "top_k": 6},   # 미가동이면 hint까지 나와야 한다
     lambda d: d.get("ranking") in ("rerank", "hybrid_rrf")
               and all(h.get("ranked_by") in ("rerank", "hybrid_rrf") for h in d.get("hits", []))
               and (d.get("ranking") != "hybrid_rrf" or "hint" in d)),
    ("R19-별표2-상위회수",        # rerank 미배선 시절 별표2가 relevance 0.6 하드코딩에
     "search_references",        # 묻혀 6위였다(2026-07-30 실측). rerank가 죽으면 검색
     {"query": "부정당업자 제재 계약 미체결 제재기간", "top_k": 6},   # 품질이 실제로
     lambda d: any("부정당업자 제한기준" in (h.get("section") or "")   # 나빠지므로 실패가
                   for h in d.get("hits", [])[:2])),                  # 맞다(은폐 금지)
    ("R20-분할발주-실무어",       # 법령 용어는 '분할계약'이라 실무어 '분할발주'로는
     "search_law",               # 조문이 안 잡히고 동음이의 '지적(地籍) 정리'가 상위로
     {"query": "분할발주 금지", "top_k": 5},          # 나왔다 — glossary aliases 회귀
     lambda d: any("제68조" in (h.get("law_ref") or "") for h in d.get("hits", []))),
    ("R21-무의미질의-0건실토",     # 존재하지 않는 용어에 시맨틱 폴백이 '삭제' 스텁 8건을
     "search_law",               # 근거처럼 반환했다(T-2026W32-184) — 관련성 하한 미달은
     {"query": "존재하지않는법률용어_9f7c2a", "top_k": 8},   # 0건 + 재질의 hint여야 한다
     lambda d: d.get("count") == 0 and not d.get("hits") and "hint" in d),
    ("R22-삭제조문-표시",         # 삭제 스텁 조문이 경고 없이 정상 조문처럼 나오면
     "search_law",               # 에이전트가 근거로 인용한다 — note로 삭제 사실 공시
     {"query": "공유재산법 시행령 제64조", "top_k": 3},
     lambda d: any("삭제" in (h.get("note") or "")
                   for h in d.get("hits", []) if "제64조" in (h.get("law_ref") or ""))),
    ("R23-국가-2천만초과-무조건소액수의금지",  # 국가·공기업 2천만 초과~1억을 가격만 보고
     "decide_contract_method",   # 소액수의로 판정하던 T-2026W33-58 게이트 — 시행령
     {"contract_type": "product", "estimated_price": 50000000,   # 제26조①5호가목은 2천만
      "org_type": "national", "project_name": "회귀검사"},        # 이하만 무조건, 초과는
     lambda d: (lambda cs: bool(cs)                              # 상대방 요건부다
                and "경쟁" in (cs[0].get("method") or "")
                and all(c.get("rule_id") not in ("PRD_005", "SVC_002") for c in cs)
                and any("요건" in (c.get("notes") or "") for c in cs))(
                    d.get("candidates", []))),
    ("R24-국가-소기업요건-수의승격",  # 요건 플래그(is_small_enterprise, 가목3) 충족 시
     "decide_contract_method",     # 요건부 소액수의가 1순위로 — 게이트만 넣고 요건별
     {"contract_type": "product", "estimated_price": 80000000,   # 룰을 안 넣으면 이 구간
      "org_type": "national", "is_small_enterprise": True,       # 후보가 0건이 된다
      "project_name": "회귀검사"},
     lambda d: (lambda cs: bool(cs)
                and cs[0].get("rule_id") == "PRD_NEGO_SMALLBIZ"
                and "소액수의" in (cs[0].get("method") or ""))(
                    d.get("candidates", []))),
    ("R25-조문본문-배달",          # law_pack이 notes를, server.py가 articles를 버려
     "decide_contract_method",    # 경고가 존재해도 배달되지 않던 결함 — 시행령 제26조
     {"contract_type": "service", "estimated_price": 50000000,   # 본문(가목3 '소상공인')이
      "org_type": "national", "project_name": "회귀검사"},        # laws_applied에 실려야
     lambda d: any("소상공인" in (a.get("body") or "")            # 에이전트가 자력 검증
                   for l in d.get("laws_applied", [])            # 가능하다
                   for a in (l.get("articles") or []))),
    ("R26-1억정확값-요건부수의-1순위",  # 정확히 1억원에서만 1순위가 뒤집히던 결함 —
     "decide_contract_method",       # 가목3 원문이 '2천만원 초과 1억원 이하'라 1억
     {"contract_type": "product", "estimated_price": 100000000,   # 정확값도 요건 구간인데
      "org_type": "national", "is_small_enterprise": True,        # PRD_003B(1억 이상)가
      "project_name": "회귀검사"},                                  # priority로 1순위를 뺏어
     lambda d: (lambda cs: bool(cs)                               # 99,999,999원=소액수의,
                and cs[0].get("rule_id") == "PRD_NEGO_SMALLBIZ"   # 100,000,000원=일반경쟁의
                and "소액수의" in (cs[0].get("method") or "")       # 1원짜리 불연속이었다
                # 일반경쟁은 사라지지 않고 2순위 대안으로 남아야 한다
                and any(c.get("rule_id") == "PRD_003B" for c in cs))(
                    d.get("candidates", []))),
    ("R27-코퍼스밖법령-못봄≠없음",   # 이 코퍼스는 공공계약 특화라 민사집행법이 없다.
     "get_law_article",             # 그런데 "제229조 조문을 찾을 수 없습니다"로 답해
     {"ref": "민사집행법 제229조"},   # 에이전트가 '그런 조문 없음'으로 읽고 판례 참조조문을
     lambda d: (d.get("error") == "article_not_found"       # 틀렸다 하거나 자체 지식으로
                and d.get("law_in_corpus") is False         # 폴백하던 결함(T-2026W33-146).
                and "민사집행법" in (d.get("message") or "")   # 법령명이 지워지지 않고
                and len(d.get("corpus_laws") or []) > 0)),  # 조회 가능 법령이 실려야 한다
    ("R28-없는조문-법령은있음",      # R27의 짝 — 같은 404라도 이건 진짜 부존재다.
     "get_law_article",            # 두 상황이 같은 문장을 내면 구별이 불가능해진다.
     {"ref": "국가계약법 시행령 제9999조"},
     lambda d: (d.get("error") == "article_not_found"
                and d.get("law_in_corpus") is True
                and "국가계약법 시행령" in (d.get("message") or ""))),
]


def main() -> int:
    global MCP
    argv = sys.argv[1:]
    if "--endpoint" in argv:
        i = argv.index("--endpoint")
        if i + 1 >= len(argv):
            print("[ERR ] --endpoint 뒤에 URL이 없다")
            return 2
        MCP = argv[i + 1]
    print(f"# 대상 {MCP} · 케이스 {len(CASES)}건")
    try:
        s = Session()
    except Exception as e:  # noqa: BLE001
        print(f"[ERR ] MCP 미도달: {type(e).__name__}: {e}")
        return 2
    fails = 0
    for cid, tool, args, check in CASES:
        try:
            d = s.call(tool, args)
            ok = bool(check(d))
        except Exception as e:  # noqa: BLE001
            ok, d = False, {"exception": f"{type(e).__name__}: {e}"}
        print(f"[{'PASS' if ok else 'FAIL'}] {cid}")
        if not ok:
            fails += 1
            print(f"       └ {json.dumps(d, ensure_ascii=False)[:200]}")
    print(f"\n결과: PASS {len(CASES) - fails} / FAIL {fails} (총 {len(CASES)})")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
