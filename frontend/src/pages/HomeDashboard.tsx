// 홈/대시보드 — 계약나침반 진입 화면 (dashboard.css 클래스 사용).
// 진입: /#home (또는 해시 없음). 카드 → 계약방법 결정 위저드 / 계약 Q&A / 용어사전.

import AuthButton from '../components/AuthButton'

export default function HomeDashboard({ onDecision, onAsk, onGlossary }: {
  onDecision: () => void; onAsk: () => void; onGlossary: () => void
}) {
  return (
    // 홈은 문서(body) 스크롤로 흐른다 — position:fixed 오버레이 스크롤러로 띄우면
    // 그 아래 위저드 문서가 따로 스크롤되는 '이중 스크롤 면'이 생기고, 모바일 인앱
    // 브라우저(스레드)는 툴바 접힘·닫기 제스처를 문서 스크롤러에 묶으므로 위로 드래그한
    // 뒤 아래로 스크롤이 먹지 않는다(T-2026W33-178 실측). 뒤 위저드는 .home-open이 접는다.
    <div className="home-page">
      <div className="app">
        {/* 상단바 */}
        {/* 마스트헤드 = 앱 헤더와 같은 문법(감청 괘선 + 워드마크 + mono 부제).
            v1의 그라데이션 아이콘 배지는 폐기했다(T-2026W33-179). */}
        <div className="topbar">
          <div className="tb-logo">
            <span className="tb-rule" aria-hidden="true" />
            <span><span className="tt">계약나침반</span> <span className="ts">공공계약 방법 결정 도우미</span></span>
          </div>
          <div className="tb-right" style={{ marginLeft: 'auto' }}><AuthButton /></div>
        </div>

        {/* 본문 */}
        <div className="dash">
          <div className="dash-hero">
            <div>
              <div className="greet"><b>계약나침반</b> — 공공계약 방법 결정 도우미</div>
              <div className="subg">사업명·금액만 입력하면 법령 기준 계약방법을 결정론 룰엔진이 안내합니다.</div>
            </div>
          </div>

          <div className="entry-cards">
            {/* 계약방법 결정 위저드 */}
            <div className="entry primary">
              <span className="defbadge">시작하기</span>
              <div className="ehead">
                <span className="eico eico-dec">
                  <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M14 2v6h6M9 13l2 2 4-4" /></svg>
                </span>
                <div><h3>계약방법 결정</h3><div className="ewho">발주 전 · 계약 담당자</div></div>
              </div>
              <p>사업명·사업개요·추정가격 3가지만 입력하면 계약유형 분류부터 계약방법·제한경쟁·적용 법령까지 한 번에 추천합니다.</p>
              <div className="emini">
                <div className="mi"><span className="miv num">3초</span><span className="mik">입력→추천</span></div>
                <div className="mi"><span className="miv num">룰엔진</span><span className="mik">결정론 근거</span></div>
              </div>
              <div className="ecta">
                <button className="btn btn-primary" onClick={onDecision}>계약방법 결정 시작 →</button>
              </div>
            </div>

            {/* 계약 Q&A (법령 챗봇) */}
            <div className="entry">
              <div className="ehead">
                <span className="eico eico-aud">
                  <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" /></svg>
                </span>
                <div><h3>계약 Q&A 챗봇</h3><div className="ewho">법령·실무 질문 자유 응답</div></div>
              </div>
              <p>계약 절차·법령 해석·실무 처리 방법을 자유롭게 질문하세요. 답변마다 근거 조문·출처를 함께 제시합니다.</p>
              <div className="emini">
                <div className="mi"><span className="miv num">RAG</span><span className="mik">출처 제시</span></div>
                <div className="mi"><span className="miv num">법령</span><span className="mik">조문 인용</span></div>
              </div>
              <div className="ecta">
                <button className="btn btn-ghost" onClick={onAsk}>계약 Q&A 열기 →</button>
              </div>
            </div>

            {/* 용어사전 */}
            <div className="entry">
              <div className="ehead">
                <span className="eico" style={{ background: 'var(--violet-tint)', color: 'var(--violet-ink)' }}>
                  <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" /><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" /></svg>
                </span>
                <div><h3>계약 용어사전</h3><div className="ewho">추정가격·적격심사·중기간…</div></div>
              </div>
              <p>공공계약 실무 용어를 검색하고, 관련 용어로 이어서 학습할 수 있습니다. 위저드 화면의 용어에도 자동으로 풀이가 달립니다.</p>
              <div className="ecta">
                <button className="btn btn-ghost" onClick={onGlossary}>용어사전 열기 →</button>
              </div>
            </div>

          </div>

          {/* MCP 안내 — AI 에이전트 사용자용 발견 경로 (2026-07-30) */}
          <div className="entry" style={{ marginTop: 14 }}>
            <div className="ehead">
              <span className="eico" style={{ background: 'var(--violet-tint)', color: 'var(--violet-ink)' }}>
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="2" y="7" width="20" height="14" rx="2" /><path d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M12 12v4M9 14h6" /></svg>
              </span>
              <div><h3>AI 에이전트용 MCP 서버</h3><div className="ewho">Claude · ChatGPT · Cursor 연결</div></div>
            </div>
            <p>
              쓰시는 AI에 계약나침반을 연결하면 룰엔진 판정·법령 조문·적격심사 세부기준(별표)·
              판례를 AI가 직접 조회해 답합니다. 서버는 LLM을 쓰지 않아 근거가 검증 가능합니다.
            </p>
            <p style={{ fontSize: 13 }}>
              <code style={{ background: 'var(--bg-2, #f4f4f5)', padding: '2px 6px', borderRadius: 4 }}>
                https://contract.sallim.app/mcp
              </code>
              {' '}· 무료 50콜/일 ·{' '}
              <a href="https://github.com/sallim-app/contract-compass/blob/master/docs/MCP.md" target="_blank" rel="noreferrer">도구 명세</a>
              {' '}·{' '}
              <a href="/mcp/pricing" target="_blank" rel="noreferrer">요금 안내</a>
            </p>
          </div>

          {/* 가이드 페이지 링크 — 생성 페이지(/g/)로 가는 유일한 사내 진입 경로.
              sitemap에만 있고 사이트 안에서 아무도 링크하지 않으면 고아 페이지가 되고,
              크롤러도 사용자도 사실상 도달하지 못한다(2026-08-06 codex 지적). */}
          <div className="entry" style={{ marginTop: 14 }}>
            <div className="ehead">
              <span className="eico" style={{ background: 'var(--violet-tint)', color: 'var(--violet-ink)' }}>
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M4 4h16v16H4z" /><path d="M8 8h8M8 12h8M8 16h5" /></svg>
              </span>
              <div><h3>공공계약 가이드</h3><div className="ewho">금액구간별 계약방법 · 수의계약 사유 · 용어</div></div>
            </div>
            <p>
              계약유형·기관유형·추정가격 구간별로 적용 가능한 계약방법과 근거 조문을 정리한
              문서입니다. 판정값은 이 서비스와 같은 룰엔진에서 나옵니다.
            </p>
            <p style={{ fontSize: 13 }}>
              <a href="/g/index.html">가이드 목차 열기 →</a>
            </p>
          </div>

          <p style={{ marginTop: 18, fontSize: 12, color: 'var(--ink-3)' }}>
            AI는 부정확할 수 있습니다. 중요한 결정 시 법령·실무 기준을 반드시 확인하세요.
          </p>
        </div>
      </div>
    </div>
  )
}
