// 랜딩(첫 화면) — MCP-first (T-2026W33-181 / D-2026W33-23, 2026-08-15).
//
// 이 표면의 목적은 계약나침반 웹앱을 전시하는 게 아니라 **MCP의 가치를 사용예시로
// 알리는 것**이다(사장님 2026-08-14 재확정). 그래서 첫 화면은 실제 도구 호출 기록
// (사용자 질문 → 도구 호출 → 근거 답변)이고, 웹 위저드는 맨 아래 '보조 데모'다.
// 카드 나열형 대시보드로 되돌리지 마라 — 되돌리면 결정 D-2026W33-23을 무효화한다.
//
// 전시하는 도구 출력은 전부 실측이다(frontend/src/data/mcpScenarios.ts 머리말 참조).
// 진입: /#home (또는 해시 없음). 계약 Q&A 챗봇은 폐지됨(D-2026W33-22).

import { useState } from 'react'
import { SCENARIOS, SERVER_FACTS } from '../data/mcpScenarios'

const MCP_URL = 'https://contract.sallim.app/mcp'
const DOCS_URL = 'https://github.com/kwenhwang/contract-compass/blob/master/docs/MCP.md'

function CopyEndpoint() {
  const [done, setDone] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(MCP_URL)
      setDone(true)
      setTimeout(() => setDone(false), 1600)
    } catch {
      // 클립보드 권한이 없는 브라우저(구형 인앱 뷰)에서는 조용히 실패한다 —
      // 주소는 옆에 그대로 보이므로 사용자가 직접 선택해 복사할 수 있다.
    }
  }
  return (
    <button className={`lp-copy${done ? ' done' : ''}`} onClick={copy} title="엔드포인트 복사">
      {done ? '복사됨 ✓' : '복사'}
    </button>
  )
}

function ScenarioCard({ s }: { s: (typeof SCENARIOS)[number] }) {
  return (
    <article className="sc">
      <div className="sc-head">
        <span className="sc-n">{s.n}</span>
        <span className="sc-axis">{s.axis}</span>
      </div>
      <div className="sc-body">
        <div className="sc-ask">
          <span className="who">사용자</span>
          <p>{s.ask}</p>
        </div>

        <div className="sc-call">
          <div className="sc-call-h">
            <span className="tag">TOOL CALL</span>
            <span>{s.tool}</span>
          </div>
          <pre>{s.args}</pre>
        </div>

        <div className="sc-out">
          <div className="sc-out-label">서버 응답 (LLM 미사용 · 결정론)</div>
          {s.facts.map((f) => (
            <div className="sc-fact" key={f.k}>
              <span className="fk">{f.k}</span>
              <span className="fv">
                {f.v}
                {f.src && <span className="fsrc">{f.src}</span>}
              </span>
            </div>
          ))}
        </div>

        <p className="sc-why">{s.why}</p>
      </div>
    </article>
  )
}

