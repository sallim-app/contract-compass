/**
 * contract-edge — 계약나침반 엣지 워커 (2026-07-30)
 *
 * ① 오리진 폴백 게이트: 게이트웨이급 장애(fetch 실패·502·503·504·52x)에서 HTML은
 *    안내 페이지, API는 구조화 503을 반환. 앱 레벨 500은 그대로 통과(버그를 가리지
 *    않는다). 검증용 강제 폴백: X-Edge-Fallback-Test 헤더.
 * ② GET /api/v1/law/*는 caches.default 엣지 캐시(오리진 Cache-Control 존중) —
 *    워커가 CF 캐시 룰보다 앞단이라 워커 안에서 캐시를 유지해야 P2 효과가 보존된다.
 *
 * ③ 판례·해석례(/api/v1/law/cases·case)는 엣지에서 파싱하되 **naru를 이그레스로**
 *   경유한다: 법제처 Open API가 OC 키 등록 서버 IP만 허용해 워커(유동 IP)가 직접
 *   못 부르므로, nginx `location /lawproxy/`(시크릿 헤더 게이트)가 law.go.kr로
 *   직결 중계한다. 파이썬 앱 미경유 — 오리진 부담은 nginx 바이트 중계뿐이고
 *   uvicorn이 죽은 부분 장애에서도 판례는 동작한다. 한계: naru(nginx) 전체 다운이면
 *   판례도 다운(그땐 폴백 게이트가 안내). lawproxy 실패 시 오리진 백엔드 경로로 폴백.
 * 배포: edge/wrangler.toml + `wrangler deploy`
 *      (secrets: LAW_API_KEY=법제처 OC, EDGE_PROXY_KEY=data/.edge_proxy_key)
 */

const STATUS_PAGE = "https://status.naru.build";

export default {
  async fetch(request, env, ctx) {
    const p = new URL(request.url).pathname;
    if (request.method === "GET" && p === "/api/v1/law/cases") {
      return edgeCached(request, ctx, 3600, () => handleCases(new URL(request.url), env, request));
    }
    if (request.method === "GET" && p === "/api/v1/law/case") {
      return edgeCached(request, ctx, 86400, () => handleCase(new URL(request.url), env, request));
    }
    if (request.method === "GET" && p.startsWith("/api/v1/law/")) {
      // P2 캐시 보존 — 오리진 Cache-Control(200만 부여)을 존중해 워커 캐시에 저장
      return edgeCached(request, ctx, null, () => passthrough(request));
    }
    return passthrough(request);
  },
};

// ── 캐시 래퍼: caches.default, TTL은 명시값 또는 응답 Cache-Control 존중 ─────
async function edgeCached(request, ctx, ttl, produce) {
  const cache = caches.default;
  const key = new Request(new URL(request.url).toString(), { method: "GET" });
  const hit = await cache.match(key);
  if (hit) {
    const h = new Response(hit.body, hit);
    h.headers.set("x-edge-cache", "HIT");
    return h;
  }
  const resp = await produce();
  if (resp.status === 200 && !(resp.headers.get("Cache-Control") || "").includes("no-store")) {
    const toStore = new Response(resp.clone().body, resp);
    if (ttl) toStore.headers.set("Cache-Control", `public, max-age=${ttl}`);
    if (toStore.headers.get("Cache-Control")?.includes("max-age")) {
      ctx.waitUntil(cache.put(key, toStore));
    }
  }
  const out = new Response(resp.body, resp);
  out.headers.set("x-edge-cache", "MISS");
  return out;
}

// ── ② 오리진 통과 + 폴백 ────────────────────────────────────────────────────
async function passthrough(request) {
  if (request.headers.get("x-edge-fallback-test")) return fallback(request, 599);
  let resp;
  try {
    resp = await fetch(request);
  } catch {
    return fallback(request, 0);
  }
  if ([502, 503, 504].includes(resp.status) || resp.status >= 520) {
    // 백엔드가 의도적으로 내는 503(캡·키 미설정)은 detail 본문이 있다 — 그대로 통과
    const ct = resp.headers.get("content-type") || "";
    if (ct.includes("json")) return resp;
    return fallback(request, resp.status);
  }
  return resp;
}

