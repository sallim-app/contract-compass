import { useState } from 'react'
import { useWizardStore } from '../store/wizardStore'
import FeedbackBox from '../components/shared/FeedbackBox'
import RecommendationFeedback from '../components/RecommendationFeedback'
import AnnotatedText from '../components/AnnotatedText'
import { useSourceDrawer } from '../components/SourceDrawer'
import Icon from '../components/Icon'
import type { RagSource, KnowledgeWebSources } from '../types'

// 디자이너(Zippt) v2 시안 적용 — flow-step3.jsx의 .op-hero / .op-metrics / .op-card / .kw-panel.
// 비즈니스 로직(legal_basis · ai_rationale · KnowledgeWebPanel · drawer 클릭) 유지.

function EstimatedPriceGuide({
  contractType, serviceType, method,
}: {
  contractType?: string; serviceType?: string; method: string
}) {
  const [open, setOpen] = useState(false)

  type GuideRow = { method: string; basis: string; note?: string }
  let rows: GuideRow[] = []
  let title = '예정가격 산정 방식'

  if (contractType === 'construction') {
    rows = [
      { method: '원가계산', basis: '설계서 기준 재료비·노무비·경비 합산', note: '일반적으로 2억원 이상 공사' },
      { method: '표준시장단가', basis: '조달청 표준시장단가 적용', note: '단가 고시 품목 해당 시' },
      { method: '거래실례가격', basis: '2인 이상 견적 조사', note: '소규모 공사 또는 단순 보수' },
    ]
    title = '공사 예정가격 산정'
  } else if (contractType === 'service') {
    if (serviceType === 'technical') {
      rows = [
        { method: '원가계산', basis: '직접인건비 + 직접경비 + 제경비(110~120%) + 기술료(20~40%) + 부가세', note: '엔지니어링 대가 기준' },
        { method: '실비정산', basis: '실제 발생 비용 정산 방식', note: '연구·조사 용역 등' },
      ]
      title = '기술용역 예정가격 산정'
    } else if (serviceType === 'it_service') {
      rows = [
        { method: '기능점수(FP) 방식', basis: '소프트웨어 기능점수 × 단가', note: '정보시스템 구축·개발' },
        { method: '투입공수 방식', basis: '투입인력 × 단가 × 기간', note: '유지보수·운영 용역' },
        { method: '원가계산', basis: '직접인건비 + 경비 + 일반관리비 + 이윤', note: '일반 용역' },
      ]
      title = 'IT용역 예정가격 산정'
    } else {
      rows = [
        { method: '원가계산', basis: '직접인건비 + 간접비 + 이윤 + 부가세' },
        { method: '거래실례가격', basis: '유사 용역 계약 사례 조사', note: '단순·반복 용역' },
      ]
      title = '용역 예정가격 산정'
    }
  } else if (contractType === 'product') {
    rows = [
      { method: '거래실례가격', basis: '시중 판매가격 또는 전자조달 가격 조사', note: '일반 물품 구매 시 우선 적용' },
      { method: '원가계산', basis: '재료비 + 노무비 + 경비 + 이윤', note: '주문 제작품 등' },
      { method: '감정가격', basis: '전문기관 감정 결과', note: '특수 물품' },
    ]
    title = '물품 예정가격 산정'
  }

  if (method.includes('수의')) {
    rows = [{ method: '견적서 징수', basis: '2인 이상 견적서 수령 후 최저가 기준 예정가격 결정', note: '소액수의(5천만 미만)는 1인 견적 가능' }, ...rows]
  }

  if (!rows.length) return null

  return (
    <div className="op-card" style={{ padding: 0 }}>
      <button
        onClick={() => setOpen(!open)}
        style={{
          width: '100%', display: 'flex', justifyContent: 'space-between', alignItems: 'center',
          padding: '14px 16px', background: 'none', border: 'none', cursor: 'pointer',
          fontFamily: 'inherit', fontSize: 'var(--text-sm)', fontWeight: 700,
          color: 'var(--text-primary)',
        }}
      >
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 7 }}>
          <Icon name="file-text" size={14} style={{ color: 'var(--accent-primary)' }} /> {title}
        </span>
        <span style={{ fontSize: 11, color: 'var(--text-quaternary)' }}>{open ? '▲ 접기' : '▼ 펼치기'}</span>
      </button>
      {open && (
        <div style={{ borderTop: '1px solid var(--border-light)', padding: 14, display: 'flex', flexDirection: 'column', gap: 8, background: 'var(--bg-tertiary)' }}>
          {rows.map((r, i) => (
            <div key={i} style={{ background: 'var(--bg-secondary)', border: '1px solid var(--border-light)', borderRadius: 'var(--radius-md)', padding: 10 }}>
              <p style={{ margin: 0, fontSize: 12, fontWeight: 700, color: 'var(--accent-secondary)' }}>{r.method}</p>
              <p style={{ margin: '4px 0 0', fontSize: 12, color: 'var(--text-secondary)', lineHeight: 1.55 }}>
                <AnnotatedText text={r.basis} />
              </p>
              {r.note && <p style={{ margin: '4px 0 0', fontSize: 11, color: 'var(--text-quaternary)' }}>※ <AnnotatedText text={r.note} /></p>}
            </div>
          ))}
          <p style={{ margin: '4px 0 0', fontSize: 11, color: 'var(--text-quaternary)' }}>예정가격 결정 후 반드시 계약담당부서 검토를 받으세요.</p>
        </div>
      )}
    </div>
  )
}

