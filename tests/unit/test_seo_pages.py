"""프로그래매틱 SEO 생성기 불변식.

여기서 지키는 것은 "생성이 되는가"가 아니라 **생성물이 대외로 나갈 자격이 있는가**다.
페이지는 랜딩·제안서와 같은 대외 주장이므로, 틀린 숫자 하나가 그대로 감사 현장에
전달된다. 그래서 금액 경계·요율 진실원·금지 표현·근거 부재를 테스트가 잡는다.
"""
import importlib.util
import re
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("bsp", BASE / "tools" / "build_seo_pages.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def bsp():
    return _load()


@pytest.fixture(scope="module")
def built(bsp):
    b = bsp.Builder()
    b.build()
    return b


# ── 금액 표기: 경계가 글자에서 사라지면 안 된다 ────────────────────────────────
def test_won_does_not_collapse_boundary(bsp):
    """400,000,001원이 '4억원'으로 표기되면 아래 밴드 상한과 같은 글자가 된다."""
    assert bsp.won(400_000_000) == "4억원"
    assert bsp.won(400_000_001) == "400,000,001원"
    assert bsp.won(230_000_000) == "2.3억원"
    assert bsp.won(20_000_000) == "2,000만원"
    assert bsp.won(99_999_999) == "99,999,999원"


def test_range_text_uses_legal_wording(bsp):
    assert bsp.range_text(1, 400_000_000) == "4억원 이하"
    assert bsp.range_text(400_000_001, None) == "4억원 초과"
    assert bsp.range_text(230_000_000, 499_999_999) == "2.3억원 이상 5억원 미만"
    assert bsp.range_text(10_000_000_000, None) == "100억원 이상"


# ── 경계 수집: '이하/초과'는 T+1에서 바뀐다 ────────────────────────────────────
def test_breakpoints_include_lte_plus_one(built):
    """`estimated_price_lte: 4억` 룰이 있으면 400,000,001이 밴드 시작이어야 한다."""
    pts = built.breakpoints()
    assert 400_000_001 in pts, "이하(lte) 경계의 T+1이 누락되면 한 밴드 안에서 판정이 바뀐다"
    assert 20_000_001 in pts
    assert 0 not in pts and 1 not in pts


def test_breakpoints_include_rate_table_keys(built):
    """요율 구간표(pass_score_by_amount)에만 있는 경계(용역 5억)를 놓치면 안 된다."""
    assert 500_000_000 in built.breakpoints()


# ── 밴드 일관성: 페이지가 주장하는 구간 전체에서 판정이 같아야 한다 ────────────
@pytest.mark.parametrize("ct", ["construction", "service", "product"])
@pytest.mark.parametrize("org", ["national", "local", "public_corp"])
def test_bands_are_internally_consistent(built, ct, org):
    for lo, hi, v in built.bands(ct, org):
        probe_hi = hi if hi else lo * 4
        for price in (lo, (lo + probe_hi) // 2, probe_hi):
            got = built.verdict(ct, org, price)
            assert built._sig(got) == built._sig(v), (
                f"{ct}/{org} 밴드 [{lo},{hi}] 안에서 {price:,}원의 판정이 다르다 — "
                "구간 전체에 대한 주장을 페이지에 실을 수 없다")


def test_no_band_skipped_silently(built):
    """발행 제외가 생기면 침묵하지 않고 목록에 남는다."""
    assert isinstance(built.skipped, list)
    for s in built.skipped:
        assert "제외" in s


# ── 요율 진실원: 엔진 경유값이어야 한다 ────────────────────────────────────────
def test_service_rate_splits_at_500m(built):
    """8151f15가 고친 결함의 페이지판 재발 방지 — 5억 위아래 요율이 달라야 한다."""
    lo_v = built.verdict("service", "national", 300_000_000)
    hi_v = built.verdict("service", "national", 600_000_000)
    assert lo_v["lower_limit_rate"] == 0.86745
    assert hi_v["lower_limit_rate"] == 0.85495


def test_pass_score_ref_is_resolved(built):
    """공개 list API가 null로 주는 룰도 페이지에는 실제 요율이 있어야 한다."""
    v = built.verdict("construction", "national", 7_000_000_000)
    assert v["lower_limit_rate"] == 0.87495
    assert v["pass_score"] == 95


# ── 대외 주장 게이트 ──────────────────────────────────────────────────────────
def test_gate_is_clean(built):
    problems = built.gate()
    assert problems == [], f"게이트 위반: {problems}"


def test_every_method_page_has_legal_basis(built):
    for p in built.pages:
        if p.slug.startswith("method-"):
            html = p.render(built.asof)
            assert "근거 조문" in html
            assert re.search(r"(국가계약법|지방계약법|계약사무규칙|공공기관|중소기업|판로지원법|소프트웨어)", html), \
                f"{p.slug}에 근거 조문 인용이 없다"


def test_no_forbidden_claims(bsp, built):
    """POSITIONING이 금지한 표현(자동 판정·검증됨·체감 수치)."""
    for p in built.pages:
        html = p.render(built.asof)
        for bad in bsp.FORBIDDEN:
            assert bad not in html, f"{p.slug}에 금지 표현 '{bad}'"


def test_sanction_page_says_not_a_verdict(built):
    """부정당은 '판정'이 아니라 '조회'까지만 — POSITIONING §4-4 박제."""
    p = next(x for x in built.pages if x.slug == "budeongdang-geungeo")
    html = p.render(built.asof)
    assert "판정하지 않습니다" in html
    assert "자동 판정" not in html


def test_disclaimer_matches_readme(built):
    """면책 문구는 README와 같아야 한다(문서 간 불일치 금지)."""
    readme = (BASE / "README.md").read_text(encoding="utf-8")
    mod = _load()
    core = "법적 자문·유권해석이 아닙니다"
    assert core in mod.DISCLAIMER
    assert core in readme
    for p in built.pages[:5]:
        assert mod.DISCLAIMER in p.render(built.asof)


def test_all_slugs_ascii_and_unique(built):
    slugs = [p.slug for p in built.pages]
    assert len(slugs) == len(set(slugs))
    for s in slugs:
        assert re.fullmatch(r"[a-z0-9\-]+", s), f"ASCII 아닌 슬러그: {s}"


def test_canonical_and_umami_present(built):
    for p in built.pages[:10]:
        html = p.render(built.asof)
        assert f'<link rel="canonical" href="https://contract.sallim.app/g/{p.slug}.html">' in html
        assert "analytics.naru.build/script.js" in html
        assert 'data-website-id="7c6aa9f8-3bee-4a18-ae4d-0c6467c355fc"' in html


def test_internal_links_resolve(built):
    slugs = {p.slug for p in built.pages}
    for p in built.pages:
        for href in re.findall(r'href="/g/([a-z0-9\-]+)\.html"', p.render(built.asof)):
            assert href in slugs, f"{p.slug} → /g/{href}.html 이 없다"


def test_jsonld_is_valid_json(built):
    import json
    for p in built.pages[:20]:
        for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>',
                                p.render(built.asof), re.S):
            json.loads(block)


# ── codex 교차검증(2026-08-06)에서 잡힌 결함들 ─────────────────────────────────
def test_local_only_reason_page_cites_local_law(built):
    """지방 전용 사유(`rebid`)에 '국가계약법 시행령 제26조 계열'이라 쓰면 틀린 말이다."""
    p = next(x for x in built.pages if x.slug == "sujeui-rebid")
    html = p.render(built.asof)
    assert "지방계약법 시행령 제25조·제26조 계열" in html
    assert "국가계약법 시행령 제26조 계열" not in html
    # 방법 default 조문(국가계약법 시행령 제26조)을 지방 페이지에 끌어오지 않는다
    assert "국가를 당사자로 하는 계약에 관한 법률 시행령 제26조" not in html


def test_method_by_amount_rules_keep_method_in_explanation(built):
    """`method_by_amount` 룰의 설명에서 계약방법이 사라지면(→ '****') 안 된다."""
    p = next(x for x in built.pages if x.slug == "method-gongsa-gukga-5000000000")
    html = p.render(built.asof)
    assert "****" not in html
    assert "일반경쟁입찰" in html
    body = html.split("왜 이 방법인가</h2>")[1][:400]
    assert "1순위 추천" in body, "설명 본문이 비었다"


def test_rule_notes_are_surfaced(built):
    """표의 구조화 값이 담지 못한 조건·예외(룰 notes)를 페이지가 떨어뜨리면 안 된다."""
    p = next(x for x in built.pages if x.slug == "method-gongsa-gukga-1")
    html = p.render(built.asof)
    assert "룰 주석" in html
    assert "1인 견적 가능" in html, "min_quotes=2 단정 옆에 룰의 예외가 있어야 한다"
    p2 = next(x for x in built.pages if x.slug.startswith("method-mulpum-gonggieop-710000000"))
    assert "발주기관별" in p2.render(built.asof)


def test_generator_is_fail_closed(bsp, monkeypatch, tmp_path):
    """게이트 위반이 있으면 기록하지 않고 rc=1 — `--strict` 없이도."""
    calls = []

    class Boom(bsp.Builder):
        def gate(self):
            return ["일부러 만든 위반"]

    monkeypatch.setattr(bsp, "Builder", Boom)
    monkeypatch.setattr(Boom, "write", lambda self: calls.append("wrote"))
    monkeypatch.setattr(bsp.sys, "argv", ["build_seo_pages.py"])
    rc = bsp.main()
    assert rc == 1
    assert calls == [], "게이트 위반인데 기록했다 — 게이트가 아니다"


def test_app_links_to_guide_hub():
    """생성 페이지가 sitemap에만 있고 앱에서 링크되지 않으면 고아 페이지다."""
    home = (BASE / "frontend" / "src" / "pages" / "HomeDashboard.tsx").read_text(encoding="utf-8")
    assert "/g/index.html" in home


def test_sitemap_contains_all_pages(built, tmp_path):
    """sitemap이 실제 생성 페이지 전건을 담는가(홈 1건만 남는 사고 방지)."""
    sitemap = (BASE / "frontend" / "public" / "sitemap.xml").read_text(encoding="utf-8")
    locs = re.findall(r"<loc>(.*?)</loc>", sitemap)
    assert len(locs) == len(built.pages) + 1, f"sitemap {len(locs)}건 vs 페이지 {len(built.pages)}건+홈"
    for p in built.pages:
        assert f"https://contract.sallim.app/g/{p.slug}.html" in locs


# ── codex 2차(2026-08-06)에서 잡힌 결함들 ──────────────────────────────────────
def test_local_method_pages_cite_only_local_law(built):
    """지방 계약방법 페이지에 국가계약법 기본 조문을 끌어오면 적용 법령 거짓 표시다."""
    import re as _re
    checked = 0
    for p in built.pages:
        if not _re.match(r"method-\w+-jibang-", p.slug):
            continue
        checked += 1
        html = p.render(built.asof)
        heads = _re.findall(r'<div class="law"><b>([^<]*)', html)
        assert heads, f"{p.slug}에 조문 블록이 없다"
        for h in heads:
            assert "지방자치단체를 당사자로" in h, \
                f"{p.slug}에 지방 아닌 조문이 실렸다: {h.strip()}"
    assert checked >= 6, f"지방 method 페이지 {checked}건 — 검사 대상이 사라졌다"


def test_verify_disk_detects_drift(built, bsp, tmp_path, monkeypatch):
    """디스크 산출물이 룰셋 파생본과 다르면 --check가 잡아야 한다."""
    assert built.verify_disk() == [], "현재 디스크 산출물이 이미 어긋나 있다"
    target = bsp.OUT_DIR / "index.html"
    original = target.read_text(encoding="utf-8")
    try:
        target.write_text(original + "<!-- 손으로 넣은 줄 -->", encoding="utf-8")
        problems = built.verify_disk()
        assert any("디스크 내용 불일치" in p for p in problems), problems
        target.unlink()
        assert any("디스크에 없음" in p for p in built.verify_disk())
    finally:
        target.write_text(original, encoding="utf-8")
    assert built.verify_disk() == []


def test_lastmod_reflects_template_revision(built, bsp):
    """룰셋 기준일만 쓰면 템플릿·카피 개정이 '변경 없음'으로 보인다."""
    assert built.lastmod == max(built.asof, bsp.TEMPLATE_REVISED)
    assert built.lastmod >= bsp.TEMPLATE_REVISED
    sitemap = (BASE / "frontend" / "public" / "sitemap.xml").read_text(encoding="utf-8")
    assert f"<lastmod>{built.lastmod}</lastmod>" in sitemap


def test_indexnow_live_check_requires_canonical():
    """`id="root"` 부재가 아니라 그 URL의 canonical 존재를 근거로 삼아야 한다."""
    src = (BASE / "tools" / "indexnow_ping.py").read_text(encoding="utf-8")
    assert 'rel="canonical" href="{u}"' in src
    # 주석에는 과거 경위로 남을 수 있으니 **실행되는 줄**만 본다.
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert 'id="root"' not in code, "라이브 판정이 여전히 id=\"root\"에 의존한다"


# ── codex 3차(2026-08-06)에서 잡힌 결함들 ──────────────────────────────────────
def test_international_bid_boundary_is_not_merged(built):
    """후순위로 붙는 의무(국제입찰)가 밴드 병합에 삼켜지면 안 된다."""
    for ct, org, boundary in (("service", "national", 710_000_000),
                              ("construction", "national", 26_500_000_000)):
        starts = [lo for lo, _, _ in built.bands(ct, org)]
        assert boundary in starts, f"{ct}/{org} 국제입찰 경계 {boundary:,}에서 밴드가 갈리지 않는다"
    p = next(x for x in built.pages if x.slug == "method-yongyeok-gukga-710000000")
    html = p.render(built.asof)
    assert "함께 적용되는 룰" in html
    assert "국제입찰" in html
    assert "GPA" in html or "정부조달협정" in html, "룰의 양허 여부 유보가 페이지에 없다"


def test_sig_includes_matched_rule_set(built):
    """서명이 1순위 룰만 보면 후보 집합 변화를 놓친다."""
    a = built.verdict("service", "national", 700_000_000)
    b = built.verdict("service", "national", 720_000_000)
    assert a["rule"]["rule_id"] == b["rule"]["rule_id"], "전제: 1순위 룰은 같다"
    assert built._sig(a) != built._sig(b), "후보 집합이 달라졌는데 서명이 같다"


def test_glossary_money_claims_are_not_published(built, bsp):
    """룰셋과 어긋나는 glossary 금액 주장을 대외 페이지로 내지 않는다."""
    published = {p.h1 for p in built.pages if p.slug.startswith("term-")}
    for term in ("소액수의계약이란?", "종합심사낙찰제란?"):
        assert term not in published, f"{term} 페이지가 발행됐다(룰셋과 금액 모순)"
    assert any("금액 주장" in s for s in built.skipped), "제외 사유가 기록되지 않았다"
    # 발행된 용어 페이지에는 금액 주장이 없어야 한다
    for p in built.pages:
        if p.slug.startswith("term-"):
            lede = p.body.split('</p>')[0]
            assert not bsp.Builder._MONEY_RE.search(lede), f"{p.slug}에 금액 주장이 남았다"


def test_single_value_band_is_readable(bsp):
    assert bsp.range_text(100_000_000, 100_000_000) == "1억원 정확히(경계값)"


def test_page_count_reasonable(built):
    """페이지가 갑자기 사라지거나(생성 실패) 폭증하면(중복 양산) 알아야 한다."""
    assert 60 <= len(built.pages) <= 400, f"페이지 {len(built.pages)}건"