export default function HomeDashboard({ onDecision, onGlossary }: {
  onDecision: () => void; onGlossary: () => void
}) {
  return (
    // 홈은 문서(body) 스크롤로 흐른다 — position:fixed 오버레이 스크롤러로 띄우면
    // 그 아래 위저드 문서가 따로 스크롤되는 '이중 스크롤 면'이 생기고, 모바일 인앱
    // 브라우저(스레드)는 툴바 접힘·닫기 제스처를 문서 스크롤러에 묶으므로 위로 드래그한
    // 뒤 아래로 스크롤이 먹지 않는다(T-2026W33-178 실측). 뒤 위저드는 .home-open이 접는다.
    <div className="home-page">
      <div className="app">
        {/* 마스트헤드 = 앱 헤더와 같은 문법(감청 괘선 + 워드마크 + mono 부제, T-2026W33-179) */}
        <div className="topbar">
          <div className="tb-logo">
            <span className="tb-rule" aria-hidden="true" />
            <span><span className="tt">계약나침반</span> <span className="ts">공공계약 법령 MCP 서버</span></span>
          </div>
        </div>

        <div className="lp">
          {/* ── 히어로 ── */}
          <header className="lp-hero">
            <span className="lp-kicker">MCP Server · 무LLM 결정론</span>
            <h1 className="lp-h1">
              쓰던 AI에 <em>공공계약 법령</em>을 붙입니다
            </h1>
            <p className="lp-lede">
              계약나침반은 AI 에이전트용 <b>MCP 서버</b>입니다. Claude·ChatGPT·Cursor에 연결하면
              AI가 계약방법 룰엔진 판정·법령 조문 원문·계약예규·적격심사 세부기준·판례를
              <b> 직접 조회해</b> 답합니다. 서버는 어떤 도구에서도 LLM을 쓰지 않으므로,
              답의 근거를 조문 번호까지 되짚을 수 있습니다.
            </p>

            <div className="lp-connect">
              <div className="lp-connect-label">Streamable HTTP 엔드포인트</div>
              <div className="lp-endpoint">
                <code>{MCP_URL}</code>
                <CopyEndpoint />
              </div>
              <code className="lp-connect-cmd">
                claude mcp add --transport http contract-compass {MCP_URL}
              </code>
              <p className="lp-connect-more">
                무료 IP당 50콜/일 · 가입·키 발급 없이 바로 연결 ·{' '}
                <a href={DOCS_URL} target="_blank" rel="noreferrer">도구 11종 명세</a>{' '}
                ·{' '}
                <a href="/mcp/pricing" target="_blank" rel="noreferrer">한도 상향</a>{' '}
                ·{' '}
                <a href="/mcp/health" target="_blank" rel="noreferrer">서버 상태</a>
              </p>
            </div>

            <div className="lp-facts">
              {SERVER_FACTS.map((f) => (
                <div className="lp-fact" key={f.k}>
                  <span className="fv">{f.v}</span>
                  <span className="fk">{f.k}</span>
                  <span className="fn">{f.note}</span>
                </div>
              ))}
            </div>
          </header>

          {/* ── 사용예시 = 이 화면의 본론 ── */}
          <section className="lp-sec">
            <div className="lp-sec-h">
              <h2>실제 사용예시</h2>
              <span className="sub">
                아래 도구 호출·응답은 2026-08-15 라이브 서버(v1.6.4)에서 실측한 것입니다 — 예시용으로 꾸민 대화가 아닙니다.
              </span>
            </div>
            {SCENARIOS.map((s) => <ScenarioCard key={s.id} s={s} />)}
          </section>

          {/* ── 설계 원칙 ── */}
          <section className="lp-sec">
            <div className="lp-sec-h">
              <h2>왜 이렇게 만들었나</h2>
              <span className="sub">AI가 법령을 '기억'해서 답하는 것과 무엇이 다른가</span>
            </div>
            <div className="lp-tenets">
              <div className="lp-tenet">
                <h3>서버는 판단하지 않는다</h3>
                <p>
                  판정은 법령을 직접 인코딩한 룰엔진이, 검색은 임베딩+BM25가, 판례는 law.go.kr
                  실시간 프록시가 한다. 같은 입력에는 언제나 같은 결과가 나오고, 답을 합성하는
                  것은 당신이 쓰는 AI다.
                </p>
              </div>
              <div className="lp-tenet">
                <h3>잘랐으면 잘랐다고 쓴다</h3>
                <p>
                  후보를 상한으로 자르면 무엇이 잘렸는지(<code>omitted_candidates</code>),
                  코퍼스 밖 법령이면 "없다"가 아니라 "우리가 못 본다"고 응답에 적는다.
                  은폐된 폴백이 없어야 AI가 지어내지 않는다.
                </p>
              </div>
              <div className="lp-tenet">
                <h3>매일 회귀로 지킨다</h3>
                <p>
                  수리한 결함은 결정론 회귀 케이스로 박히고 매일 04:30 라이브에 대고 재확인된다.
                  외부에서도 같은 하네스로 우리 판정을 되짚을 수 있다(저장소 공개).
                </p>
              </div>
            </div>
          </section>

          {/* ── 보조 데모 — 웹 위저드는 여기로 강등됐다(D-2026W33-23) ── */}
          <section className="lp-sec">
            <div className="lp-sec-h">
              <h2>연결 없이 먼저 보고 싶다면</h2>
              <span className="sub">같은 룰엔진을 웹에서 직접 두드려 보는 보조 데모</span>
            </div>
            <div className="lp-demo">
              <button className="lp-demo-card" onClick={onDecision}>
                <span className="dt">계약방법 결정 위저드</span>
                <span className="dd">
                  사업명·사업개요·추정가격을 넣으면 MCP의 <code>decide_contract_method</code>와
                  같은 룰엔진이 계약방법 후보와 근거 조문을 보여줍니다.
                </span>
                <span className="dgo">데모 실행 →</span>
              </button>
              <button className="lp-demo-card" onClick={onGlossary}>
                <span className="dt">계약 용어사전</span>
                <span className="dd">
                  추정가격·적격심사·중기간 등 실무 용어를 검색하고 관련 용어로 이어서 봅니다.
                </span>
                <span className="dgo">용어사전 열기 →</span>
              </button>
              {/* 생성 페이지(/g/)로 가는 유일한 사내 진입 경로 — sitemap에만 있고 사이트
                  안에서 아무도 링크하지 않으면 고아 페이지가 된다(2026-08-06 codex 지적).
                  backend/services/dist_status.py가 번들에 이 문자열이 있는지 감시한다. */}
              <a className="lp-demo-card" href="/g/index.html">
                <span className="dt">공공계약 가이드</span>
                <span className="dd">
                  계약유형·기관유형·추정가격 구간별 계약방법과 근거 조문을 정리한 문서.
                  판정값은 이 서비스와 같은 룰엔진에서 나옵니다.
                </span>
                <span className="dgo">가이드 목차 →</span>
              </a>
            </div>
          </section>

          <footer className="lp-foot">
            <p>
              오류·개정 미반영을 발견하면 MCP <code>report_issue</code> 도구로 바로 제보할 수
              있습니다 — 운영 검토 파이프라인에 직결됩니다.{' '}
              <a href={DOCS_URL} target="_blank" rel="noreferrer">도구 명세</a>
            </p>
            <p>
              ⚠️ 이 서비스는 정보 제공 목적이며 법적 자문·유권해석이 아닙니다. 적격심사 통과점수·
              낙찰하한율·각종 한도는 발주기관 세부기준과 법령 개정에 따라 다를 수 있으므로,
              실제 발주 전 소속 기관 계약 부서와 현행 법령을 확인하세요.
            </p>
            <p>
              <b>In English</b> — Contract Compass is a Korean public-procurement guidance
              service. Enter a project name, budget and organization type, and a deterministic
              rule engine returns the applicable contracting method with its statutory basis
              (no LLM in the judgment path). Free tier available; paid keys raise call limits
              only, as fixed-term licenses with no recurring billing.{' '}
              <a href="/mcp/pricing">Pricing</a> · <a href="/legal/terms#en">Terms &amp; privacy
              (English)</a> · Contact sallimapp@gmail.com
            </p>
            <p>
              <a href="/legal/privacy">개인정보처리방침</a> ·{' '}
              <a href="/legal/terms">이용약관</a> · 운영 살림(Sallim) ·{' '}
              문의 sallimapp@gmail.com
            </p>
          </footer>
        </div>
      </div>
    </div>
  )
}

// 전환 계측: 홈→위저드 진입은 App.tsx가 track('wizard-start')로 잡는다(분모가 거기 있다).
// 엔드포인트 복사는 세지 않는다 — 클립보드 권한 실패가 조용해서 분자가 왜곡된다.