const KW_TABS: { key: keyof KnowledgeWebSources; label: string; icon: string }[] = [
  { key: 'law', label: '법령', icon: 'scale' },
  { key: 'guide', label: '실무가이드', icon: 'file-text' },
  { key: 'textbook', label: '참고자료', icon: 'book-open' },
]

function KnowledgeTabs({
  kw, onOpen,
}: {
  kw: KnowledgeWebSources; onOpen: (src: RagSource) => void
}) {
  const tabs = KW_TABS.filter((t) => kw[t.key]?.length)
  const [active, setActive] = useState<keyof KnowledgeWebSources>(tabs[0]?.key ?? 'law')
  if (!tabs.length) return null
  const sources = (kw[active] || []).filter((src) => (src.excerpt || '').trim().length >= 50)

  return (
    <div className="kw-panel">
      <div className="kw-tabs">
        {tabs.map((t) => (
          <button
            key={t.key}
            type="button"
            className={`kw-tab ${active === t.key ? 'on' : ''}`}
            onClick={() => setActive(t.key)}
          >
            <Icon name={t.icon} size={13} /> {t.label} <span className="kw-cnt">{kw[t.key].length}</span>
          </button>
        ))}
      </div>
      <div className="kw-body">
        {sources.map((s) => (
          <button key={s.chunk_id} type="button" className="kw-row" onClick={() => onOpen(s)}>
            <div className="kw-row-top">
              <span className="kw-row-title">{s.section_title}</span>
              <span className="kw-row-rel">{s.source_type === 'law' ? '법령' : `${Math.round(s.relevance_score * 100)}%`}</span>
            </div>
            <p className="kw-row-ex"><AnnotatedText text={s.excerpt} /></p>
          </button>
        ))}
      </div>
    </div>
  )
}