function fallback(request, code) {
  const wantsJson =
    new URL(request.url).pathname.startsWith("/api/") ||
    new URL(request.url).pathname.startsWith("/mcp") ||
    (request.headers.get("accept") || "").includes("json");
  if (wantsJson) {
    return json(
      {
        error: "origin_unavailable",
        message: "계약나침반 서버에 일시적으로 연결할 수 없습니다. 잠시 후 다시 시도해 주세요.",
        status_page: STATUS_PAGE,
        origin_status: code,
      },
      503,
      { "Retry-After": "120", "x-edge-fallback": "1" },
    );
  }
  const html = `<!doctype html><html lang="ko"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>계약나침반 — 일시 점검 중</title>
<body style="font-family:system-ui,sans-serif;max-width:32rem;margin:15vh auto;padding:0 1rem;line-height:1.6">
<h1 style="font-size:1.4rem">🧭 계약나침반이 잠시 쉬고 있습니다</h1>
<p>서버에 일시적으로 연결할 수 없습니다. 보통 몇 분 안에 복구됩니다.</p>
<p><a href="${STATUS_PAGE}">실시간 상태 확인</a> · <a href="javascript:location.reload()">새로고침</a></p>
</body></html>`;
  return new Response(html, {
    status: 503,
    headers: { "content-type": "text/html; charset=utf-8", "Retry-After": "120", "x-edge-fallback": "1" },
  });
}

// ── ③ 판례·해석례: 엣지 파싱 + naru 이그레스(lawproxy) ─────────────────────
function cdata(tag, block) {
  const m = block.match(new RegExp(`<${tag}>(?:<!\\[CDATA\\[)?([\\s\\S]*?)(?:\\]\\]>)?</${tag}>`));
  return m ? m[1].trim().replaceAll("<br/>", " ") : "";
}

async function drf(env, path, params) {
  if (!env.LAW_API_KEY || !env.EDGE_PROXY_KEY) return { err: "secrets 미설정" };
  const qs = new URLSearchParams({ OC: env.LAW_API_KEY, type: "XML", ...params });
  try {
    const r = await fetch(`https://contract.naru.build/lawproxy/${path}?${qs}`, {
      headers: { "x-edge-proxy-key": env.EDGE_PROXY_KEY },
      signal: AbortSignal.timeout(15000),
    });
    if (!r.ok) return { err: `lawproxy HTTP ${r.status}` };
    const xml = await r.text();
    if (!xml.includes("<")) return { err: "law.go.kr 응답 형식 이상" };
    return { xml };
  } catch {
    return { err: "lawproxy 연결 실패" };
  }
}

// ── 판례·해석례 공통 계약 (백엔드 backend/api/v1/law.py와 **문구까지 일치**시킨다) ──
// 2026-08-14 T-2026W33-171: 이 엣지 파서는 백엔드가 2026-07-30에 고친 "본문 미제공 판례를
// 빈 필드로 조용히 넘기던 결함"(R9)과 오늘 넣은 source_url을 못 받은 **갈라진 사본**이었다.
// contract.naru.build만 빈 필드를 24시간 캐시로 서빙했고, 회귀는 localhost만 봐서 무탐지였다.
// 같은 로직이 두 곳에 있는 한 또 갈린다 — 그래서 엣지↔오리진 패리티를 tools/edge_parity_check.py가
// 매일 검사한다(그쪽이 이 파일의 짝이다).
const CASE_URL = {
  prec: (id) => `https://www.law.go.kr/LSW/precInfoP.do?precSeq=${id}`,
  expc: (id) => `https://www.law.go.kr/LSW/expcInfoP.do?expcSeq=${id}`,
};
const CASE_UNAVAILABLE_HINT =
  "이 판례·해석례는 law.go.kr에 본문이 제공되지 않습니다(하급심·타기관 제공 등). " +
  "검색 결과의 사건명·사건번호를 그대로 인용하되 본문 근거가 필요하면 다른 판례를 조회하세요.";

