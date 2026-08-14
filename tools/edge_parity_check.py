#!/usr/bin/env python3
"""엣지↔오리진 패리티 검사 — 같은 로직의 두 번째 구현이 갈렸는지 매일 본다.

**왜 이 검사가 따로 필요한가** (T-2026W33-172, 2026-08-14):
`edge/worker.js`는 판례·해석례(`/api/v1/law/case[s]`)를 **엣지에서 직접 파싱**한다.
uvicorn이 죽어도 판례가 살아 있게 하려는 설계지만, 대가로 같은 로직이 두 곳에 있다.
실제로 갈렸다: 백엔드가 2026-07-30에 고친 "본문 미제공 판례를 빈 필드로 넘기던 결함"과
2026-08-14에 넣은 `source_url`이 엣지에는 없어, **contract.naru.build만 2주 동안
빈 필드를 24시간 캐시로 서빙**했다. 결정론 회귀(tools/mcp_regression.py)는 localhost만
두들기므로 이 갈라짐을 **원리적으로 볼 수 없었고**, 외부 탐침이 우연히 잡았다.

그래서 이 검사는 "결함 하나"가 아니라 **두 구현이 어긋났다는 사실 자체**를 본다.
값이 아니라 계약(응답 키 집합·오류 코드·출처 링크 유무)을 비교한다 — law.go.kr 실시간
데이터라 값은 회차마다 달라질 수 있지만 계약은 달라지면 안 된다.

exit 0=일치 / 1=어긋남 / 2=수집 실패(둘 중 한쪽 미도달 — 네트워크·오리진 장애)
사용: python3 tools/edge_parity_check.py [--edge URL] [--origin URL]
"""
from __future__ import annotations

import argparse
import json
import sys

import httpx

EDGE = "https://contract.naru.build"
ORIGIN = "https://contract.sallim.app"   # 워커 미배선 경로 = 오리진 동작의 대표

# (라벨, 경로, 파라미터) — 엣지가 가로채는 경로만. 값이 아니라 계약을 비교한다.
CASES = [
    ("본문 있는 판례", "/api/v1/law/case", {"kind": "prec", "case_id": "204256"}),
    ("본문 미제공 판례", "/api/v1/law/case", {"kind": "prec", "case_id": "617909"}),
    ("없는 해석례 일련번호", "/api/v1/law/case", {"kind": "expc", "case_id": "999999999"}),
    ("판례 검색", "/api/v1/law/cases", {"q": "부정당업자", "top_k": 2}),
]


def fetch(base: str, path: str, params: dict) -> tuple[object | None, str | None]:
    try:
        r = httpx.get(f"{base}{path}", params=params, timeout=60)
        r.raise_for_status()
        return r.json(), None
    except Exception as e:  # noqa: BLE001 — 수집 실패와 어긋남을 구별해야 한다
        return None, f"{type(e).__name__}: {e}"


def contract_of(payload: object) -> dict:
    """비교 대상 = 응답의 '계약'. 값(제목·본문)은 원천이 갱신하면 달라질 수 있다."""
    if isinstance(payload, list):
        first = payload[0] if payload else {}
        return {"shape": "list",
                "empty": not payload,
                "keys": sorted(first) if isinstance(first, dict) else [],
                "has_source_url": bool(isinstance(first, dict) and first.get("source_url"))}
    if isinstance(payload, dict):
        return {"shape": "object",
                "keys": sorted(payload),
                "error": payload.get("error"),
                "has_source_url": bool(payload.get("source_url")),
                "has_hint": bool(payload.get("hint"))}
    return {"shape": type(payload).__name__}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--edge", default=EDGE)
    ap.add_argument("--origin", default=ORIGIN)
    a = ap.parse_args()
    print(f"# 엣지 {a.edge} ↔ 오리진 {a.origin} · 케이스 {len(CASES)}건")

    diffs = 0
    for label, path, params in CASES:
        e_body, e_err = fetch(a.edge, path, params)
        o_body, o_err = fetch(a.origin, path, params)
        if e_err or o_err:
            print(f"[ERR ] {label} — 수집 실패 (엣지: {e_err or 'ok'} / 오리진: {o_err or 'ok'})")
            return 2
        e_c, o_c = contract_of(e_body), contract_of(o_body)
        if e_c == o_c:
            print(f"[SAME] {label}")
            continue
        diffs += 1
        print(f"[DIFF] {label}")
        for k in sorted(set(e_c) | set(o_c)):
            if e_c.get(k) != o_c.get(k):
                print(f"       {k}: 엣지={json.dumps(e_c.get(k), ensure_ascii=False)} "
                      f"↔ 오리진={json.dumps(o_c.get(k), ensure_ascii=False)}")

    print(f"\n결과: 일치 {len(CASES) - diffs} / 어긋남 {diffs} (총 {len(CASES)})")
    if diffs:
        print("→ edge/worker.js와 backend/api/v1/law.py 중 한쪽만 고친 것이다. "
              "둘 다 고치고 배포(wrangler deploy)한 뒤 캐시를 퍼지하라.")
    return 1 if diffs else 0


if __name__ == "__main__":
    sys.exit(main())