export default function Step3Page() {
  const { step2Result, step2RagSources, step2KnowledgeWeb, setStep, step1Input, sessionId } = useWizardStore()
  const { open: openDrawer } = useSourceDrawer()

  const openSource = (src: RagSource) => openDrawer({
    title: src.section_title || '참조 규정',
    content: src.content || src.excerpt || '',
    subtitle: src.document_id ? `📁 ${src.document_id}` : (src.chunk_id ? `🆔 ${src.chunk_id}` : undefined),
    relevance: src.relevance_score,
    sourceType: src.source_type,
  })

  if (!step2Result) return null
  const { method, legal_basis, ai_rationale, details, public_procurement_obligations, confidence } = step2Result
  // F13-5 (2026-06-09): 사용자가 Step2에서 선택한 조건이 실제 적용됐는지 시각 노출
  const appliedConditions = step2Result.applied_conditions as Record<string, unknown> | undefined
  const ignoredReason = step2Result.selection_ignored_reason
  // F19 (2026-06-09): LLM에 전달된 결정론 자료 팩 — 사용자 투명성 (laws_applied만 제공)
  const decisionPack = (step2Result as any).decision_pack as {
    human_explanation?: string
    laws_applied?: Array<{ key: string; law_name: string; articles: Array<{ title: string; body: string }> }>
    _summary?: { law_count?: number; total_chars?: number }
  } | undefined
  const CONDITION_LABELS: Record<string, string> = {
    sme_restriction: '중소기업자간 경쟁',
    small_enterprise_restriction: '소기업·소상공인 제한',
    regional_restriction: '지역제한',
    performance_restriction: '실적제한',
    joint_contract: '공동도급',
    joint_contract_kind: '공동도급 방식',
    regional_restriction_region: '지역명',
  }
  const bidderDecision = (details as any)?.bidder_decision || (details as any)?.bidder_selection || (method.includes('수의') ? '수의계약' : '적격심사')


  return (
    <div className="fl-body" style={{ padding: 0 }}>
      {step1Input.project_name && (
        <p className="op-project">
          {step1Input.project_name}
          {step1Input.estimated_price ? ` · ${(step1Input.estimated_price / 100_000_000).toFixed(1)}억원` : ''}
        </p>
      )}


      {/* 최종 추천 헤더 */}
      <div className="op-hero">
        <div className="op-hero-bg" aria-hidden="true" />
        {/* 2026-06-24 fix: 피드백 위젯 절대배치 → 플렉스로 변경. 긴 계약방법 제목과 겹침 방지(PC 넓은 화면) */}
        <div className="op-hero-top">
          <div className="op-hero-main">
            <span className="op-hero-kicker"><Icon name="clipboard-check" size={13} /> 최종 추천 계약방법</span>
            <p className="op-hero-method">{method}</p>
            <div className="op-hero-row">
              <span className="op-hero-chip">낙찰자결정 · {bidderDecision}</span>
              <span className="op-hero-conf">신뢰도 {Math.round(confidence * 100)}%</span>
            </div>
          </div>
          <div className="op-hero-fb">
            <RecommendationFeedback
              sessionId={sessionId}
              recommendedMethod={method}
              ruleId={step2Result.rule_id || undefined}
              compact
              page="Step3Page"
              label="이 결정이 맞나요?"
            />
          </div>
        </div>
        {/* F13-5 (2026-06-09): 사용자 선택 반영 시각화 */}
        {appliedConditions && Object.keys(appliedConditions).length > 0 && (
          <div style={{
            marginTop: 12, padding: 10,
            background: 'var(--brand-tint)',
            border: '1px solid var(--brand)',
            borderRadius: 8, fontSize: 11,
          }}>
            <div style={{ fontWeight: 800, color: 'var(--ink)', marginBottom: 6 }}>
              ✓ Step2에서 선택하신 조건 반영
            </div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
              {Object.entries(appliedConditions).map(([k, v]) => {
                const label = CONDITION_LABELS[k] ?? k
                const valStr = typeof v === 'boolean' ? (v ? '✓' : '✗') : String(v ?? '')
                const isOff = v === false
                return (
                  <span key={k} style={{
                    padding: '3px 8px', borderRadius: 999,
                    background: isOff ? 'var(--high-tint)' : 'var(--safe-tint)',
                    color: isOff ? 'var(--high-ink)' : 'var(--safe-ink)',
                    fontWeight: 700,
                  }}>
                    {label}: {valStr}
                  </span>
                )
              })}
            </div>
          </div>
        )}
        {/* Q14 (2026-06-13): 선택한 룰이 무시된 사유 — appliedConditions가 비어도 단독 노출 */}
        {ignoredReason && (
          <div style={{
            marginTop: 12, padding: 10,
            background: 'var(--high-tint)',
            border: '1px solid var(--high-ink)',
            borderRadius: 8, fontSize: 11,
            color: 'var(--high-ink)', fontWeight: 700,
          }}>
            ⚠️ {ignoredReason}
          </div>
        )}
      </div>

      {/* F26 (2026-06-10): 룰엔진 자연어 한 문장 설명 — "100억 넘으면 ...밖에 없음" */}
      {decisionPack?.human_explanation && (
        <div style={{
          marginTop: 16,
          padding: '14px 18px',
          background: 'var(--brand)',
          color: 'white',
          borderRadius: 12,
          boxShadow: 'var(--sh-2)',
          fontSize: 15,
          lineHeight: 1.6,
          fontWeight: 600,
        }}>
          🧭 <strong>왜 이 추천인가요?</strong>{' '}
          <span dangerouslySetInnerHTML={{
            __html: decisionPack.human_explanation.replace(/\*\*(.+?)\*\*/g, '<strong style="background: rgba(255,255,255,0.18); padding: 2px 6px; border-radius: 4px;">$1</strong>')
          }} />
        </div>
      )}

      {/* F25-F (2026-06-10): 참조규정을 RAG가 아닌 decision_pack(룰별 결정론 자료 팩)을 메인으로 노출.
          케이스별 정확한 시행령·시행규칙 조문을 UI 메인으로 승격.
          기본 펼침(open) + 시각 강조(브랜드 boundary). RAG sources(아래 step2RagSources)는 보조. */}
      {decisionPack && decisionPack.laws_applied?.length ? (
        <details open style={{
          marginTop: 16,
          padding: '16px 20px',
          background: 'var(--brand-tint)',
          border: '2px solid var(--brand)',
          borderRadius: 14,
          boxShadow: 'var(--sh-1)',
        }}>
          <summary style={{ cursor: 'pointer', fontSize: 15, fontWeight: 900, color: 'var(--ink)' }}>
            📌 참조규정 — 이 케이스에 적용되는 정확한 법령 조문 (룰엔진 lookup)
            <span style={{ marginLeft: 10, fontSize: 12, fontWeight: 600, color: 'var(--brand)' }}>
              법령 {decisionPack._summary?.law_count ?? 0}개
            </span>
          </summary>
          <p style={{ margin: '10px 0 14px', fontSize: 12, color: 'var(--ink-2)', lineHeight: 1.55 }}>
            RAG 검색이 아닌 **룰엔진이 케이스별로 정확히 lookup한 결정 자료**입니다. 같은 입력에 같은 자료가 LLM에 전달되어 같은 결과를 보장합니다.
          </p>

          {(decisionPack.laws_applied?.length ?? 0) > 0 && (
            <div style={{ marginBottom: 14 }}>
              <div style={{ fontSize: 12, fontWeight: 700, color: 'var(--ink)', marginBottom: 6 }}>📜 적용 법령 본문</div>
              {decisionPack.laws_applied?.map((law) => (
                <details key={law.key} style={{ marginBottom: 6, padding: '8px 10px', background: 'white', border: '1px solid var(--line)', borderRadius: 6 }}>
                  <summary style={{ cursor: 'pointer', fontSize: 12, fontWeight: 600 }}>
                    {law.key} <span style={{ color: 'var(--ink-3)', fontWeight: 400 }}>— {law.law_name}</span>
                  </summary>
                  {law.articles.map((art, i) => (
                    <div key={i} style={{ marginTop: 8, fontSize: 11.5, lineHeight: 1.6 }}>
                      <div style={{ fontWeight: 700, color: 'var(--ink-2)' }}>{art.title}</div>
                      <div style={{ color: 'var(--ink-2)', whiteSpace: 'pre-wrap', marginTop: 4 }}>{art.body}</div>
                    </div>
                  ))}
                </details>
              ))}
            </div>
          )}

        </details>
      ) : null}

      {/* 적격심사 점수 / 낙찰하한율 */}
      {(!!details.qualification_score || !!details.lower_limit_rate) && (
        <div className="op-metrics">
          {details.qualification_score != null && (
            <div className="op-metric">
              <span className="op-metric-label">적격심사 통과점수</span>
              <span className="op-metric-val">{String(details.qualification_score)}<em>점</em></span>
            </div>
          )}
          {details.lower_limit_rate != null && (
            <div className="op-metric">
              <span className="op-metric-label">낙찰하한율</span>
              <span className="op-metric-val">{String(details.lower_limit_rate)}</span>
            </div>
          )}
        </div>
      )}

      {/* 중소기업자간 경쟁제품 적용 심사기준 */}
      {step1Input.sme_applicable_standard && (
        <div className="op-card" style={{ borderColor: 'rgba(63,91,107,0.26)', background: 'var(--cat-complex-bg)' }}>
          <p className="op-card-title" style={{ color: 'var(--cat-complex-fg)' }}>
            <Icon name="landmark" size={14} style={{ color: 'var(--cat-complex-fg)' }} />
            적용 심사기준 (중소기업자간 경쟁제품)
            {/* F36-5 (2026-06-11): 다중 선택 codes 모두 노출 — 사용자 의견 "Step2 선택이 Step3에 적용 안 됨" */}
            {(step1Input.sme_product_codes && step1Input.sme_product_codes.length > 0) ? (
              <span style={{ marginLeft: 6, display: 'inline-flex', gap: 4, flexWrap: 'wrap' }}>
                {step1Input.sme_product_codes.map((code, i) => (
                  <span key={code} style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--cat-complex-fg)', fontWeight: 600, background: 'rgba(63,91,107,0.08)', padding: '1px 6px', borderRadius: 4 }}>
                    {code}{step1Input.sme_product_names?.[i] ? ` (${step1Input.sme_product_names[i]})` : ''}
                  </span>
                ))}
              </span>
            ) : step1Input.sme_product_code && (
              <span style={{ marginLeft: 6, fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--cat-complex-fg)', fontWeight: 600 }}>
                {step1Input.sme_product_code}
              </span>
            )}
          </p>
          <p style={{ margin: 0, fontSize: 'var(--text-sm)', color: 'var(--text-secondary)', lineHeight: 1.6 }}>
            {step1Input.sme_applicable_standard === '조달청' && (
              <>추정가격이 고시금액(2.3억원) 이상이므로 <b>조달청에 위탁</b>하여 <AnnotatedText text="「조달청 중소기업자간 경쟁물품에 대한 계약이행능력심사 세부기준」(제525호)" />을 적용합니다.</>
            )}
            {step1Input.sme_applicable_standard === '중기부' && (
              <>추정가격이 고시금액(2.3억원) 미만이므로 <b>중소벤처기업부 「중소기업자간 경쟁제품 중 물품의 구매에 관한 계약이행능력심사 세부기준」</b>을 적용합니다.</>
            )}
            {step1Input.sme_applicable_standard === '직접발주' && (
              <>용역의 경우 「공기업·준정부기관 계약사무규칙」에 따라 직접 발주가 가능합니다.</>
            )}
          </p>
        </div>
      )}

      {/* AI 선정 근거 */}
      <div className="op-card">
        <p className="op-card-title"><Icon name="sparkles" size={14} /> AI 선정 근거</p>
        <p className="op-rationale"><AnnotatedText text={ai_rationale} /></p>
      </div>

      {/* 법적 근거 */}
      <div className="op-card">
        <p className="op-card-title"><Icon name="scale" size={14} /> 법적 근거</p>
        <ul className="op-legal">
          {legal_basis.map((lb, i) => (
            <li key={i} className="op-legal-item">
              <Icon name="arrow-right" size={13} className="op-legal-arr" />
              <span>{lb}</span>
            </li>
          ))}
        </ul>
      </div>

      {/* 참조 규정 출처 — F26 (2026-06-10): RAG는 보조로 약화. 메인은 decision_pack(위 참조규정 박스).
          사용자 의견 누적: "참조규정 안 맞음" → relevance ≥0.6 cutoff + "보조" 라벨 + collapsible. */}
      {step2KnowledgeWeb ? (
        <details style={{ marginTop: 16, padding: '8px 12px', background: 'var(--surface-2)', border: '1px solid var(--line)', borderRadius: 10 }}>
          <summary style={{ cursor: 'pointer', fontSize: 12, fontWeight: 700, color: 'var(--ink-3)' }}>
            🔎 RAG 검색 결과 (보조 · 정확한 자료는 위 "참조규정" 박스)
          </summary>
          <div style={{ marginTop: 10 }}>
            <KnowledgeTabs kw={step2KnowledgeWeb} onOpen={openSource} />
          </div>
        </details>
      ) : step2RagSources.filter((s) => s.relevance_score >= 0.6).length > 0 && (
        <details style={{ marginTop: 16, padding: '8px 12px', background: 'var(--surface-2)', border: '1px solid var(--line)', borderRadius: 10 }}>
          <summary style={{ cursor: 'pointer', fontSize: 12, fontWeight: 700, color: 'var(--ink-3)' }}>
            🔎 RAG 검색 결과 (보조 · 정확한 자료는 위 "참조규정" 박스)
          </summary>
          <div className="ref-list" style={{ marginTop: 10 }}>
            {step2RagSources
              .filter((src) => (src.excerpt || '').trim().length >= 50 && src.relevance_score >= 0.6)
              .map((src) => (
                <button
                  key={src.chunk_id}
                  type="button"
                  className="ref-row"
                  onClick={() => openSource(src)}
                >
                  <span className="ref-ic" style={{ background: 'var(--cat-compare-bg)', color: 'var(--cat-compare-fg)' }}>
                    <Icon name="file-text" size={13} />
                  </span>
                  <span className="ref-title">{src.section_title}</span>
                  <span className="ref-rel">{Math.round(src.relevance_score * 100)}%</span>
                  <Icon name="arrow-up-right" size={13} className="ref-go" />
                </button>
              ))}
          </div>
        </details>
      )}

      {/* PQ 사전심사 */}
      {!!details.pq_required && (
        <div className="op-card" style={{ background: 'var(--cat-compare-bg)', borderColor: 'rgba(14,116,144,0.28)' }}>
          <p className="op-card-title" style={{ color: 'var(--cat-compare-fg)' }}>
            <Icon name="alert-triangle" size={14} style={{ color: 'var(--cat-compare-fg)' }} /> PQ 사전심사 필수
          </p>
          <p style={{ margin: 0, fontSize: 'var(--text-xs)', color: 'var(--text-secondary)', lineHeight: 1.55 }}>
            입찰 공고 전 PQ 공고를 별도로 진행해야 합니다 (최소 30일 이상 사전 게재).
          </p>
          {Array.isArray(details.pq_evaluation_items) && (details.pq_evaluation_items as string[]).length > 0 && (
            <ul className="op-legal" style={{ marginTop: 10 }}>
              {(details.pq_evaluation_items as string[]).map((item, i) => (
                <li key={i} className="op-legal-item">
                  <span style={{ color: 'var(--cat-compare-fg)', fontWeight: 700 }}>•</span> {item}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {/* 전문공사 라이선스 */}
      {!!details.license_required && (
        <div className="op-card" style={{ background: 'var(--danger-soft)', borderColor: 'rgba(179,35,24,0.28)' }}>
          <p className="op-card-title" style={{ color: 'var(--danger)' }}>
            <Icon name="alert-triangle" size={14} style={{ color: 'var(--danger)' }} /> 전문공사 필수 확인 사항
          </p>
          <p style={{ margin: '0 0 6px', fontSize: 'var(--text-xs)', color: 'var(--text-secondary)' }}>
            <b>입찰자격:</b> {String(details.license_required)}
          </p>
          {!!details.applicable_criteria && (
            <p style={{ margin: '0 0 6px', fontSize: 'var(--text-xs)', color: 'var(--text-secondary)' }}>
              <b>적격심사 기준:</b> {String(details.applicable_criteria)}
            </p>
          )}
          {Array.isArray(details.special_notes) && (details.special_notes as string[]).length > 0 && (
            <ul className="op-legal" style={{ marginTop: 6 }}>
              {(details.special_notes as string[]).map((note, i) => (
                <li key={i} className="op-legal-item">
                  <span style={{ color: 'var(--danger)', fontWeight: 700 }}>•</span> {note}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {/* 공공구매 의무 */}
      {public_procurement_obligations.length > 0 && (
        <div className="op-card" style={{ background: 'var(--warning-soft)', borderColor: 'rgba(150,101,11,0.30)' }}>
          <p className="op-card-title" style={{ color: 'var(--med-ink)' }}>
            <Icon name="clipboard-check" size={14} style={{ color: 'var(--med-ink)' }} /> 공공구매 의무 확인
          </p>
          {/* 종전 slice(0,4)가 해당 의무 6건 중 2건(장애인기업·소상공인)을 **말없이 잘랐다**.
              담당자가 이 화면으로 의무구매 비율을 산정하면 누락 책임이 담당자에게 간다.
              우리가 파는 원칙("잘랐으면 잘랐다고 쓴다")을 자기 화면이 어긴 자리라 전량 표시로
              바꾼다(2026-08-15 UX 리뷰, 규칙 파일 실측 6건). */}
          <ul className="op-legal">
            {public_procurement_obligations.map((ob: any, i) => (
              <li key={i} className="op-legal-item">
                <span style={{ color: 'var(--med-ink)', fontWeight: 700 }}>•</span>
                {ob.category}: {ob.mandatory_ratio ? `${(ob.mandatory_ratio * 100).toFixed(0)}% 이상` : '-'} 의무구매
                {ob.legal_basis ? <span style={{ color: 'var(--med-ink)', opacity: 0.75 }}> ({ob.legal_basis})</span> : null}
              </li>
            ))}
          </ul>
          <p className="op-card-note" style={{ color: 'var(--med-ink)', fontSize: '0.82rem', marginTop: 6 }}>
            해당 의무 {public_procurement_obligations.length}건 전부입니다 — 비율은 기관 전체
            구매액 기준이며 개별 계약 건마다 적용되는 값이 아닙니다.
          </p>
        </div>
      )}

      <EstimatedPriceGuide
        contractType={step1Input.contract_type}
        serviceType={step1Input.service_type}
        method={method}
      />

      <FeedbackBox sessionId={sessionId} />

      {/* CTA — 결정 요약 리포트 (인쇄 가능 정리 뷰) */}
      <button
        type="button"
        className="fl-cta op-pdf"
        onClick={() => setStep(4)}
      >
        <Icon name="file-text" size={18} /> 결정 요약 리포트 보기
      </button>

      <button type="button" className="op-restart" onClick={() => setStep(2)}>
        <Icon name="arrow-right" size={13} style={{ transform: 'rotate(180deg)' }} /> 이전 단계로
      </button>
    </div>
  )
}
