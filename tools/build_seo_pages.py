#!/usr/bin/env python3
"""프로그래매틱 SEO 페이지 생성기 — 롱테일 정적 HTML + sitemap.

왜 정적 생성인가
----------------
프론트엔드는 Vite+React SPA이고 URL로 주소 지정 가능한 페이지가 `/` 하나뿐이다
(라우터 라이브러리 없음). 검색엔진이 "국가기관 용역 5억원 계약방법"으로 찾아올
착지 페이지가 존재하지 않는다. 그래서 `frontend/public/g/` 아래 **JS 없이도 본문이
보이는 정적 HTML**을 생성한다. `npm run build`가 public/을 dist/로 복사하고,
백엔드 catch-all(`backend/main.py` serve_spa)이 `candidate.is_file()`이면 그 파일을
그대로 서빙하므로 백엔드·nginx·CF 변경이 필요 없다.

URL에 `.html`을 붙이는 이유: catch-all은 디렉토리 인덱스를 처리하지 않는다
(`/g/foo/`는 is_file() 실패 → SPA index.html 반환). 확장자 없는 파일은 media_type도
못 맞춘다. `.html`이 유일하게 백엔드 무변경으로 안전한 형태다.

이 생성기가 **모델·사람이 못 보는 것을 대신 보는 지점** (회사 기치: 도구 계층)
-------------------------------------------------------------------------
1. **밴드 일관성 게이트** — 금액구간 페이지는 "4억~7.1억이면 하한율 86.745%"처럼
   *구간 전체*에 대한 주장을 한다. 그래서 구간 안을 실제로 샘플링해 판정이 바뀌지
   않는지 확인하고, 바뀌면 그 밴드를 **쪼개거나 발행하지 않는다**.
   실측(2026-08-06): 룰 `conditions`의 임계값만 모으면 용역 4억~7.1억이 한 밴드가
   되는데 하한율은 5억에서 86.745%→85.495%로 바뀐다(5억은 `pass_score_by_amount`
   키에만 있는 경계). 그대로 발행하면 대외 페이지에 틀린 요율을 박제하는 것이고,
   이는 커밋 8151f15가 고친 P0(같은 질의에 87.495%와 89.745%가 함께 나온 사고)와
   같은 계열이다. 그래서 경계 수집원을 conditions + pass_score_by_amount +
   method_by_amount 전부로 넓히고, 그 위에 샘플 검증을 둔다.
2. **근거 없으면 생성 거부** — `legal_basis`가 비면 그 페이지를 만들지 않는다.
   "답만 있고 근거 없는 페이지"는 이 제품이 존재하는 이유(감사 대비)와 반대다.
3. **판정의 진실원은 라이브와 동일** — 페이지 숫자를 여기서 다시 계산하지 않는다.
   `RuleEngine.match()` / `get_pass_score()` / `build_decision_pack()`을 그대로
   호출한다. 라이브 API와 같은 코드를 통과한 값만 페이지에 들어가므로 랜딩↔라이브
   불일치가 구조적으로 불가능하다. (공개 GET `/api/v1/rules-public/list`는
   `pass_score_ref`를 해석하지 않아 CST_001·CST_006·CST_007·SVC_001이 null로
   나온다 — 그 경로로 뽑으면 요율이 빈칸이 된다. 반드시 엔진 경유.)
4. **금지 문구 게이트** — `docs/POSITIONING.md`가 금지한 표현("자동 판정",
   "검증됨", "○분 절약" 류)이 생성물에 있으면 실패로 종료한다.

발행하지 않는 것 (근거 부재 — 못 봄 ≠ 없음)
-------------------------------------------
- **지자체 낙찰하한율 상세**: `rules/local_award_criteria.json`은 레코드에
  `"공종": "확인필요"`, `"주의": "…별표 대조 필요(구현단계)"`가 박힌 미완성 자료이고
  런타임 소비처가 없다. 요율 페이지를 만들 근거가 아니다.
- **부정당업자 제재 판정**: 룰엔진 판정 대상이 아니다(POSITIONING §4-4). 제재기준
  별표 원문 XML은 .gitignore돼 공개 리포에 없다. 그래서 "판정" 페이지 대신
  **근거 조회 안내** 1페이지만 낸다.

사용법
------
    python3 tools/build_seo_pages.py            # 생성
    python3 tools/build_seo_pages.py --check    # 파일을 쓰지 않고 검증만
    python3 tools/build_seo_pages.py --strict   # 게이트 위반이 있으면 rc=1

생성 후 라이브 반영: `cd frontend && npm run build` (FastAPI가 dist를 요청마다
읽으므로 재시작 불필요). 색인 제출은 `tools/indexnow_ping.py` — **라이브 반영 후**에만.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# 경량 import만 쓴다(chromadb·numpy를 끌고 오는 API 모듈을 건드리지 않는다) —
# 그래야 이 생성기와 그 게이트 테스트가 CI 경량 환경에서 그대로 돈다.
from backend.services.law_pack import (  # noqa: E402
    LAW_REGISTRY,
    build_decision_pack,
    resolve_registry_keys,
)
from backend.services.rule_engine import RuleEngine, rule_method  # noqa: E402

_rule_method = rule_method

SITE = "https://contract.sallim.app"
OUT_DIR = BASE / "frontend" / "public" / "g"
SITEMAP = BASE / "frontend" / "public" / "sitemap.xml"
RULES_PATH = BASE / "rules" / "contract_rules.json"
GLOSSARY_PATH = BASE / "data" / "glossary.json"

# 템플릿·문구를 고칠 때 **손으로 올려라.** sitemap `lastmod`는 룰셋 기준일과 이 값 중
# 늦은 쪽을 쓴다. 룰셋 기준일만 쓰면 템플릿·카피만 바뀐 개정이 크롤러에게 "변경 없음"으로
# 보인다. 파일 mtime을 쓰지 않는 이유: git이 mtime을 보존하지 않아 체크아웃마다 sitemap이
# 달라지고, 그러면 아래 디스크 검증(--check)이 매번 거짓 드리프트를 낸다.
TEMPLATE_REVISED = "2026-08-06"

UMAMI = (
    '<script defer src="https://analytics.naru.build/script.js" '
    'data-website-id="7c6aa9f8-3bee-4a18-ae4d-0c6467c355fc" '
    'data-domains="contract.sallim.app,contract.naru.build"></script>'
)

# README 면책과 **동일 문구**를 유지한다(POSITIONING: 문서·랜딩·라이브 간 불일치 금지).
DISCLAIMER = (
    "이 서비스는 정보 제공 목적이며 법적 자문·유권해석이 아닙니다. 적격심사 통과점수·"
    "낙찰하한율·각종 한도는 발주기관별 세부기준과 법령 개정에 따라 다를 수 있으므로, "
    "실제 발주 전 반드시 소속 기관 계약 부서와 현행 법령을 확인하세요."
)
# POSITIONING §4-3: 이 단서를 떼지 마라.
RATE_CAVEAT = "적격심사 통과점수·낙찰하한율은 발주기관별 세부기준·법령 개정에 따라 다를 수 있습니다."

# POSITIONING이 금지한 표현 — 생성물에 있으면 빌드 실패.
FORBIDDEN = ["자동 판정", "검증됨", "분 절약", "100% 정확", "법적 효력이", "유권해석입니다"]

CT_LABEL = {"construction": "공사", "service": "용역", "product": "물품"}
ORG_LABEL = {"national": "국가기관", "local": "지방자치단체", "public_corp": "공기업·준정부기관"}
ORG_ROMAN = {"national": "gukga", "local": "jibang", "public_corp": "gonggieop"}
CT_ROMAN = {"construction": "gongsa", "service": "yongyeok", "product": "mulpum"}
REASON_LABEL = {
    "urgent": "긴급 필요",
    "rebid_failure": "재공고 유찰",
    "rebid": "재공고 유찰(지방계약법)",
    "technical_difficulty": "기술 곤란·불가분",
    "patent_new_tech": "특허·신기술",
    "specific_person": "특정인 계약",
    "small_repeat": "소액(경쟁 비효율)",
    "other_justified": "기타 정당화 사유",
}
# 계약유형별 페이지 가정 — 페이지 본문에 반드시 노출한다(인식 경계 계약).
CT_ASSUMPTION = {
    "construction": "종합공사(일반건설업) 기준입니다. 전기·정보통신·소방시설·문화재수리 등 개별 법령 공사는 업역과 적용 기준이 달라 결과가 바뀝니다.",
    "service": "용역 구분(기술·학술·시설·정보화)을 지정하지 않은 일반 용역 기준입니다. 구분에 따라 적격심사 기준이 달라질 수 있습니다.",
    "product": "중소기업자간 경쟁제품이 **아닌** 물품 기준입니다. 경쟁제품이면 중소기업자간 제한경쟁이 우선 적용됩니다.",
}

# ── 한글 → ASCII 슬러그(개정 로마자, 음운변화 미적용) ───────────────────────────
# ASCII 슬러그를 쓰는 이유: 한글 URL은 CF Worker→nginx→uvicorn 3단을 지나며 퍼센트
# 인코딩이 보존되는지를 **라이브에 올리기 전엔 검증할 수 없다**. 검증 못 하는 것을
# 대외 URL로 박제하지 않는다. 한글 키워드는 title·h1·description에 들어간다(순위에
# 실제로 기여하는 자리).
_CHO = ["g", "kk", "n", "d", "tt", "r", "m", "b", "pp", "s", "ss", "", "j", "jj", "ch", "k", "t", "p", "h"]
_JUNG = ["a", "ae", "ya", "yae", "eo", "e", "yeo", "ye", "o", "wa", "wae", "oe", "yo",
         "u", "wo", "we", "wi", "yu", "eu", "ui", "i"]
_JONG = ["", "k", "k", "k", "n", "n", "n", "t", "l", "k", "m", "l", "l", "l", "p", "l",
         "m", "p", "p", "t", "t", "ng", "t", "t", "k", "t", "p", "t"]


def romanize(text: str) -> str:
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if 0xAC00 <= code <= 0xD7A3:
            idx = code - 0xAC00
            out.append(_CHO[idx // 588])
            out.append(_JUNG[(idx % 588) // 28])
            out.append(_JONG[idx % 28])
        elif ch.isalnum():
            out.append(ch.lower())
        else:
            out.append("-")
    return re.sub(r"-+", "-", "".join(out)).strip("-") or "x"


def won(v: int) -> str:
    """금액 사람표기. 절삭으로 경계값이 가려지지 않게 한다(law_pack과 같은 원칙).

    `%g` 같은 유효자릿수 표기를 쓰면 400,000,001원이 "4억원"으로 표기돼 **바로 아래
    밴드의 상한과 같은 글자**가 된다("4억원 이하 소액수의" 페이지와 "4억원 초과
    일반경쟁" 페이지가 둘 다 '4억원'을 말하게 된다). 그래서 나누어떨어지는 단위일
    때만 억/만원으로 줄이고, 아니면 원 단위 전액을 적는다.
    """
    if v < 100_000_000:
        return f"{v // 10_000:,}만원" if v % 10_000 == 0 else f"{v:,}원"
    if v % 100_000_000 == 0:
        return f"{v // 100_000_000:,}억원"
    if v % 10_000_000 == 0:
        return f"{v / 100_000_000:g}억원"
    if v % 10_000 == 0:
        return f"{v // 10_000:,}만원"
    return f"{v:,}원"


def is_local_rule(rule: dict) -> bool:
    """지방계약법 전용 룰인가.

    이 구분이 필요한 이유: `resolve_registry_keys(..., include_method_defaults=True)`는
    계약방법 이름으로 **국가계약법** 기본 조문(제7조·시행령 제26조·제42조)을 덧붙인다.
    지방 룰에 그걸 붙이면 룰이 인용하지 않은 법을 근거로 표시하게 된다 — 지자체
    담당자에게 적용 법령을 거짓으로 알려주는 것이다. `build_decision_pack`이 이미
    `include_method_defaults=not _is_local`로 같은 구분을 한다.
    """
    org = rule.get("org_type")
    if isinstance(org, list):
        return set(org) == {"local"}
    return org == "local"


def _clean(v: int) -> bool:
    """만원 단위로 떨어지는 금액인가(사람이 읽는 경계로 쓸 수 있는가)."""
    return v % 10_000 == 0


def range_text(lo: int, hi: int | None) -> str:
    """밴드를 사람이 읽는 구간 문구로. 경계는 법문 어법(이상/초과/미만/이하)대로.

    `[400,000,001, ∞)`을 "400,000,001원 이상"이라 쓰지 않고 "4억원 초과"라 쓴다 —
    같은 사실을 실무자가 쓰는 말로 적으면서 경계를 흐리지 않는다.
    """
    # 한 금액만 해당하는 밴드(경계값 단독) — "1억원 이상 1억원 이하"는 사람이 읽는 말이
    # 아니다. 경계값 판정이 위아래와 다르다는 사실이 요점이므로 그렇게 적는다.
    if hi is not None and lo == hi:
        return f"{won(lo)} 정확히(경계값)"
    if lo <= 1:
        lower = ""
    elif _clean(lo):
        lower = f"{won(lo)} 이상"
    else:
        lower = f"{won(lo - 1)} 초과"
    if hi is None:
        return lower or "전 구간"
    nxt = hi + 1
    upper = f"{won(nxt)} 미만" if _clean(nxt) else f"{won(hi)} 이하"
    return f"{lower} {upper}".strip()


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def rate_pct(v) -> str:
    return f"{v * 100:.3f}%".rstrip("0").rstrip(".") + "%" if False else f"{v * 100:.3f}%"


CSS = """*{box-sizing:border-box}body{margin:0;font:16px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans KR",sans-serif;color:#1a2233;background:#fff}
.wrap{max-width:760px;margin:0 auto;padding:20px 18px 64px}a{color:#1552b8}nav.bc{font-size:13px;color:#667;margin:8px 0 18px}nav.bc a{color:#667}
h1{font-size:25px;line-height:1.35;margin:6px 0 14px}h2{font-size:19px;margin:30px 0 10px;padding-top:14px;border-top:1px solid #e6e9ef}
h3{font-size:16px;margin:20px 0 6px}.lede{font-size:17px;color:#22304d;background:#f5f8ff;border-left:3px solid #1552b8;padding:12px 14px;border-radius:0 6px 6px 0}
table{width:100%;border-collapse:collapse;margin:12px 0;font-size:15px}th,td{border:1px solid #e0e4ec;padding:8px 10px;text-align:left;vertical-align:top}
th{background:#f7f9fc;font-weight:600}dl{margin:10px 0}dt{font-weight:600;margin-top:14px}dd{margin:4px 0 0}
.law{background:#fafbfd;border:1px solid #e6e9ef;border-radius:6px;padding:12px 14px;margin:10px 0;font-size:14.5px;white-space:pre-wrap}
.law b{display:block;margin-bottom:5px;font-size:14px;color:#334}.cta{display:inline-block;background:#1552b8;color:#fff;padding:11px 18px;border-radius:6px;text-decoration:none;font-weight:600;margin:8px 0}
.caveat{font-size:14px;color:#6b4e0a;background:#fffbea;border:1px solid #f0e3b4;border-radius:6px;padding:10px 12px;margin:12px 0}
footer{margin-top:38px;padding-top:16px;border-top:1px solid #e6e9ef;font-size:13px;color:#667}
ul.links{padding-left:0;list-style:none;columns:2;column-gap:24px}ul.links li{margin:5px 0;break-inside:avoid}
@media(max-width:560px){ul.links{columns:1}h1{font-size:22px}}"""


class Page:
    def __init__(self, slug, title, desc, h1, body, breadcrumb, jsonld=None, priority="0.6"):
        self.slug, self.title, self.desc, self.h1 = slug, title, desc, h1
        self.body, self.breadcrumb = body, breadcrumb
        self.jsonld = jsonld or []
        self.priority = priority

    def render(self, asof: str) -> str:
        url = f"{SITE}/g/{self.slug}.html"
        bc = " › ".join(f'<a href="{esc(u)}">{esc(n)}</a>' if u else esc(n)
                        for n, u in self.breadcrumb)
        ld = [{
            "@context": "https://schema.org",
            "@type": "BreadcrumbList",
            "itemListElement": [
                {"@type": "ListItem", "position": i + 1, "name": n,
                 **({"item": SITE + u} if u else {})}
                for i, (n, u) in enumerate(self.breadcrumb)
            ],
        }] + self.jsonld
        ld_html = "\n".join(
            f'<script type="application/ld+json">{json.dumps(o, ensure_ascii=False)}</script>'
            for o in ld)
        return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(self.title)}</title>
<meta name="description" content="{esc(self.desc)}">
<link rel="canonical" href="{url}">
<meta property="og:type" content="article">
<meta property="og:title" content="{esc(self.title)}">
<meta property="og:description" content="{esc(self.desc)}">
<meta property="og:url" content="{url}">
<meta property="og:locale" content="ko_KR">
<meta property="og:site_name" content="계약나침반">
<style>{CSS}</style>
{ld_html}
{UMAMI}
</head>
<body>
<div class="wrap">
<nav class="bc">{bc}</nav>
<h1>{esc(self.h1)}</h1>
{self.body}
<footer>
<p><b>기준 자료</b> — 룰셋 기준일 {esc(asof)} · 조문 원문은 국가법령정보센터(law.go.kr) 등재본. 이 페이지의 판정값은 계약나침반 룰엔진이 라이브 서비스와 같은 코드로 계산한 것입니다.</p>
<p>⚠️ {esc(DISCLAIMER)}</p>
<p><a href="/">계약나침반 홈</a> · <a href="/g/index.html">가이드 목차</a></p>
</footer>
</div>
</body>
</html>
"""


def faq_ld(qa: list[tuple[str, str]]) -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": q,
             "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in qa
        ],
    }


class Builder:
    def __init__(self) -> None:
        self.engine = RuleEngine(str(RULES_PATH))
        self.rules = self.engine._data.get("rules", [])
        self.asof = self.engine._data.get("last_updated", "미표기")
        self.glossary = json.loads(GLOSSARY_PATH.read_text(encoding="utf-8"))
        self.skipped: list[str] = []
        self.pages: list[Page] = []
        self._band_cache: dict[tuple[str, str], list] = {}

    # ── 금액 경계 수집 ────────────────────────────────────────────────────────
    def breakpoints(self) -> list[int]:
        """판정이 바뀔 수 있는 모든 금액 경계(= 새 밴드가 시작하는 금액).

        두 번 틀릴 수 있는 자리다.
        1. conditions만 모으면 부족하다 — 요율·방법 구간표(`*_by_amount`) 키에만 있는
           경계(예: 용역 5억)를 놓쳐 한 밴드 안에서 하한율이 바뀐다.
        2. **'이하/초과'는 T가 아니라 T+1에서 바뀐다.** `estimated_price_lte: 4억`인
           룰은 정확히 4억까지 매칭되므로 판정이 바뀌는 금액은 400,000,001원이다.
           T만 넣으면 [4억, 5억) 밴드의 첫 금액에서만 소액수의고 그 위는 일반경쟁인
           밴드가 만들어진다 — 룰엔진이 2026-07-31에 고친 '초과 vs 이상' 결함과
           같은 계열의 실수를 페이지 쪽에서 되풀이하는 것이다.
        """
        pts: set[int] = set()
        for r in self.rules:
            for k, v in (r.get("conditions") or {}).items():
                if not (k.startswith("estimated_price") and isinstance(v, int)):
                    continue
                if k in ("estimated_price_gte", "estimated_price_lt"):
                    pts.add(v)          # T에서 바뀐다(이상 / 미만)
                elif k in ("estimated_price_lte", "estimated_price_gt"):
                    pts.add(v + 1)      # T+1에서 바뀐다(이하 / 초과)
                else:
                    pts.update((v, v + 1))
            res = r.get("result") or {}
            for mapname in ("pass_score_by_amount", "method_by_amount"):
                for key in (res.get(mapname) or {}):
                    try:
                        pts.add(int(str(key).split("_", 1)[1]))
                    except (IndexError, ValueError):
                        continue
        # `gte_0` 같은 하단 개방 구간은 경계가 아니다 — 넣으면 빈 밴드(1~-1)가 생긴다.
        return sorted(p for p in pts if p > 1)

    def verdict(self, ct: str, org: str, price: int):
        params = {"contract_type": ct, "estimated_price": price}
        if ct == "construction":
            params["construction_specialty"] = "general"
        matched = self.engine.match(params, org_type=org)
        if not matched:
            return None
        top = matched[0]
        ps = self.engine.get_pass_score(top, price) or {}
        return {
            "rule": top,
            "method": _rule_method(top, price),
            "pass_score": ps.get("pass_score"),
            "lower_limit_rate": ps.get("lower_limit_rate"),
            "byeolpyo": ps.get("byeolpyo"),
            "matched_ids": [r["rule_id"] for r in matched],
            "others": matched[1:],
        }

    @staticmethod
    def _sig(v) -> tuple:
        """밴드를 가르는 기준 — 1순위 룰의 표시값 **+ 매칭된 룰 집합 전체**.

        1순위 룰만 보면(2026-08-06 codex 3차 지적) 새 의무가 후순위로 붙는 경계에서
        밴드가 병합된다. 실측: 용역 7.1억에서 `SVC_INTL_001`(국제입찰), 공사 265억에서
        `CST_INTL_001`이 후보에 추가되는데 1순위는 SVC_001·CST_001로 그대로다 — 서명에서
        빠지면 "5억 이상 10억 미만" 한 페이지가 국제입찰 의무를 통째로 삼킨다.
        후보 집합이 바뀌면 답이 바뀐 것이다.
        """
        if v is None:
            return ()
        return (v["rule"]["rule_id"], v["method"], v["pass_score"],
                v["lower_limit_rate"], v["byeolpyo"], tuple(v["matched_ids"]))

    def bands(self, ct: str, org: str) -> list[tuple[int, int | None, dict]]:
        """판정이 일정한 금액 밴드 목록. 밴드 안에서 판정이 흔들리면 발행하지 않는다."""
        cached = self._band_cache.get((ct, org))
        if cached is not None:
            return cached
        # 임계값 t는 그 자체가 새 밴드의 **시작**이다(`_gte`는 t에서 판정이 바뀐다).
        # 따라서 밴드는 [1, t1-1], [t1, t2-1], …, [tn, ∞). 경계값을 밴드 밖으로
        # 흘리면 (1) 그 금액에 해당하는 페이지가 없고 (2) 인접 밴드 병합이 깨진다.
        starts = [1] + self.breakpoints()
        raw: list[tuple[int, int | None, dict]] = []
        for i, lo in enumerate(starts):
            hi = (starts[i + 1] - 1) if i + 1 < len(starts) else None
            v_lo = self.verdict(ct, org, lo)
            probe_hi = hi if hi else lo * 4
            v_hi = self.verdict(ct, org, probe_hi)
            mid = (lo + probe_hi) // 2
            v_mid = self.verdict(ct, org, mid)
            if self._sig(v_lo) != self._sig(v_hi) or self._sig(v_lo) != self._sig(v_mid):
                self.skipped.append(
                    f"밴드 불일치로 발행 제외: {ct}/{org} {lo:,}~{hi and hi or '∞'} "
                    f"(lo={self._sig(v_lo)} mid={self._sig(v_mid)} hi={self._sig(v_hi)})")
            elif v_lo is not None:
                raw.append((lo, hi, v_lo))
        merged: list[list] = []
        for lo, hi, v in raw:
            if merged and self._sig(merged[-1][2]) == self._sig(v) and merged[-1][1] == lo - 1:
                merged[-1][1] = hi
            else:
                merged.append([lo, hi, v])
        out = [(m[0], m[1], m[2]) for m in merged]
        self._band_cache[(ct, org)] = out
        return out

    # ── 근거 조문 ─────────────────────────────────────────────────────────────
    def law_html(self, legal_basis: list[str], method: str = "", limit: int = 4) -> str:
        """근거 조문 전문 블록 + **무엇을 못 실었는지 정직 공시**.

        페이지 길이 때문에 전문을 limit건까지만 싣는다. 그 사실을 적지 않으면
        "인용된 근거 = 여기 실린 조문 전부"로 읽힌다 — 도구가 사용자에게 총계를
        숨기는 것(회사 기치가 금지하는 바로 그 패턴)이다. 그래서 ①총 몇 건 중 몇 건인지
        ②전문을 못 붙인 근거가 무엇인지를 페이지에 남긴다.
        """
        keys = resolve_registry_keys(legal_basis, method, include_method_defaults=bool(method))
        articles: list[tuple[str, str, str]] = []
        for k in keys:
            entry = LAW_REGISTRY.get(k)
            if not entry:
                continue
            for art in entry.get("articles", []):
                articles.append((f"{entry.get('law_name', k)} {art.get('title', '')}".strip(),
                                 entry.get("promulgation", ""), art.get("body", "")))
        # 인용은 했는데 registry에 조문 전문이 없는 근거(예: 예규·세부기준·고시)
        no_text = [b for b in (legal_basis or [])
                   if not any(LAW_REGISTRY.get(k, {}).get("articles")
                              for k in resolve_registry_keys([b], "", include_method_defaults=False))]
        out = []
        for head, promul, body in articles[:limit]:
            out.append(
                f'<div class="law"><b>{esc(head)}'
                + (f' <span style="font-weight:400;color:#778">{esc(promul)}</span>' if promul else "")
                + f"</b>{esc(body)}</div>")
        notes = []
        if len(articles) > limit:
            notes.append(f"조문 전문은 관련 {len(articles)}건 중 {limit}건만 실었습니다.")
        if no_text:
            notes.append("전문을 함께 싣지 못한 근거: " + ", ".join(no_text)
                         + " (예규·세부기준·고시는 원문 소관 기관 공고를 확인하세요).")
        if not articles:
            notes.append("이 근거의 조문 전문은 이 페이지에 포함되지 않았습니다.")
        if notes:
            out.append('<p style="font-size:14px;color:#667">' + esc(" ".join(notes))
                       + " 서비스의 법령 조회는 현행 조문과 특정 시점 시행본을 분리해 제공합니다.</p>")
        return "\n".join(out)

    @staticmethod
    def basis_list(legal_basis: list[str]) -> str:
        return "<ul>" + "".join(f"<li>{esc(b)}</li>" for b in legal_basis) + "</ul>"

    # ── 금액구간 × 계약유형 × 기관유형 ────────────────────────────────────────
    def build_method_pages(self) -> None:
        for ct in ("construction", "service", "product"):
            for org in ("national", "local", "public_corp"):
                bands = self.bands(ct, org)
                slugs = [(lo, hi, f"method-{CT_ROMAN[ct]}-{ORG_ROMAN[org]}-{lo}") for lo, hi, _ in bands]
                for (lo, hi, v), (_, _, slug) in zip(bands, slugs):
                    rule = v["rule"]
                    basis = rule.get("legal_basis") or []
                    if not basis:
                        self.skipped.append(f"근거 없어 발행 제외: {rule.get('rule_id')} ({ct}/{org})")
                        continue
                    self.pages.append(self._method_page(ct, org, lo, hi, v, slug, slugs))

    def _method_page(self, ct, org, lo, hi, v, slug, slugs) -> Page:
        ctl, orgl = CT_LABEL[ct], ORG_LABEL[org]
        rule, res = v["rule"], v["rule"].get("result", {})
        rng = range_text(lo, hi)
        rng_short = rng
        title = f"{orgl} {ctl} 추정가격 {rng} 계약방법 — {v['method']} | 계약나침반"
        desc = (f"{orgl}이 발주하는 {ctl} 추정가격 {rng}일 때 적용 가능한 계약방법은 "
                f"{v['method']}입니다. 근거 조문과 적격심사 기준을 함께 확인하세요.")

        rows = [("계약방법", v["method"])]
        if res.get("bidder_selection"):
            rows.append(("낙찰자 선정", res["bidder_selection"]))
        if v["pass_score"] is not None:
            rows.append(("적격심사 통과점수", f"{v['pass_score']}점"
                         + (f" ({v['byeolpyo']})" if v.get("byeolpyo") else "")))
        if v["lower_limit_rate"] is not None:
            rows.append(("낙찰하한율", f"{v['lower_limit_rate'] * 100:.3f}%"))
        if res.get("min_quotes"):
            # "2인 이상"은 룰의 기본값이고, 하위 금액대의 예외(2천만원 이하 1인 견적 등)는
            # 룰의 `notes`에만 있다. 표만 보고 단정하지 않게 주석을 함께 싣는다(아래 참조).
            rows.append(("최소 견적서", f"{res['min_quotes']}인 이상 (예외는 아래 '룰 주석' 참조)"))
        if res.get("electronic_required_gt"):
            rows.append(("전자입찰(견적)", f"{won(res['electronic_required_gt'])} 초과 시 전자조달시스템 이용"))
        if res.get("procurement_agency"):
            rows.append(("조달 방식", res["procurement_agency"]))
        table = ("<table><tbody>" + "".join(
            f"<tr><th>{esc(k)}</th><td>{esc(val)}</td></tr>" for k, val in rows)
            + "</tbody></table>")

        # 룰 주석(`notes`)은 표의 구조화 값이 담지 못한 **조건·예외**를 담고 있다.
        # 실측(2026-08-06 codex 교차검증 지적): CST_005는 min_quotes=2지만 notes에
        # "2천만원 이하: 1인 견적 가능"이 있고, PRD_006은 통과점수·하한율이 발주기관
        # 세부기준에 따라 다를 수 있다고 적어 둔다. 주석을 떨어뜨리면 페이지가 룰보다
        # 더 단정적인 말을 하게 된다 — 룰의 유보를 페이지가 삭제하는 셈이다.
        notes_html = ""
        if rule.get("notes"):
            notes_html = ('<h3>룰 주석 — 이 판정에 붙는 조건·예외</h3>'
                          f'<p class="caveat">{esc(rule["notes"])}</p>')
        pack = build_decision_pack(rule, ct, max(lo, 1))
        why = pack.get("human_explanation") or ""
        why = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc(why))

        alts = [a for a in (res.get("alternatives") or []) if isinstance(a, dict)]
        alt_html = ""
        if alts:
            alt_html = "<h2>대신 선택할 수 있는 방법</h2><dl>" + "".join(
                f"<dt>{esc(a.get('method',''))}</dt><dd>{esc(a.get('reason',''))}</dd>"
                for a in alts) + "</dl>"

        # 이 구간에서 **함께 매칭된 다른 룰** — 국제입찰(WTO-GPA)·공공구매 의무처럼
        # 1순위 방법과 별개로 붙는 의무가 여기 있다. 1순위만 싣고 나머지를 버리면
        # 페이지가 의무를 삭제한다(2026-08-06 codex 3차 지적).
        others_html = ""
        if v.get("others"):
            items = []
            for o in v["others"]:
                om = _rule_method(o, lo)
                items.append(
                    f"<dt>{esc(o.get('name') or om)}</dt><dd>{esc(om)}"
                    + (f" · 근거: {esc(', '.join((o.get('legal_basis') or [])[:2]))}"
                       if o.get("legal_basis") else "")
                    + (f"<br><span style='font-size:13.5px;color:#556'>{esc(o.get('notes'))}</span>"
                       if o.get("notes") else "")
                    + "</dd>")
            others_html = ("<h2>이 구간에 함께 적용되는 룰</h2>"
                           "<p>1순위 계약방법과 별개로 이 추정가격 구간에서 함께 검토해야 하는 "
                           "규정입니다.</p><dl>" + "".join(items) + "</dl>")

        # 내부 링크: 같은 조합의 다른 금액 구간 + 같은 금액의 다른 기관유형
        siblings = [f'<li><a href="/g/{s}.html">추정가격 {esc(range_text(l, h))}</a></li>'
                    for l, h, s in slugs if s != slug]
        others = []
        for o in ("national", "local", "public_corp"):
            if o == org:
                continue
            ob = self.bands(ct, o)
            hit = next((b for b in ob if b[0] <= lo and (b[1] is None or lo <= b[1])), None)
            if hit and (hit[2]["rule"].get("legal_basis")):
                s = f"method-{CT_ROMAN[ct]}-{ORG_ROMAN[o]}-{hit[0]}"
                others.append(f'<li><a href="/g/{s}.html">{esc(ORG_LABEL[o])} {esc(ctl)} '
                              f'{esc(range_text(hit[0], hit[1]))} → {esc(hit[2]["method"])}</a></li>')

        q = f"{orgl}이 발주하는 {ctl}, 추정가격이 {rng}이면 계약방법은 무엇인가요?"
        a = (f"{v['method']}입니다. 근거는 {', '.join((rule.get('legal_basis') or [])[:2])}"
             f"이며, 발주기관별 세부기준·법령 개정에 따라 달라질 수 있습니다.")

        body = f"""
<p class="lede">{esc(orgl)}이 발주하는 <b>{esc(ctl)}</b>, 추정가격 <b>{esc(rng)}</b>이면
적용 가능한 계약방법은 <b>{esc(v['method'])}</b>입니다.</p>
<h2>판정 요약</h2>
{table}
{notes_html}
<p class="caveat">{esc(RATE_CAVEAT)} 또한 이 페이지는 {esc(CT_ASSUMPTION[ct].replace('**', ''))}</p>
<h2>왜 이 방법인가</h2>
<p>{why}</p>
<h2>근거 조문</h2>
{self.basis_list(rule.get('legal_basis') or [])}
{self.law_html(rule.get('legal_basis') or [], "" if is_local_rule(rule) else v['method'])}
{alt_html}
{others_html}
<h2>내 사안으로 확인하기</h2>
<p>추정가격·기관유형·수의계약 사유 등 실제 조건을 넣으면 룰엔진이 같은 근거로 후보를 제시합니다.
같은 입력에는 항상 같은 결과가 나옵니다(계약방법 판정에 LLM을 쓰지 않습니다).</p>
<p><a class="cta" href="/#decide">계약방법 판정 시작하기</a></p>
<h2>같은 조건의 다른 금액 구간</h2>
<ul class="links">{''.join(siblings) or '<li>이 조합은 구간이 하나입니다.</li>'}</ul>
{'<h2>다른 기관유형은 어떻게 다른가</h2><ul class="links">' + ''.join(others) + '</ul>' if others else ''}
"""
        return Page(slug, title, desc,
                    f"{orgl} {ctl} 추정가격 {rng_short} — 계약방법은 {v['method']}",
                    body,
                    [("계약나침반", "/"), ("가이드", "/g/index.html"),
                     (f"{orgl} {ctl}", ""), (rng_short, "")],
                    [faq_ld([(q, a)])], priority="0.8")

    # ── 수의계약 사유별 ───────────────────────────────────────────────────────
    def build_reason_pages(self) -> None:
        reasons: dict[str, list[dict]] = {}
        for r in self.rules:
            rn = (r.get("conditions") or {}).get("negotiation_reason")
            if rn:
                reasons.setdefault(rn, []).append(r)
        for rn, rs in sorted(reasons.items()):
            rs = [r for r in rs if r.get("legal_basis")]
            if not rs:
                self.skipped.append(f"근거 없어 발행 제외: 수의계약 사유 {rn}")
                continue
            label = REASON_LABEL.get(rn, rn)
            # 슬러그는 사유 **키**에서 만든다 — 라벨에서 만들면 `rebid`(지방)과
            # `rebid_failure`가 같은 슬러그로 충돌한다.
            slug = "sujeui-" + rn.replace("_", "-")
            title = f"수의계약 사유 — {label} 요건과 근거 조문 | 계약나침반"
            desc = (f"{label}을 사유로 한 수의계약이 가능한지, 계약유형별로 어떤 근거 조문과 "
                    f"요건이 붙는지 정리했습니다. 사유서·증빙 요건까지 확인하세요.")
            rows = []
            for r in sorted(rs, key=lambda x: x.get("contract_type", "")):
                res = r.get("result", {})
                rows.append(
                    f"<tr><th>{esc(CT_LABEL.get(r.get('contract_type'), r.get('contract_type')))}"
                    + (f"<br><span style='font-weight:400;font-size:13px;color:#778'>{esc(ORG_LABEL[r['org_type']])} 전용</span>"
                       if r.get("org_type") else "")
                    + f"</th><td><b>{esc(res.get('method', '') or _rule_method(r))}</b>"
                    + (f"<br>{esc(res.get('bidder_selection'))}" if res.get("bidder_selection") else "")
                    + (f"<br>최소 견적 {esc(res.get('min_quotes'))}인 이상" if res.get("min_quotes") else "")
                    + f"<br><span style='font-size:13.5px;color:#556'>근거: {esc(', '.join(r.get('legal_basis') or []))}</span>"
                    + (f"<br><span style='font-size:13.5px;color:#556'>{esc(r.get('notes'))}</span>"
                       if r.get("notes") else "")
                    + "</td></tr>")
            all_basis = []
            for r in rs:
                for b in r.get("legal_basis") or []:
                    if b not in all_basis:
                        all_basis.append(b)
            # 어느 법 계열인가 — 룰의 org_type이 정한다. 전부 local 전용 사유(예: 지방
            # `rebid`)에 "국가계약법 시행령 제26조 계열"이라 쓰면 그냥 틀린 말이고,
            # 방법 default 조문(국가계약법 시행령 제26조)까지 끌어와 무관한 조문을 싣는다.
            # build_decision_pack이 `include_method_defaults=not _is_local`로 같은 구분을
            # 이미 하고 있다 — 페이지도 같은 규칙을 따른다.
            is_local_only = all(is_local_rule(r) for r in rs)
            law_family = ("지방계약법 시행령 제25조·제26조 계열" if is_local_only
                          else "국가계약법 시행령 제26조 계열")
            q = f"{label} 사유로 수의계약을 할 수 있나요?"
            a = ("가능한 경우가 법령에 정해져 있습니다. 근거는 "
                 f"{', '.join(all_basis[:2])}이며, 사유를 정당화하는 사유서·증빙이 필요합니다. "
                 "요건 충족 여부는 발주기관 계약부서 확인이 필요합니다.")
            body = f"""
<p class="lede"><b>{esc(label)}</b>는 {esc(law_family)}의 수의계약 사유입니다.
계약유형별로 적용되는 방법과 근거 조문이 다릅니다.</p>
<h2>계약유형별 적용</h2>
<table><tbody>{''.join(rows)}</tbody></table>
<p class="caveat">수의계약은 사유가 인정되는지가 핵심입니다. 감사 사례에서 반복되는 지적은
사유 없는 수의계약·사업 쪼개기입니다 — 사유서와 증빙을 반드시 남기세요. 계약나침반은
요건 충족 여부를 대신 결정하지 않고, 적용 가능한 후보와 근거 조문을 제시합니다.</p>
<h2>근거 조문</h2>
{self.basis_list(all_basis)}
{self.law_html(all_basis, "" if is_local_only else "수의계약")}
<h2>내 사안으로 확인하기</h2>
<p>계약유형·추정가격과 함께 수의계약 사유를 선택하면 해당 사유 룰이 매칭돼 근거 조문까지 함께 나옵니다.</p>
<p><a class="cta" href="/#decide">수의계약 가능 여부 확인하기</a></p>
<h2>다른 수의계약 사유</h2>
<ul class="links">{''.join(
    f'<li><a href="/g/sujeui-{o.replace("_", "-")}.html">'
    f'{esc(REASON_LABEL.get(o, o))}</a></li>' for o in sorted(reasons) if o != rn)}</ul>
"""
            self.pages.append(Page(slug, title, desc,
                                   f"수의계약 사유: {label} — 요건과 근거 조문", body,
                                   [("계약나침반", "/"), ("가이드", "/g/index.html"),
                                    ("수의계약 사유", ""), (label, "")],
                                   [faq_ld([(q, a)])], priority="0.8"))

    # ── 용어 ─────────────────────────────────────────────────────────────────
    # 용어 정의 안의 금액 주장 — 룰셋과 어긋날 수 있어 발행하지 않는다.
    _MONEY_RE = re.compile(r"\d[\d,.]*\s*(?:억|천만|백만|만)?\s*원")

    def build_term_pages(self) -> None:
        terms = []
        for t in self.glossary:
            if not (t.get("term") and t.get("definition")):
                continue
            # 왜 금액이 들어간 용어를 발행하지 않는가(2026-08-06 codex 3차 지적):
            # `data/glossary.json`의 금액 주장이 룰셋과 어긋나 있다 — 소액수의계약은
            # "물품·용역 2.2억원"이라 적혀 있지만 룰셋 판정은 국가 1억원·지자체 2천만원이고,
            # 종합심사낙찰제는 "300억원 이상"이지만 룰셋은 100억원 이상에서 적용한다.
            # 어느 쪽이 법적으로 맞는지는 도메인 판단이고 우리가 여기서 정할 일이 아니다.
            # 다만 **같은 배포물 안에서 서로 모순되는 두 숫자를 대외에 내는 것**은 확실히
            # 잘못이다. 그래서 정합이 확인될 때까지 그 용어의 페이지를 내지 않는다
            # (지어내지도, 조용히 고치지도 않는다). 계약방법 페이지의 금액은 전부
            # 룰엔진 판정값이라 이 문제가 없다.
            if self._MONEY_RE.search(t["definition"]):
                self.skipped.append(
                    f"금액 주장 룰셋 미대조로 발행 제외: 용어 '{t['term']}' "
                    f"({', '.join(self._MONEY_RE.findall(t['definition']))}) — "
                    "glossary와 룰셋 금액이 어긋나면 대외 페이지에 모순을 박제한다")
                continue
            terms.append(t)
        by_slug = {}
        for t in terms:
            slug = "term-" + romanize(t["term"])
            while slug in by_slug:
                slug += "-2"
            by_slug[slug] = t
        items = list(by_slug.items())
        for i, (slug, t) in enumerate(items):
            term, definition = t["term"], t["definition"]
            related = t.get("related") or []
            aliases = t.get("aliases") or []
            title = f"{term}이란? 공공계약 용어 뜻과 근거 조문 | 계약나침반"
            desc = (definition[:110] + ("…" if len(definition) > 110 else "")
                    + f" — {term}의 근거 조문까지 확인하세요.")
            nbrs = [items[(i + k) % len(items)] for k in range(1, 7)]
            body = f"""
<p class="lede">{esc(definition)}</p>
{'<p><b>같은 뜻으로 쓰는 말</b> — ' + esc(', '.join(aliases)) + '</p>' if aliases else ''}
<h2>근거 조문</h2>
{self.basis_list(related) if related else '<p>이 용어는 특정 조문 하나로 정의되지 않습니다. 서비스의 법령 조회에서 관련 조문을 확인하세요.</p>'}
{self.law_html(related) if related else ''}
<h2>실무에서 왜 문제가 되나</h2>
<p>계약방법·적격심사·하한율 판단은 이 용어들이 법령에서 어떻게 정의되는지에 따라 결과가 달라집니다.
용어 정의를 잘못 잡으면 계약방법 선택 자체가 감사 지적 대상이 될 수 있습니다.
계약나침반은 계약유형·추정가격·기관유형을 넣으면 적용 가능한 계약방법과 근거 조문을 함께 제시합니다.</p>
<p><a class="cta" href="/#decide">내 사안의 계약방법 확인하기</a></p>
<h2>함께 보는 용어</h2>
<ul class="links">{''.join(f'<li><a href="/g/{s}.html">{esc(o["term"])}</a></li>' for s, o in nbrs)}</ul>
<p><a href="/g/index.html">공공계약 용어·계약방법 가이드 전체 목차</a></p>
"""
            self.pages.append(Page(slug, title, desc, f"{term}이란?", body,
                                   [("계약나침반", "/"), ("가이드", "/g/index.html"),
                                    ("용어", ""), (term, "")],
                                   [faq_ld([(f"{term}이란 무엇인가요?", definition)])],
                                   priority="0.6"))

    # ── 부정당 근거 조회 (판정 아님) ──────────────────────────────────────────
    def build_sanction_page(self) -> None:
        """POSITIONING §4-4: '부정당 자동 판정'이라고 쓰면 과장이다 — 조회까지만."""
        slug = "budeongdang-geungeo"
        body = """
<p class="lede">계약나침반은 부정당업자 제재 <b>여부를 판정하지 않습니다</b>. 제재기준 별표와
판례·법령해석례를 조문 단위로 찾아 근거를 제시하는 것까지가 이 도구의 역할입니다.</p>
<h2>이 도구가 주는 것 / 주지 않는 것</h2>
<table><tbody>
<tr><th>주는 것</th><td>부정당업자 입찰참가자격 제한기준(시행규칙 별표) 등 규정 원문 검색,
법령 조문 조회(현행 / 특정 시점 시행본 분리), 관련 판례·법령해석례 검색</td></tr>
<tr><th>주지 않는 것</th><td>제재 대상 여부·제재 기간의 판정, 처분의 적법성 평가,
법적 자문·유권해석</td></tr>
</tbody></table>
<p class="caveat">제재 기간·감경은 위반 유형과 사실관계에 따라 달라지고, 처분권자는 발주기관입니다.
근거 조문을 찾는 데까지 쓰고, 판단은 소속 기관 계약부서·법무 검토를 거치세요.</p>
<h2>왜 '현행 조문'만으로는 부족한가</h2>
<p>과거 계약·처분을 다룰 때 현행 조문을 그대로 적용하면 조용히 틀립니다. 계약나침반의 법령 조회는
현행 조문과 <b>특정 날짜에 시행 중이던 조문</b>을 분리해 제공합니다 — 감사 대응에서 기준 시점이
답에 붙어 있어야 하는 이유입니다.</p>
<h2>AI 에이전트에 물려 쓰기</h2>
<p>Claude·ChatGPT·Cursor 등에 MCP로 연결하면 같은 조회를 도구 호출로 쓸 수 있습니다.
검색 결과가 0건이면 0건이라고 공시합니다 — 못 찾은 것을 없는 것으로 바꾸지 않습니다.</p>
<p><a class="cta" href="/">계약나침반에서 규정·판례 조회하기</a></p>
<h2>함께 보는 문서</h2>
<ul class="links">
<li><a href="/g/index.html">공공계약 계약방법·용어 가이드</a></li>
<li><a href="/g/sujeui-urgent.html">수의계약 사유: 긴급 필요</a></li>
</ul>
"""
        self.pages.append(Page(
            slug,
            "부정당업자 제재기준·판례 근거 찾기 | 계약나침반",
            "부정당업자 입찰참가자격 제한기준 별표와 관련 판례·법령해석례를 조문 단위로 찾는 방법. "
            "계약나침반은 제재 여부를 판정하지 않고 근거 조문을 찾아줍니다.",
            "부정당업자 제재 — 근거 조문·판례를 찾는 방법",
            body,
            [("계약나침반", "/"), ("가이드", "/g/index.html"), ("부정당 제재 근거", "")],
            priority="0.7"))

    # ── 허브 ─────────────────────────────────────────────────────────────────
    def build_hub(self) -> None:
        methods = [p for p in self.pages if p.slug.startswith("method-")]
        reasons = [p for p in self.pages if p.slug.startswith("sujeui-")]
        terms = [p for p in self.pages if p.slug.startswith("term-")]
        others = [p for p in self.pages if p.slug.startswith("budeongdang")]

        def ul(ps):
            return '<ul class="links">' + "".join(
                f'<li><a href="/g/{p.slug}.html">{esc(p.h1)}</a></li>' for p in ps) + "</ul>"

        groups = []
        for ct in ("construction", "service", "product"):
            for org in ("national", "local", "public_corp"):
                pre = f"method-{CT_ROMAN[ct]}-{ORG_ROMAN[org]}-"
                sel = [p for p in methods if p.slug.startswith(pre)]
                if sel:
                    sel.sort(key=lambda p: int(p.slug.rsplit("-", 1)[1]))
                    groups.append(f"<h3>{esc(ORG_LABEL[org])} · {esc(CT_LABEL[ct])}</h3>" + ul(sel))
        body = f"""
<p class="lede">계약유형·추정가격·기관유형별 <b>적용 가능한 계약방법</b>과 근거 조문,
수의계약 사유별 요건, 공공계약 용어 정의를 정리한 가이드입니다. 모든 판정값은 계약나침반
룰엔진이 라이브 서비스와 같은 코드로 계산합니다.</p>
<p><a class="cta" href="/#decide">내 사안 바로 판정하기</a></p>
<h2>금액구간별 계약방법 ({len(methods)}건)</h2>
{''.join(groups)}
<h2>수의계약 사유별 요건 ({len(reasons)}건)</h2>
{ul(reasons)}
<h2>부정당업자 제재 근거</h2>
{ul(others)}
<h2>공공계약 용어 ({len(terms)}건)</h2>
{ul(terms)}
"""
        self.pages.insert(0, Page(
            "index",
            "공공계약 계약방법·낙찰하한율·용어 가이드 | 계약나침반",
            "국가기관·지방자치단체·공기업의 공사·용역·물품 계약방법을 추정가격 구간별로 정리했습니다. "
            "수의계약 사유별 요건과 공공계약 용어 정의, 근거 조문을 함께 제공합니다.",
            "공공계약 계약방법·용어 가이드",
            body,
            [("계약나침반", "/"), ("가이드", "")],
            priority="0.9"))

    # ── 실행 ─────────────────────────────────────────────────────────────────
    def build(self) -> None:
        self.build_method_pages()
        self.build_reason_pages()
        self.build_term_pages()
        self.build_sanction_page()
        self.build_hub()

    def gate(self) -> list[str]:
        """생성물 자체 검증 — 위반은 실패다."""
        problems: list[str] = []
        seen_slug, seen_title = set(), {}
        for p in self.pages:
            if p.slug in seen_slug:
                problems.append(f"슬러그 중복: {p.slug}")
            seen_slug.add(p.slug)
            if p.title in seen_title:
                problems.append(f"title 중복(중복 콘텐츠 위험): {p.title} ← {p.slug}, {seen_title[p.title]}")
            seen_title[p.title] = p.slug
            if not re.fullmatch(r"[a-z0-9\-]+", p.slug):
                problems.append(f"슬러그에 ASCII 아닌 문자: {p.slug}")
            rendered = p.render(self.asof)
            for bad in FORBIDDEN:
                if bad in rendered:
                    problems.append(f"금지 표현 '{bad}' 포함: {p.slug}")
            if len(p.desc) < 40:
                problems.append(f"description 너무 짧음: {p.slug}")
            if DISCLAIMER not in rendered:
                problems.append(f"면책 문구 누락: {p.slug}")
            for href in re.findall(r'href="/g/([a-z0-9\-]+)\.html"', rendered):
                if href not in {q.slug for q in self.pages}:
                    problems.append(f"내부 링크 깨짐: {p.slug} → /g/{href}.html")
        return problems

    @property
    def lastmod(self) -> str:
        """페이지 내용이 마지막으로 바뀐 날. 룰셋 기준일과 템플릿 개정일 중 늦은 쪽."""
        return max(self.asof, TEMPLATE_REVISED)

    def expected_files(self) -> dict[str, str]:
        """이 룰셋·템플릿이 만들어야 하는 파일 전체(경로 → 내용)."""
        out = {f"{p.slug}.html": p.render(self.asof) for p in self.pages}
        urls = [(f"{SITE}/", "1.0")] + [(f"{SITE}/g/{p.slug}.html", p.priority) for p in self.pages]
        lines = ['<?xml version="1.0" encoding="UTF-8"?>',
                 '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        for u, pr in urls:
            lines.append(f"  <url><loc>{u}</loc><lastmod>{self.lastmod}</lastmod>"
                         f"<priority>{pr}</priority></url>")
        lines.append("</urlset>")
        out["../sitemap.xml"] = "\n".join(lines) + "\n"
        return out

    def verify_disk(self) -> list[str]:
        """**디스크에 있는 것**(= 배포되는 것)이 룰셋에서 파생된 것과 같은지 검증.

        왜 필요한가(2026-08-06 codex 지적): 게이트가 메모리에서 만든 페이지만 검사하면
        정작 배포되는 `frontend/public/g/*.html`은 아무도 안 본다. 누가 페이지를 손으로
        고쳤거나, 룰셋 개정 후 생성기를 안 돌렸거나, 파일이 누락돼도 게이트는 초록이다.
        `--check`는 그 파일들을 실제로 읽어 대조한다 — 이 리포의 페이지는 파생 산출물이고,
        파생 산출물의 진실원은 룰셋이다(수기 편집 금지).
        """
        problems: list[str] = []
        expected = self.expected_files()
        if not OUT_DIR.exists():
            return [f"생성 디렉토리 없음: {OUT_DIR.relative_to(BASE)} — 생성기를 실행하라"]
        for rel, content in expected.items():
            path = (OUT_DIR / rel).resolve()
            if not path.exists():
                problems.append(f"디스크에 없음: {rel}")
            elif path.read_text(encoding="utf-8") != content:
                problems.append(f"디스크 내용 불일치(수기 편집 또는 미재생성): {rel}")
        on_disk = {f.name for f in OUT_DIR.glob("*.html")}
        for extra in sorted(on_disk - {k for k in expected if not k.startswith("..")}):
            problems.append(f"룰셋에서 파생되지 않은 잔재 파일: {extra}")
        return problems

    def write(self) -> None:
        if OUT_DIR.exists():
            shutil.rmtree(OUT_DIR)
        OUT_DIR.mkdir(parents=True)
        for rel, content in self.expected_files().items():
            path = OUT_DIR / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="파일을 쓰지 않고 검증만 — 게이트 + **디스크 산출물 대조**")
    ap.add_argument("--strict", action="store_true",
                    help="(기본 동작이라 불필요 — 호환용) 게이트 위반 시 rc=1")
    ap.add_argument("--force", action="store_true",
                    help="게이트 위반이 있어도 기록한다(권장하지 않음, rc는 여전히 1)")
    args = ap.parse_args()

    b = Builder()
    b.build()
    problems = b.gate()
    # --check(=CI)는 **배포되는 파일**까지 대조한다. 메모리 검사만으로는 손으로 고친
    # 페이지·룰셋 개정 후 미재생성·누락 파일을 잡지 못한다.
    disk_problems = b.verify_disk() if args.check else []

    print(f"룰셋 기준일: {b.asof} · 템플릿 개정일: {TEMPLATE_REVISED} · "
          f"sitemap lastmod: {b.lastmod} · 룰 {len(b.rules)}건 · 용어 {len(b.glossary)}건")
    print(f"생성 페이지 {len(b.pages)}건 "
          f"(허브 1 · 금액구간 {sum(1 for p in b.pages if p.slug.startswith('method-'))} · "
          f"수의사유 {sum(1 for p in b.pages if p.slug.startswith('sujeui-'))} · "
          f"용어 {sum(1 for p in b.pages if p.slug.startswith('term-'))} · 기타 1)")
    if b.skipped:
        print(f"발행 제외 {len(b.skipped)}건 (근거 부재·밴드 불일치):")
        for s in b.skipped:
            print("  -", s)
    if problems:
        print(f"게이트 위반 {len(problems)}건:")
        for p in problems:
            print("  ✗", p)
    else:
        print("게이트 통과: 슬러그·title 중복 0 · 금지표현 0 · 면책 누락 0 · 내부링크 깨짐 0")
    if args.check:
        if disk_problems:
            print(f"디스크 산출물 불일치 {len(disk_problems)}건 "
                  f"(배포되는 파일이 룰셋 파생본과 다르다 — 생성기를 다시 돌려라):")
            for p in disk_problems[:20]:
                print("  ✗", p)
            if len(disk_problems) > 20:
                print(f"  … 외 {len(disk_problems) - 20}건")
        else:
            print(f"디스크 산출물 대조 통과: {len(b.pages)}파일 + sitemap 전건 일치")
    problems = problems + disk_problems

    # fail-closed: 게이트 위반이 있으면 **기록하지 않는다**(2026-08-06 codex 지적).
    # 종전에는 위반을 출력만 하고 잘못된 페이지를 그대로 써 넣은 뒤 rc=0으로 끝났다
    # — `--strict`를 잊은 실행 하나가 대외 페이지에 오류를 박제할 수 있었다.
    # 게이트는 기본이 차단이어야 게이트다.
    if problems and not args.force:
        print("→ 게이트 위반이 있어 기록하지 않았다. 수리 후 다시 실행하라 "
              "(정말 기록해야 하면 --force, 권장하지 않음).")
        return 1
    if not args.check:
        b.write()
        print(f"기록: {OUT_DIR.relative_to(BASE)}/*.html ({len(b.pages)}파일), "
              f"{SITEMAP.relative_to(BASE)}")
        print("라이브 반영: cd frontend && npm run build  (재시작 불필요)")
        print("색인 제출: python3 tools/indexnow_ping.py --verify-live  ※ 라이브 반영 확인 후에만")

    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