async function handleCases(url, env, request) {
  const q = (url.searchParams.get("q") || "").trim();
  const kind = url.searchParams.get("kind") || "all";
  const topK = Math.max(1, Math.min(parseInt(url.searchParams.get("top_k") || "5", 10) || 5, 10));
  if (q.length < 2 || q.length > 100) return json({ detail: "q는 2~100자" }, 422);
  if (!["prec", "expc", "all"].includes(kind)) return json({ detail: "kind는 prec|expc|all" }, 422);
  const kinds = kind === "all" ? ["prec", "expc"] : [kind];
  const out = [];
  for (const k of kinds) {
    const { xml, err } = await drf(env, "lawSearch.do", { target: k, display: String(topK), query: q });
    if (err) return passthrough(stripTestHeader(request)); // 이그레스 실패 → 오리진 백엔드 경로 폴백
    if (!xml.includes("totalCnt")) return passthrough(stripTestHeader(request)); // 차단·형식 이상도 폴백
    for (const block of xml.match(new RegExp(`<${k} id=[\\s\\S]*?</${k}>`, "g")) || []) {
      out.push(
        k === "prec"
          ? { kind: "prec", case_id: cdata("판례일련번호", block),
              source_url: CASE_URL.prec(cdata("판례일련번호", block)),
              title: cdata("사건명", block),
              org: cdata("법원명", block), case_no: cdata("사건번호", block), date: cdata("선고일자", block) }
          : { kind: "expc", case_id: cdata("법령해석례일련번호", block),
              source_url: CASE_URL.expc(cdata("법령해석례일련번호", block)),
              title: cdata("안건명", block),
              org: cdata("회신기관명", block) || cdata("해석기관명", block), case_no: cdata("안건번호", block),
              date: cdata("회신일자", block) || cdata("해석일자", block) },
      );
    }
  }
  return json(out, 200, { "x-edge": "cases" });
}

async function handleCase(url, env, request) {
  const kind = url.searchParams.get("kind") || "";
  const id = url.searchParams.get("case_id") || "";
  if (!["prec", "expc"].includes(kind)) return json({ detail: "kind는 prec|expc" }, 422);
  if (!/^\w{1,20}$/.test(id)) return json({ detail: "case_id 형식 오류" }, 422);
  const { xml, err } = await drf(env, "lawService.do", { target: kind, ID: id });
  if (err) return passthrough(stripTestHeader(request));
  const f = (tag, limit = 2500) => {
    const v = cdata(tag, xml);
    return v.length > limit ? v.slice(0, limit) + "…(생략)" : v;
  };
  const source_url = CASE_URL[kind](id);
  // 검색엔 뜨지만 본문 API가 "일치하는 …없습니다"를 주는 판례(하급심 등)와 존재하지 않는
  // 일련번호를 **빈 필드 성공으로 위장하지 않는다** — 오리진과 같은 구조화 오류를 낸다.
  // 오류 응답은 캐시하지 않는다(no-store): 원천이 본문을 제공하기 시작하면 즉시 반영돼야 하고,
  // 24시간 동안 "없음"을 박아두면 그 자체가 또 하나의 거짓말이 된다.
  const titleTag = kind === "prec" ? "사건명" : "안건명";
  if (xml.includes("일치하는") || !cdata(titleTag, xml)) {
    return json({ error: "case_body_unavailable", kind, case_id: id, source_url,
                  hint: CASE_UNAVAILABLE_HINT },
                200, { "x-edge": "case", "Cache-Control": "no-store" });
  }
  const body =
    kind === "prec"
      ? { kind, case_id: id, source_url,
          title: f("사건명"), org: f("법원명"), case_no: f("사건번호"), date: f("선고일자"),
          issue: f("판시사항"), summary: f("판결요지"), referenced_laws: f("참조조문", 800) }
      : { kind, case_id: id, source_url,
          title: f("안건명"), org: f("해석기관명"), case_no: f("안건번호"), date: f("해석일자"),
          question: f("질의요지"), answer: f("회답"), reasoning: f("이유", 4000) };
  return json(body, 200, { "x-edge": "case" });
}

function stripTestHeader(request) {
  const h = new Headers(request.headers);
  h.delete("x-edge-fallback-test");
  return new Request(request, { headers: h });
}

function json(obj, status = 200, headers = {}) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", ...headers },
  });
}
