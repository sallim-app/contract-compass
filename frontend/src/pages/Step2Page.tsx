import { useState, useEffect } from 'react'
import { useWizardStore } from '../store/wizardStore'
import { filterStep1, filterStep2 } from '../api/client'
import FeedbackBox from '../components/shared/FeedbackBox'
import RecommendationFeedback from '../components/RecommendationFeedback'
import ProductClassifySection from '../components/ProductClassifySection'
import SmeProductSearchModal from '../components/SmeProductSearchModal'
import Icon from '../components/Icon'
import { useSourceDrawer } from '../components/SourceDrawer'
import { SW_GUIDE_DOWNLOAD_URL, SME_PRODUCTS_DOWNLOAD_XLSX_URL } from '../api/client'
import { saveHistory } from '../App'
import { inferServiceType } from './Step1Page'
import type { ContractType, ConstructionSpecialty, Step1Input } from '../types'

// 디자이너(Zippt) v2 시안 적용 — flow-step2.jsx의 .s2-sec / .ct-grid / .cand-list / .rs-list 구조.
// 비즈니스 로직(useWizardStore + filterStep1/filterStep2 + community_prior + ProductClassifySection)은 그대로.

const CT_OPTIONS: { value: ContractType; label: string; icon: string }[] = [
  { value: 'service', label: '용역', icon: 'clipboard-check' },
  { value: 'product', label: '물품', icon: 'tag' },
  { value: 'construction', label: '공사', icon: 'building' },
]

// F20-C1 (2026-06-10): 종합 1 + 법령 5 (소방·문화재 등) + 전문 14 = 20개 enum
// 건설산업기본법 시행령 별표1 전문공사 14개 그룹화
const SPECIALTY_OPTIONS: { value: ConstructionSpecialty; label: string; hint: string; group: '종합' | '법령' | '전문' }[] = [
  // 종합공사
  { value: 'general', label: '종합공사', hint: '토목·건축·토목건축·산업환경설비·조경 (4억 기준)', group: '종합' },
  // 법령공사 (별도 법령)
  { value: 'electrical', label: '전기공사', hint: '전기공사업법 (1.6억 기준)', group: '법령' },
  { value: 'ict', label: '정보통신공사', hint: '정보통신공사업법 (1.6억 기준)', group: '법령' },
  { value: 'fire_safety', label: '소방시설공사', hint: '소방시설공사업법 (1.6억 기준)', group: '법령' },
  { value: 'cultural_heritage', label: '문화재수리', hint: '문화재수리법 (1.6억 기준)', group: '법령' },
  { value: 'other', label: '🔧 기타 법령공사', hint: '예: 환경·소음진동·해체 — 자유 텍스트 입력', group: '법령' },
  // 전문공사 14개 (건설산업기본법 시행령 별표1, 2억 기준)
  { value: 'ground_paving', label: '지반조성·포장공사업', hint: '건설산업기본법 시행령 별표1', group: '전문' },
  { value: 'interior', label: '실내건축공사업', hint: '건설산업기본법', group: '전문' },
  { value: 'metal_window_roof', label: '금속창호·지붕건축물조립공사업', hint: '건설산업기본법', group: '전문' },
  { value: 'painting_waterproof', label: '도장·습식·방수·석공사업', hint: '건설산업기본법', group: '전문' },
  { value: 'landscape', label: '조경식재·시설물공사업', hint: '건설산업기본법', group: '전문' },
  { value: 'steel_structure', label: '철강구조물공사업', hint: '건설산업기본법', group: '전문' },
  { value: 'underwater_dredging', label: '수중·준설공사업', hint: '건설산업기본법', group: '전문' },
  { value: 'elevator', label: '승강기·삭도공사업', hint: '건설산업기본법', group: '전문' },
  { value: 'mechanical', label: '기계가스설비공사업', hint: '건설산업기본법', group: '전문' },
  { value: 'gas_heating', label: '가스난방공사업', hint: '건설산업기본법', group: '전문' },
  { value: 'water_sewer', label: '상·하수도설비공사업', hint: '건설산업기본법', group: '전문' },
  { value: 'boring_grouting', label: '보링·그라우팅·파일공사업', hint: '건설산업기본법', group: '전문' },
  { value: 'railway', label: '철도·궤도공사업', hint: '건설산업기본법', group: '전문' },
  { value: 'facility_maintenance', label: '시설물유지관리업', hint: '건설산업기본법', group: '전문' },
]

function priceText(price: number): string {
  if (!price) return ''
  const eok = Math.floor(price / 100_000_000)
  const man = Math.floor((price % 100_000_000) / 10_000)
  return [eok ? `${eok}억` : '', man ? `${man.toLocaleString()}만` : ''].filter(Boolean).join(' ') + '원'
}

const SRC_TYPE_META: Record<string, { icon: string; toneFg: string; toneBg: string }> = {
  law:      { icon: 'scale',     toneFg: 'var(--cat-compare-fg)', toneBg: 'var(--cat-compare-bg)' },
  internal: { icon: 'landmark',  toneFg: 'var(--cat-complex-fg)', toneBg: 'var(--cat-complex-bg)' },
  textbook: { icon: 'book-open', toneFg: 'var(--accent-secondary)', toneBg: 'var(--accent-soft)' },
  guide:    { icon: 'file-text', toneFg: 'var(--text-secondary)', toneBg: 'var(--bg-tertiary)' },
  faq:      { icon: 'message-circle', toneFg: 'var(--success)', toneBg: 'var(--success-soft)' },
}

export default function Step2Page() {
  const {
    step1Result, step1Input, sessionId,
    setStep1Input, setStep1Result, setSessionId,
    setStep2Conditions, setStep2Result, setStep2RagSources, setStep2KnowledgeWeb,
    setStep, setLoading, setError, isLoading, error,
  } = useWizardStore()
  const { open: openDrawer } = useSourceDrawer()
  const [conditions, setConditions] = useState<Record<string, boolean | string>>({})
  // Phase 2: 사용자가 실무 옵션 클릭 시 선택된 kind (null = 1순위 룰 사용)
  const [selectedAltKind, setSelectedAltKind] = useState<string | null>(null)
  const [selectedRuleId, setSelectedRuleId] = useState<string>(
    step1Result?.candidates[0]?.rule_id ?? ''
  )
  const [step2Done, setStep2Done] = useState(false)
  // 2026-06-02 F4-3: 중기간 판정 → 계약방법 추천 순차 진행 (description 5자+ 일 때만 ProductClassify 노출 → confirm 전까지 ③ 흐림)
  const [smeConfirmed, setSmeConfirmed] = useState(false)
  // F34 (2026-06-11): 중기간 검색 모달 — 사용자 의견 "웹에서 검색"
  const [smeSearchOpen, setSmeSearchOpen] = useState(false)
  const needsSmeFirst = (step1Input.description ?? '').trim().length >= 5 && !smeConfirmed

  useEffect(() => {
    const first = step1Result?.candidates[0]?.rule_id
    if (first) setSelectedRuleId(first)
    setStep2Done(false)
  }, [step1Result])

  // 분석 중(선이동 직후) — flow-step2.jsx의 .fl-analyzing 화면
  if (!step1Result) {
    if (isLoading) {
      return (
        <div className="fl-analyzing">
          <div className="spinner" />
          <p className="fl-an-title">AI가 계약방법을 분석하고 있습니다…</p>
          <p className="fl-an-sub">계약유형 · 분류 · 제한경쟁 · 적격심사 기준 확인 중</p>
          <div className="fl-an-steps">
            {['계약유형 추천', '분류·중기간 판정', '계약방법·법령', '적용 기준 확인'].map((s, i) => (
              <span key={s} className="fl-an-step" style={{ animationDelay: `${i * 0.4}s` }}>
                <Icon name="check" size={11} strokeWidth={3} /> {s}
              </span>
            ))}
          </div>
        </div>
      )
    }
    return null
  }

  const toggleCondition = (id: string, val: boolean) => {
    setConditions((prev) => ({ ...prev, [id]: val }))
  }

  // ⓪ 계약유형 변경 → filterStep1 재분석
  const changeContractType = async (ct: ContractType) => {
    if (ct === step1Input.contract_type) return
    setError(null)
    setLoading(true)
    // F25-C (2026-06-10): service_type 자동 강제 default 제거. AI는 추천만 — 사용자가 명시 클릭해야 적용.
    // 사용자 의견: "왜 기술용역이 선택되어있어?"
    const input = {
      ...step1Input,
      contract_type: ct,
      service_type: ct === 'service' ? step1Input.service_type : undefined,   // 기존 선택 유지 또는 미선택
      construction_specialty: ct === 'construction' ? (step1Input.construction_specialty ?? ('general' as const)) : undefined,
    }
    setStep1Input(input)
    try {
      const result = await filterStep1(input as Step1Input)
      setStep1Result(result)
      setSessionId(result.session_id)
      setConditions({})
    } catch (e: any) {
      setError(e.response?.data?.detail || '계약유형 변경 중 오류가 발생했습니다.')
    } finally {
      setLoading(false)
    }
  }

  const changeSpecialty = async (spec: ConstructionSpecialty) => {
    if (spec === step1Input.construction_specialty) return
    setError(null)
    setLoading(true)
    const input = { ...step1Input, construction_specialty: spec }
    setStep1Input(input)
    try {
      const result = await filterStep1(input as Step1Input)
      setStep1Result(result)
      setSessionId(result.session_id)
      setConditions({})
    } catch (e: any) {
      setError(e.response?.data?.detail || '공사 종류 변경 중 오류가 발생했습니다.')
    } finally {
      setLoading(false)
    }
  }

  const handleSubmit = async () => {
    setError(null)
    setLoading(true)
    setStep2Conditions(conditions as Record<string, boolean>)
    try {
      const result = await filterStep2(sessionId, conditions as Record<string, unknown>, selectedRuleId || undefined, selectedAltKind || undefined)
      setStep2Result(result.final_recommendation)
      setStep2RagSources(result.rag_sources ?? [])
      setStep2KnowledgeWeb(result.knowledge_web ?? null)
      saveHistory({
        ts: new Date().toISOString(),
        project_name: step1Input.project_name ?? '',
        contract_type: step1Input.contract_type ?? '',
        estimated_price: step1Input.estimated_price ?? 0,
        method: result.final_recommendation.method,
        input: step1Input,
      })
      setStep2Done(true)
      setStep(3)
    } catch (e: any) {
      setError(e.response?.data?.detail || '오류가 발생했습니다.')
    } finally {
      setLoading(false)
    }
  }

  const currentCt = step1Input.contract_type ?? 'service'
  const suggestedCt = step1Input.suggested_contract_type
  const isUserChanged = !!suggestedCt && currentCt !== suggestedCt
  const currentSpec: ConstructionSpecialty = step1Input.construction_specialty ?? 'general'
  // 6/20 의견: 용역 종류 키워드 자동 추천 — 사업개요로 기술/IT/학술/기타 추천 (선택은 사용자 자유)
  const recoServiceType = inferServiceType(step1Input.description ?? '')

  // 출처 클릭 → SourceDrawer (excerpt가 content 안에 포함되면 highlight로 강조)
  const openSrc = (src: { section_title?: string; excerpt?: string; relevance_score?: number; chunk_id?: string; source_type?: string }) => {
    const fullContent = (src as any).content || src.excerpt || ''
    const ex = (src.excerpt || '').trim()
    // excerpt(잘린 200자)가 content에 정확 포함될 때만 highlight (RAG 매칭 구간 강조)
    const hl = ex && ex.length >= 20 && fullContent.length > ex.length && fullContent.includes(ex) ? ex : undefined
    openDrawer({
      title: src.section_title || '참조 규정',
      content: fullContent,
      highlight: hl,
      subtitle: (src as any).document_id ? `📁 ${(src as any).document_id}` : (src.chunk_id ? `🆔 ${src.chunk_id}` : undefined),
      relevance: src.relevance_score,
      sourceType: src.source_type,
    })
  }

  return (
    <div className="fl-body" style={{ padding: 0 }}>
      {/* 입력 요약 */}
      <div className="sum-card">
        <span className="sum-kicker">분석 대상</span>
        <p className="sum-name">
          {step1Input.project_name}
          {step1Input.estimated_price ? <span className="sum-price"> · {priceText(step1Input.estimated_price)}</span> : null}
        </p>
        {step1Input.description && <p className="sum-desc">{step1Input.description}</p>}
      </div>

      {/* ⓪ 계약유형 */}
      <section className="s2-sec" id="s-type">
        <div className="s2-head">
          <span className="s2-num">0</span>
          <h3 className="s2-title">계약유형</h3>
          {!isUserChanged
            ? <span className="s2-tag ai"><Icon name="sparkles" size={11} /> AI 추천</span>
            : <span className="s2-tag edit"><Icon name="repeat" size={11} /> 직접 변경</span>}
        </div>
        <div className="ct-grid">
          {CT_OPTIONS.map((o) => {
            const sel = currentCt === o.value
            return (
              <button
                key={o.value}
                type="button"
                disabled={isLoading}
                className={`ct-btn ${sel ? 'on' : ''}`}
                onClick={() => changeContractType(o.value)}
              >
                <Icon name={o.icon} size={18} />{o.label}
                {sel && <Icon name="check" size={14} className="ct-check" />}
              </button>
            )
          })}
        </div>
        {!isUserChanged && step1Input.suggested_reason && (
          <p className="s2-note">
            AI 추천 근거: {step1Input.suggested_reason}
            <br />
            <span style={{ color: 'var(--accent-primary)', fontWeight: 600 }}>
              💡 분류가 잘못됐다면 위 버튼으로 직접 선택하세요 — 계약방법·법령이 다시 분석됩니다
            </span>
          </p>
        )}
        {isUserChanged && (
          <p className="s2-note warn">
            계약유형을 변경하면 계약방법·법령이 다시 분석됩니다. (AI 추천: {CT_OPTIONS.find(c => c.value === suggestedCt)?.label})
          </p>
        )}

        {/* 2026-06-02 F4-9: service일 때 용역 종류 선택 (적격심사 별표 차이) */}
        {currentCt === 'service' && (
          <div className="spec-block">
            <p className="spec-label">
              용역 종류 <span>적격심사 세부기준 별표가 달라집니다 (기술/유사/기타)</span>
            </p>
            <div className="spec-row">
              {([
                { v: 'technical', label: '기술용역', hint: '감리·진단·검사·CM·설계 등 — 별표4 등' },
                { v: 'it_service', label: 'IT/SW 용역', hint: '정보시스템 구축·유지관리 — SW 적격심사 별표' },
                { v: 'academic', label: '학술·연구', hint: '연구·조사·평가 용역 — 별표 별도' },
                { v: 'other', label: '기타용역', hint: '일반·단순 용역 — 별표 별도' },
              ] as const).map((s) => {
                const sel = (step1Input.service_type ?? 'technical') === s.v
                const isReco = recoServiceType === s.v
                return (
                  <button
                    key={s.v}
                    type="button"
                    disabled={isLoading}
                    title={s.hint}
                    className={`spec-btn ${sel ? 'on' : ''}`}
                    onClick={async () => {
                      const input = { ...step1Input, service_type: s.v }
                      setStep1Input(input)
                      try {
                        setLoading(true)
                        const result = await filterStep1(input as Step1Input)
                        setStep1Result(result)
                        setSessionId(result.session_id)
                      } finally {
                        setLoading(false)
                      }
                    }}
                  >
                    {s.label}{sel && ' ✓'}{isReco && !sel && ' ★'}
                  </button>
                )
              })}
            </div>
            <p className="s2-note">
              🤖 사업개요 키워드로 <strong>{({ technical: '기술용역', it_service: 'IT/SW 용역', academic: '학술·연구', facility: '시설용역', other: '기타용역' } as const)[recoServiceType]}</strong>(★)을 추천했습니다 — 다르면 직접 선택하세요.
            </p>
            <p className="s2-note">
              💡 용역 종류에 따라 적격심사 통과점수·별표가 달라집니다. 정확한 적용은 용역적격심사세부기준 제2조·제11조 참조.
            </p>
          </div>
        )}

        {currentCt === 'construction' && (
          <div className="spec-block">
            <p className="spec-label">공사 종류 <span>종합/전문에 따라 제한 금액 기준이 다릅니다</span></p>
            <div className="spec-row">
              {SPECIALTY_OPTIONS.map((s) => {
                const sel = currentSpec === s.value
                return (
                  <button
                    key={s.value}
                    type="button"
                    disabled={isLoading}
                    title={s.hint}
                    className={`spec-btn ${sel ? 'on' : ''}`}
                    onClick={() => changeSpecialty(s.value)}
                  >
                    {s.label}{sel && ' ✓'}
                  </button>
                )
              })}
            </div>
            {/* F10-1: 'other' 선택 시 자유 텍스트 input (조경·환경·해체 등) */}
            {currentSpec === 'other' && (
              <input
                type="text"
                value={step1Input.construction_specialty_other ?? ''}
                onChange={(e) => {
                  const v = e.target.value
                  setStep1Input({ ...step1Input, construction_specialty_other: v })
                }}
                placeholder="공사 종류 직접 입력 (예: 조경, 해체, 소음진동, 환경)"
                className="spec-other-input"
                style={{
                  marginTop: 8,
                  width: '100%',
                  padding: '8px 12px',
                  border: '1px solid var(--med)',
                  background: 'var(--med-tint)',
                  borderRadius: 8,
                  fontSize: 13,
                }}
              />
            )}
            <p className="s2-note">
              {currentSpec === 'general'
                ? '종합공사: 지역제한 150억 미만 / 실적제한 30억 이상'
                : '전문공사: 지역제한 10억 미만 / 실적제한 3억 이상'}
            </p>
          </div>
        )}
      </section>

      {/* ① 분류·중기간 판정 */}
      <section className="s2-sec" id="s-class">
        <div className="s2-head">
          <span className="s2-num">1</span>
          <h3 className="s2-title">공사용자재 직접구매 / 중기간 경쟁제품 확인</h3>
        </div>
        <p className="s2-note" style={{ marginTop: 0, marginBottom: 6 }}>
          발주 전 최우선 확인 — 중기간 경쟁제품(공사용자재)은 분리발주 의무 검토 대상
        </p>
        {/* F35-C (2026-06-11): 사급/관급 자재 안내 — 사용자 의견 "구분 필요" */}
        <p style={{ fontSize: 11, color: 'var(--text-tertiary)', margin: '0 0 10px 0', lineHeight: 1.55 }}>
          💡 <b>사급자재</b>: 공사업체 자체 구매 (일반) · <b>관급자재</b>: 발주기관 직접 구매 후 지급 (공사용자재 직접구매 대상이 해당)
        </p>
        {/* F34 (2026-06-11): 웹 검색 + 다운로드 버튼 — 사용자 의견 "1페이지 csv를 2단계로 옮기고 웹에서 열고 검색" */}
        <div style={{ display: 'flex', gap: 8, marginBottom: 12, flexWrap: 'wrap' }}>
          <button
            type="button"
            onClick={() => setSmeSearchOpen(true)}
            style={{
              display: 'inline-flex', alignItems: 'center', gap: 6,
              padding: '8px 14px', borderRadius: 8, fontSize: 13, fontWeight: 700,
              background: 'var(--brand)', color: 'white', border: 'none',
              cursor: 'pointer', boxShadow: 'var(--sh-2)',
            }}
          >
            <Icon name="search" size={14} /> 중기간 경쟁제품 웹 검색
          </button>
          <a
            href={SME_PRODUCTS_DOWNLOAD_XLSX_URL}
            style={{
              display: 'inline-flex', alignItems: 'center', gap: 6,
              padding: '8px 14px', borderRadius: 8, fontSize: 12, fontWeight: 600,
              background: 'var(--bg-tertiary)', color: 'var(--text-secondary)', textDecoration: 'none',
              border: '1px solid var(--border-light)',
            }}
          >
            <Icon name="download" size={13} /> xlsx 다운로드
          </a>
        </div>
        <ProductClassifySection
          description={step1Input.description ?? ''}
          contractType={currentCt}
          sessionId={sessionId}
          onSelect={async ({ smeCode, smeName, applicableStandard, smeCodes, smeNames, smeCombineMode }) => {
            const updatedInput = {
              ...step1Input,
              sme_product_code: smeCode ?? undefined,
              sme_product_name: smeName ?? undefined,
              sme_applicable_standard: applicableStandard,
              is_sme_competition_product: !!smeCode,
              // F13-6 (2026-06-09): 다중 코드 보존
              sme_product_codes: smeCodes && smeCodes.length > 0 ? smeCodes : undefined,
              sme_product_names: smeNames && smeNames.length > 0 ? smeNames : undefined,
              // 6/20 의견: 복수 품목 결합 조건 보존 (의견서·근거 서술용)
              sme_combine_mode: smeCombineMode,
            }
            setStep1Input(updatedInput)
            setSmeConfirmed(true)  // F4-3: 중기간 판정 완료 → ③ 활성화
            // F37-B (2026-06-11): 사용자 의견 "2단계 선택이 실시간 반영 안 됨" 정면.
            // 중기간 변경 시 step1 재호출 → candidates·community_prior 즉시 갱신 (F36-2 강제 고정 효과)
            if (!!smeCode && updatedInput.contract_type && updatedInput.project_name && updatedInput.estimated_price) {
              try {
                const refreshed = await filterStep1(updatedInput as Step1Input)
                setStep1Result(refreshed)
                if (refreshed.session_id) setSessionId(refreshed.session_id)
                const firstRuleId = refreshed.candidates?.[0]?.rule_id
                if (firstRuleId) setSelectedRuleId(firstRuleId)
              } catch (_e) {
                // 재호출 실패는 사용자 경험 막지 않음 (이전 candidates 유지)
              }
            }
          }}
        />
      </section>

      {/* SW 가이드 (용역만) */}
      {currentCt === 'service' && (
        <a href={SW_GUIDE_DOWNLOAD_URL} className="dl-link" style={{ color: 'var(--cat-complex-fg)' }}>
          <Icon name="file-text" size={13} /> 공공SW사업 법제도 가이드 다운로드 (2025.11) — SW 용역 발주 참고
        </a>
      )}

      {/* ③ 계약방법 추천 — candidates + community_prior */}
      {/* 2026-06-02 F6-1: F4-3 비활성을 안내 배너만으로 완화 — 사용자가 ③ 클릭 가능 */}
      <section className="s2-sec" id="s-method" style={{ position: 'relative' }}>
        {needsSmeFirst && (
          <div style={{
            marginBottom: 10, padding: '8px 12px',
            background: 'var(--warning-soft)', border: '1px solid rgba(150,101,11,0.30)',
            borderRadius: 8, fontSize: 12, color: 'var(--med-ink)',
          }}>
            💡 ① 중기간 경쟁제품 판정을 먼저 확정하면 계약방법이 더 정확해집니다. (현재는 '미해당' 가정으로 표시 중)
          </div>
        )}
        {/* 2026-06-02 F7-1 / F8-1 정정: 계약방법(7조 + 43조) vs 낙찰자결정(42조) 정확 분리 */}
        <div style={{
          marginBottom: 10, padding: '8px 12px',
          background: 'var(--cat-compare-bg)', border: '1px solid rgba(14,116,144,0.20)',
          borderRadius: 8, fontSize: 11, color: 'var(--cat-compare-fg)',
          display: 'flex', gap: 10, alignItems: 'flex-start',
        }}>
          📖 <span>
            <b>계약방법</b>: 일반경쟁·제한경쟁·지명경쟁·수의계약 (시행령 <b>제7조</b>)<br/>
            <b>낙찰자결정방법</b>: 적격심사·종합심사·**협상**·최저가·기타 (시행령 <b>제42·43조</b>)<br/>
            <span style={{ opacity: 0.85 }}>※ 실무에서는 시행령 43조 '협상'을 <b>일반/제한경쟁 + 협상</b> 형태로 운영하는 경우가 많습니다. (협상 자체는 낙찰자결정 차원)</span>
          </span>
        </div>
        <div className="s2-head" style={{ alignItems: 'flex-start', gap: 12 }}>
          <span className="s2-num">3</span>
          <h3 className="s2-title">계약방법 추천</h3>
          {/* 2026-06-02: inline 피드백 위젯 — 추천 결과 옆에서 즉시 평가 */}
          <div style={{ marginLeft: 'auto' }}>
            <RecommendationFeedback
              sessionId={sessionId}
              recommendedMethod={step1Result.candidates[0]?.method || ''}
              ruleId={step1Result.candidates[0]?.rule_id}
              compact
            />
          </div>
        </div>
        {/* #2 디자이너 목업 재현: 1순위 추천을 .reco 카드(신뢰도 트랙바 + "왜 이 방법인가")로 — 표현 강화. 선택 로직은 아래 후보 목록 유지 */}
        {(() => {
          const c0 = step1Result.candidates[0]
          if (!c0) return null
          const conf = Math.round((c0.confidence || 0) * 100)
          const confLabel = conf >= 80 ? '높음' : conf >= 60 ? '보통' : '참고'
          const bidder = c0.bidder_selection
          const cp = c0.community_prior
          const cpMatch = cp && cp.n > 0 && (c0.method.includes(cp.top_method) || cp.top_method.includes(c0.method.split(' ')[0]))
          // "왜 이 방법인가" — 실데이터만 (요약 + 추정가격 근거 + 실무 일치)
          const why: React.ReactNode[] = []
          if (c0.summary) why.push(<><b>{c0.method}</b> — {c0.summary}</>)
          if (step1Input.estimated_price) why.push(<>추정가격 <b>{priceText(step1Input.estimated_price)}</b> 기준으로 룰엔진(<span style={{ fontFamily: 'var(--font-mono)' }}>{c0.rule_id}</span>)이 결정한 방법입니다.</>)
          if (cpMatch && cp) why.push(<>유사 사례 <b>{cp.n}건 중 {Math.round(cp.top_ratio * 100)}%</b>가 '{cp.top_method}' — 실무 관행과도 일치합니다.</>)
          return (
            <div className="reco" style={{ marginBottom: 14 }}>
              <div className="reco-top">
                <span className="rb"><Icon name="sparkles" size={12} /> AI 추천 계약방법</span>
                <span className="conf">신뢰도 <b style={{ color: conf >= 80 ? 'var(--safe-ink)' : 'var(--med-ink)' }}>{confLabel}</b>
                  <span className="track"><span className="fill" style={{ display: 'block', width: `${conf}%`, height: '100%', padding: 0, border: 'none', borderRadius: 3, background: conf >= 80 ? 'var(--safe)' : 'var(--med)' }} /></span>
                </span>
              </div>
              <div className="reco-body">
                <div className="reco-method">{c0.method}
                  {bidder && <span className="sub2">낙찰자결정 · {bidder}</span>}
                </div>
                <div className="reco-why">
                  <h5>왜 이 방법인가</h5>
                  <ul>
                    {why.map((w, i) => (
                      <li key={i}><span className="ck"><Icon name="check" size={12} /></span><span>{w}</span></li>
                    ))}
                  </ul>
                </div>
              </div>
            </div>
          )
        })()}
        {step1Result.candidates.length > 1 && (
          <p className="s2-note" style={{ marginTop: 0, marginBottom: 10 }}>아래에서 다른 계약방법으로 변경할 수 있습니다.</p>
        )}
        {/* 2026-06-02 F5-1a: 실무 분포 vs AI 추천 불일치 시 명시적 경고 */}
        {(() => {
          const c0 = step1Result.candidates[0]
          const cp = c0?.community_prior
          if (!cp || cp.n === 0 || cp.top_ratio < 0.5) return null
          const matches = c0.method.includes(cp.top_method) || cp.top_method.includes(c0.method.split(' ')[0])
          if (matches) return null
          return (
            <div style={{
              marginBottom: 10, padding: '10px 12px',
              background: 'var(--warning-soft)', border: '1px solid rgba(150,101,11,0.35)',
              borderRadius: 10, fontSize: 12, color: 'var(--med-ink)',
            }}>
              ⚠️ <b>실무 관행과 다름</b> — 유사 {cp.n}건 중 <b>{Math.round(cp.top_ratio * 100)}%가 '{cp.top_method}'</b>입니다.
              AI 추천(법령 기준)은 '<b>{c0.method}</b>'지만, 실무 사정에 따라 아래 <b>실무 옵션</b> 또는 <b>④ 제한경쟁</b>에서 변경 가능합니다.
              {/* F36-3 (2026-06-11): 사용자 의견 "실무 불일치일 때 하단에 설명을 추가" — 왜 다른지 사유 1~2줄 */}
              <div style={{ marginTop: 8, paddingTop: 8, borderTop: '1px dashed rgba(150,101,11,0.35)', fontSize: 11.5, lineHeight: 1.55, color: 'var(--med-ink)' }}>
                💡 <b>왜 다를까?</b> 법령은 '경쟁' 원칙이라 일반경쟁이 기본이지만, 공공기관 실무에서는 <b>지역경제 활성화·기술 안정성·중기간 보호</b> 사유로 제한경쟁(지역·실적·중기간)을 자주 적용합니다. <br/>
                → 본 사업도 <b>관련 사유(지역제한·실적제한·중기간 등) 검토</b> 후 4번 섹션에서 선택 가능합니다.
              </div>
            </div>
          )
        })()}
        <div className="cand-list">
          {step1Result.candidates.map((c) => {
            const sel = selectedRuleId === c.rule_id
            const cp = c.community_prior
            const matched = cp && (c.method.includes(cp.top_method) || cp.top_method.includes(c.method.split(' ')[0]))
            // F5-1b: 실무옵션 선택 시 candidates 모두 dim
            const altSelected = !!selectedAltKind
            return (
              <button
                key={c.rule_id}
                type="button"
                className={`cand ${sel && !altSelected ? 'on' : ''}`}
                // F36-4 (2026-06-11): 1순위 카드는 항상 브랜드색 강조 (사용자 의견 "기본 추천 하이라이트 안 됨")
                style={altSelected ? { opacity: 0.45 } : c.rank === 1 ? {
                  border: '2px solid var(--brand)',
                  background: 'var(--brand-tint)',
                } : undefined}
                onClick={() => { setSelectedRuleId(c.rule_id); setSelectedAltKind(null) }}
              >
                <div className="cand-top">
                  {/* F8-2: 1순위 뱃지는 selected이고 alt 미선택일 때만 orange. 다른 후보 선택 시 회색 */}
                  {/* F36-4: 1순위는 ✨ 기본 추천 / 나머지는 추가 추천 라벨 명확화 */}
                  <span className={`cand-rank ${c.rank === 1 && sel && !altSelected ? 'first' : ''}`}>
                    {c.rank === 1
                      ? (sel && !altSelected ? '✨ 기본 추천 (선택됨)' : '✨ AI 기본 추천')
                      : `➕ 추가 추천 ${c.rank}`}
                  </span>
                  <span className="cand-method">{c.method}</span>
                  {sel && !altSelected && <Icon name="check-circle" size={16} className="cand-sel" />}
                  {sel && !altSelected && (
                    <span style={{ fontSize: 10, fontWeight: 800, color: 'var(--accent-primary)', background: 'var(--accent-soft)', padding: '2px 8px', borderRadius: 999, marginLeft: 4 }}>
                      ✓ 이 옵션 선택됨
                    </span>
                  )}
                  <span className="cand-conf">{Math.round(c.confidence * 100)}%</span>
                </div>
                {/* F28 (2026-06-10): rule_id 명시 노출 — 사용자 의견 "어떤 룰에 의해 추천한 건지 PC만 표시되는데?" 정정.
                    모바일에서도 visible 하도록 flex wrap. */}
                <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 6, marginTop: 4, fontSize: 11 }}>
                  <span style={{ fontFamily: 'var(--font-mono)', background: 'var(--bg-tertiary)', color: 'var(--text-secondary)', padding: '2px 8px', borderRadius: 4, fontWeight: 700 }}>
                    🆔 {c.rule_id}
                  </span>
                  <span style={{ color: 'var(--text-tertiary)' }}>매칭 룰</span>
                </div>
                <p className="cand-sum">{c.summary}</p>
                {cp && cp.n > 0 && (
                  <div className={`scc-compare ${matched ? 'is-match' : 'is-warn'}`} style={{ marginTop: 10, marginBottom: 0 }}>
                    <div className="scc-cmp-col">
                      <span className="scc-cmp-label"><Icon name="sparkles" size={12} /> AI 추천</span>
                      <span className="scc-cmp-value">{c.method}</span>
                    </div>
                    <div className="scc-cmp-mid">
                      <span className={`scc-cmp-verdict ${matched ? 'ok' : 'warn'}`}>
                        <Icon name={matched ? 'check-circle' : 'alert-triangle'} size={15} />
                        {matched ? '실무 일치' : '실무 불일치'}
                      </span>
                    </div>
                    <div className="scc-cmp-col scc-cmp-right">
                      <span className="scc-cmp-label"><Icon name="building" size={12} /> 실무 최빈</span>
                      <span className="scc-cmp-value">{cp.top_method} <em>{Math.round(cp.top_ratio * 100)}%</em></span>
                    </div>
                  </div>
                )}
                {Object.entries(c.key_params).length > 0 && (
                  <div className="cand-params">
                    {Object.entries(c.key_params).map(([k, v]) => (
                      <span key={k} className="cand-param">{k}: <b>{String(v)}</b></span>
                    ))}
                  </div>
                )}
              </button>
            )
          })}
        </div>

        {/* 실무 옵션 (Phase 2): 클릭 시 final 결과에 사용자 선택 반영 */}
        {step1Result.practice_alternatives && step1Result.practice_alternatives.length > 0 && (
          <div
            style={{
              marginTop: 14, padding: 12,
              background: 'var(--bg-tertiary)', border: '1px solid var(--border-light)',
              borderRadius: 10,
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 8, justifyContent: 'space-between' }}>
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 12, fontWeight: 700, color: 'var(--text-secondary)' }}>
                <Icon name="info" size={14} style={{ color: 'var(--text-tertiary)' }} />
                실무 옵션 — 사정에 따라 다른 계약방법 선택 가능
              </span>
              {selectedAltKind && (
                <button
                  type="button"
                  onClick={() => {
                    setSelectedAltKind(null)
                    const first = step1Result?.candidates?.[0]?.rule_id
                    if (first) setSelectedRuleId(first)
                  }}
                  style={{ background: 'none', border: 'none', cursor: 'pointer', fontSize: 11, color: 'var(--accent-primary)', fontWeight: 700 }}
                >
                  선택 해제 → 1순위 추천 사용
                </button>
              )}
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {step1Result.practice_alternatives.map((alt) => {
                const sel = selectedAltKind === alt.kind
                return (
                  <button
                    key={alt.kind}
                    type="button"
                    onClick={() => {
                      // 2026-06-02 F4-12: 실무옵션 선택 시 최우선 추천(candidates) highlight 해제
                      if (sel) {
                        setSelectedAltKind(null)
                        // 해제 시 1순위 candidate 복원
                        const first = step1Result?.candidates?.[0]?.rule_id
                        if (first) setSelectedRuleId(first)
                      } else {
                        setSelectedAltKind(alt.kind)
                        setSelectedRuleId('')
                      }
                    }}
                    style={{
                      display: 'flex', gap: 8, alignItems: 'flex-start', textAlign: 'left',
                      padding: '8px 10px', borderRadius: 8,
                      background: sel ? 'var(--accent-soft)' : 'var(--bg-secondary)',
                      border: sel ? '1.5px solid var(--accent-primary)' : '1px solid var(--border-light)',
                      cursor: 'pointer', fontFamily: 'inherit',
                    }}
                  >
                    {sel && <Icon name="check-circle" size={14} style={{ color: 'var(--accent-primary)', flexShrink: 0, marginTop: 2 }} />}
                    <span style={{ fontSize: 12, fontWeight: 700, color: 'var(--text-primary)', minWidth: 130 }}>
                      {alt.method}
                    </span>
                    {/* F25-E (2026-06-10): alternatives reason 시각 강조 — 사용자 의견 "왜 대기업 참여제한하는지 알 수 없다" */}
                    <span style={{ fontSize: 12, color: 'var(--text-secondary, var(--ink-2))', lineHeight: 1.55, flex: 1, fontWeight: 500 }}>
                      💡 {alt.reason}
                    </span>
                  </button>
                )
              })}
            </div>
            <p style={{ fontSize: 10, color: 'var(--text-quaternary)', marginTop: 8, marginBottom: 0 }}>
              ※ 시스템은 법령 기본을 1순위로 추천합니다. 클릭하면 실무 사유에 따른 옵션이 최종 결과에 반영됩니다.
            </p>
          </div>
        )}

        {/* 2026-06-02 F5-2 / F7-1: 낙찰자 결정방법 선택 UI — 시행령 제42조 */}
        {step1Result.candidates[0] && (() => {
          const defaultBidder = (step1Result.candidates[0] as any).bidder_selection || (step1Result.candidates[0] as any).key_params?.bidder_selection || '적격심사'
          const bidderOpts = ((step1Result.candidates[0] as any).bidder_options as any[]) || []  // rule이 제공한 알맞은 낙찰자결정 후보들
          const BIDDER_OPTIONS = [
            { value: 'qualification', label: '적격심사', hint: '점수·낙찰하한율 기준 — 시행령 제42조' },
            { value: 'lowest', label: '최저가', hint: '단순 물품·공사 — 가격 위주' },
            { value: 'negotiated', label: '협상', hint: 'IT·SW·창의·전문분야 — 시행령 제43조 협상에 의한 계약체결방법' },
            { value: 'comprehensive', label: '종합심사낙찰제', hint: '대형 공사·기술 우대 — 시행령 제42조 제5항' },
            { value: 'sme_quote', label: '수의시담 / 견적', hint: '수의계약 시' },
          ]
          // selectedAltKind에서 매핑 추정 — 협상 룰(SVC_005 등) 매칭 시 bidder='협상' 자동
          const inferredBidder = selectedAltKind?.includes('small_negotiated') ? 'sme_quote'
            : selectedAltKind === 'designated_competitive' ? 'qualification'
            : conditions['_bidder_selection'] as string || ''
          const activeBidder = inferredBidder || (defaultBidder.includes('협상') ? 'negotiated' : defaultBidder.includes('종합심사') ? 'comprehensive' : defaultBidder.includes('최저가') ? 'lowest' : 'qualification')
          return (
            <div style={{ marginTop: 14, padding: 12, background: 'var(--bg-secondary)', border: '1px solid var(--border-light)', borderRadius: 10 }}>
              <div style={{ fontSize: 12, fontWeight: 700, marginBottom: 8, color: 'var(--text-primary)', display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                <Icon name="gauge" size={14} /> 낙찰자 결정방법 <span style={{ fontSize: 10, color: 'var(--text-tertiary)', fontWeight: 500 }}>(시행령 제42조)</span>
                <span style={{ fontSize: 10, color: 'var(--text-tertiary)', fontWeight: 400 }}>
                  · 기본 추천: <b>{defaultBidder}</b>
                </span>
                {bidderOpts.length > 0 && (
                  <span style={{ fontSize: 10, fontWeight: 700, color: 'var(--accent-primary)', background: 'var(--accent-soft)', padding: '2px 8px', borderRadius: 999 }}>
                    🤖 추가 추천: {bidderOpts.map((b: any) => b.bidder).join(', ')}
                  </span>
                )}
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: 6 }}>
                {BIDDER_OPTIONS.map((b) => {
                  const sel = activeBidder === b.value
                  const isDefault = activeBidder === b.value && !inferredBidder
                  return (
                    <button
                      key={b.value}
                      type="button"
                      onClick={() => setConditions((p: any) => ({ ...p, _bidder_selection: b.value }))}
                      title={b.hint}
                      style={{
                        padding: '8px 10px', fontSize: 12, fontWeight: 700,
                        background: sel ? 'var(--accent-soft)' : 'var(--bg-primary)',
                        color: sel ? 'var(--accent-primary)' : 'var(--text-primary)',
                        border: sel ? '1.5px solid var(--accent-primary)' : '1px solid var(--border-light)',
                        borderRadius: 8, cursor: 'pointer', fontFamily: 'inherit',
                        textAlign: 'left', position: 'relative',
                      }}
                    >
                      {sel && '✓ '}{b.label}
                      {isDefault && (
                        <span style={{ display: 'block', fontSize: 9, fontWeight: 800, color: 'var(--accent-primary)', marginTop: 2 }}>
                          🤖 AI 추천
                        </span>
                      )}
                    </button>
                  )
                })}
              </div>
              <p style={{ fontSize: 10, color: 'var(--text-quaternary)', marginTop: 8, marginBottom: 0 }}>
                ※ 실무옵션 클릭 시 자동으로 권장 낙찰자결정 방법이 highlight됩니다. 필요 시 직접 변경 가능.
              </p>
            </div>
          )
        })()}
      </section>

      {/* ④ 제한경쟁 인터랙티브 추천 (next_step_questions) */}
      {step1Result.next_step_questions.length > 0 && (
        <section className="s2-sec restrict-sec" id="s-restrict">
          <div className="s2-head">
            <span className="s2-num">4</span>
            <h3 className="s2-title">제한경쟁 추천</h3>
          </div>
          <div className="rs-question">
            <Icon name="filter" size={16} />
            <div>
              <p className="rs-q">이 사업, 입찰 참가자격을 제한하시겠습니까?</p>
              {step1Input.estimated_price ? (
                <p className="rs-pricenote">
                  추정가격 {priceText(step1Input.estimated_price)}
                  {currentCt === 'construction' ? ` · ${currentSpec === 'general' ? '종합' : '전문'}공사` : ''} 기준 분기
                </p>
              ) : null}
            </div>
          </div>
          <div className="rs-list">
            {step1Result.next_step_questions.map((q) => {
              const basisIdx = (q.description || '').lastIndexOf('근거:')
              const body = basisIdx > 0 ? q.description!.slice(0, basisIdx).trim() : (q.description || '')
              const basis = basisIdx > 0 ? q.description!.slice(basisIdx) : ''
              return (
                <div key={q.id} className="rs-opt kind-available">
                  <div className="rs-opt-head">
                    <span className="rs-opt-label">{q.text}</span>
                    <span className="rs-pill ok">가능</span>
                  </div>
                  {body && <p className="rs-rule">{body}</p>}
                  {basis && (
                    <div className="rs-legal">
                      <span className="rs-legal-link"><Icon name="scale" size={11} /> {basis}</span>
                    </div>
                  )}
                  <div className="rs-yn">
                    <button
                      type="button"
                      className={`rs-yn-btn ${conditions[q.id] === true ? 'yes' : ''}`}
                      onClick={() => toggleCondition(q.id, true)}
                    >예, 제한</button>
                    <button
                      type="button"
                      className={`rs-yn-btn ${conditions[q.id] === false ? 'no' : ''}`}
                      onClick={() => toggleCondition(q.id, false)}
                    >아니오</button>
                  </div>
                  {q.if_yes && conditions[q.id] === true && (
                    <p className="rs-ifyes"><Icon name="arrow-right" size={12} /> {q.if_yes}</p>
                  )}
                  {/* F4-4 / F13-3 (2026-06-09): 지역제한 선택 시 지역명 input — 강조 (amber 배경, 필수 표시) */}
                  {conditions[q.id] === true && (q.id.includes('region') || q.text.includes('지역')) && (
                    <div style={{ marginTop: 10, padding: 12, background: 'var(--med-tint)', border: '1px solid var(--med)', borderRadius: 8 }}>
                      <label style={{ fontSize: 12, fontWeight: 800, color: 'var(--med-ink)', display: 'block', marginBottom: 6 }}>
                        🗺️ 제한할 지역명 <span style={{ color: 'var(--high)' }}>*필수</span>
                      </label>
                      <input
                        type="text"
                        value={(conditions as any).regional_restriction_region ?? '' as any as string}
                        onChange={(e) => setConditions((p: any) => ({ ...p, regional_restriction_region: e.target.value }))}
                        placeholder="예: 대전광역시 / 충청남도 / 장흥군 등"
                        style={{
                          width: '100%', padding: '8px 12px', fontSize: 13,
                          border: '1px solid var(--med)', borderRadius: 6,
                          fontFamily: 'inherit', background: '#fff',
                          fontWeight: 600,
                        }}
                      />
                      <p style={{ fontSize: 10, color: 'var(--med-ink)', marginTop: 4 }}>
                        이 지역명이 계약대상자 결정 의견서·공고문에 명시됩니다
                      </p>
                    </div>
                  )}
                  {/* F13-2 (2026-06-09): 공동도급 sub_options 4종 라디오 + 법령 클릭 */}
                  {conditions[q.id] === true && q.sub_options && q.sub_options.length > 0 && (
                    <div style={{ marginTop: 10, padding: 12, background: 'var(--brand-tint)', border: '1px solid var(--brand)', borderRadius: 8 }}>
                      <p style={{ fontSize: 12, fontWeight: 800, color: 'var(--ink)', marginBottom: 8 }}>
                        세부 방식 선택 (계약대상자 결정 의견서에 반영)
                      </p>
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                        {q.sub_options.map((so) => {
                          const selected = (conditions as any)[`${q.id}_kind`] === so.value
                          return (
                            <label key={so.value} style={{
                              display: 'flex', alignItems: 'flex-start', gap: 8,
                              padding: '8px 10px', background: '#fff',
                              border: `2px solid ${selected ? 'var(--brand)' : 'var(--line)'}`,
                              borderRadius: 6, cursor: 'pointer',
                            }}>
                              <input
                                type="radio"
                                name={`${q.id}_kind`}
                                value={so.value}
                                checked={selected}
                                onChange={() => setConditions((p: any) => ({ ...p, [`${q.id}_kind`]: so.value }))}
                                style={{ marginTop: 3 }}
                              />
                              <div style={{ flex: 1 }}>
                                <div style={{ fontSize: 13, fontWeight: 700, color: 'var(--ink)' }}>{so.label}</div>
                                <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginTop: 2 }}>{so.desc}</div>
                                <div style={{ fontSize: 10, color: 'var(--brand)', marginTop: 4, fontWeight: 600 }}>
                                  📖 {so.law}
                                </div>
                              </div>
                            </label>
                          )
                        })}
                      </div>
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </section>
      )}

      {/* F28 (2026-06-10): Step2에도 Step3와 동일한 decision_pack 메인 카드 노출.
          사용자 의견 "2단계 참조근거가 3단계랑 다른데?" — 동일한 룰엔진 자료 셋트를 양 단계에 일관 표시. */}
      {(() => {
        const dp = (step1Result as any)?.decision_pack as {
          rule?: { rule_id?: string; name?: string; method?: string }
          human_explanation?: string
          laws_applied?: Array<{ key: string; law_name: string; articles: Array<{ title: string; body: string }> }>
          _summary?: { law_count?: number; total_chars?: number }
        } | undefined
        if (!dp) return null
        const hasContent = (dp.laws_applied?.length ?? 0) > 0
        if (!hasContent && !dp.human_explanation) return null
        return (
          <>
            {dp.human_explanation && (
              <div style={{
                marginTop: 16, padding: '14px 18px',
                background: 'var(--brand)',
                color: 'white', borderRadius: 12, boxShadow: 'var(--sh-2)',
                fontSize: 15, lineHeight: 1.6, fontWeight: 600,
              }}>
                🧭 <strong>왜 이 추천인가요?</strong>{' '}
                <span dangerouslySetInnerHTML={{
                  __html: dp.human_explanation.replace(/\*\*(.+?)\*\*/g, '<strong style="background: rgba(255,255,255,0.18); padding: 2px 6px; border-radius: 4px;">$1</strong>'),
                }} />
              </div>
            )}
            {hasContent && (
              <details open style={{
                marginTop: 16, padding: '16px 20px',
                background: 'var(--brand-tint)',
                border: '2px solid var(--brand)', borderRadius: 14,
                boxShadow: 'var(--sh-1)',
              }}>
                <summary style={{ cursor: 'pointer', fontSize: 15, fontWeight: 900, color: 'var(--ink)' }}>
                  📌 참조규정 — 이 케이스에 적용되는 정확한 법령 조문 (룰엔진 lookup)
                  <span style={{ marginLeft: 10, fontSize: 12, fontWeight: 600, color: 'var(--brand)' }}>
                    법령 {dp._summary?.law_count ?? 0}개
                  </span>
                </summary>
                <p style={{ margin: '10px 0 14px', fontSize: 12, color: 'var(--ink-2)', lineHeight: 1.55 }}>
                  RAG 검색이 아닌 <b>룰엔진이 케이스별로 정확히 lookup한 결정 자료</b>입니다. 같은 입력에 같은 자료가 LLM에 전달되어 같은 결과를 보장합니다.
                </p>
                {(dp.laws_applied?.length ?? 0) > 0 && (
                  <div style={{ marginBottom: 14 }}>
                    <div style={{ fontSize: 12, fontWeight: 700, color: 'var(--ink)', marginBottom: 6 }}>📜 적용 법령 본문</div>
                    {dp.laws_applied?.map((law) => (
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
            )}
          </>
        )
      })()}

      {/* RAG 보조 — 정확한 자료는 Step3 decision_pack(룰별 법령 셋트). relevance ≥0.6만 노출 */}
      {(() => {
        const ragFiltered = step1Result.rag_sources.filter((s) => (s.relevance_score ?? 0) >= 0.6)
        if (ragFiltered.length === 0) return null
        return (
          <details className="s2-sec" id="s-ref" style={{ opacity: 0.85 }}>
            <summary style={{ cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 8, padding: '8px 0', fontSize: 'var(--text-sm)', color: 'var(--text-secondary)', fontWeight: 500 }}>
              <Icon name="book-open" size={13} />
              <span>🔎 RAG 검색 결과 (보조 · 정확한 자료는 다음 단계 '참조규정' 박스)</span>
              <span style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--text-tertiary)' }}>{ragFiltered.length}건</span>
            </summary>
            <div className="ref-list" style={{ marginTop: 8 }}>
              {ragFiltered.map((s) => {
                const st = (s as any).source_type || 'guide'
                const meta = SRC_TYPE_META[st] || SRC_TYPE_META.guide
                return (
                  <button
                    key={s.chunk_id}
                    type="button"
                    className="ref-row"
                    onClick={() => openSrc({
                      section_title: s.section_title,
                      excerpt: s.excerpt,
                      relevance_score: s.relevance_score,
                      chunk_id: s.chunk_id,
                      source_type: st,
                    })}
                  >
                    <span className="ref-ic" style={{ background: meta.toneBg, color: meta.toneFg }}>
                      <Icon name={meta.icon} size={13} />
                    </span>
                    <span className="ref-title">{s.section_title}</span>
                    <span className="ref-rel">{Math.round(s.relevance_score * 100)}%</span>
                    <Icon name="arrow-up-right" size={13} className="ref-go" />
                  </button>
                )
              })}
            </div>
          </details>
        )
      })()}

      {error && (
        <div style={{
          background: 'var(--danger-soft)', border: '1px solid rgba(179,35,24,0.28)',
          color: 'var(--danger)', padding: '10px 14px', borderRadius: 'var(--radius-lg)',
          fontSize: 'var(--text-xs)', fontWeight: 600,
        }}>{error}</div>
      )}

      {step2Done && <FeedbackBox sessionId={sessionId} />}

      {/* 2026-06-02 F5-1c: 현재 선택 라이브 표시 — 사용자가 어떤 옵션을 확정 직전인지 명확 */}
      {step1Result?.candidates?.[0] && (() => {
        const selRule = step1Result.candidates.find(c => c.rule_id === selectedRuleId)
        const altSel = selectedAltKind && step1Result.practice_alternatives?.find(a => a.kind === selectedAltKind)
        const bidder = (conditions as any)['_bidder_selection']
        const regionKeys = Object.keys(conditions).filter(k => k.endsWith('_region'))
        return (
          <div style={{
            marginTop: 4, padding: '10px 14px',
            background: 'var(--accent-soft)', border: '1px solid rgba(20,73,122,0.25)',
            borderRadius: 10, fontSize: 12, color: 'var(--accent-secondary)',
          }}>
            <div style={{ fontWeight: 800, marginBottom: 4 }}>📌 현재 선택 (확정 시 의견서에 반영)</div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, fontSize: 11 }}>
              <span><b>계약방법</b>: {altSel ? altSel.method : (selRule?.method || '—')}</span>
              {bidder && <span>· <b>낙찰자결정</b>: {bidder}</span>}
              {regionKeys.length > 0 && regionKeys.map(rk => (
                <span key={rk}>· <b>지역제한</b>: {(conditions as any)[rk] || '(미입력)'}</span>
              ))}
              {Object.entries(conditions).filter(([k, v]) => v === true && !k.startsWith('_')).map(([k]) => (
                <span key={k}>· {k} 적용</span>
              ))}
            </div>
          </div>
        )
      })()}

      <div style={{ display: 'flex', gap: 10, marginTop: 4 }}>
        <button
          type="button"
          onClick={() => setStep(1)}
          style={{
            flex: '0 0 auto', fontFamily: 'inherit', fontSize: 'var(--text-sm)', fontWeight: 700,
            color: 'var(--text-secondary)', background: 'var(--bg-secondary)',
            border: '1px solid var(--border-medium)', borderRadius: 'var(--radius-xl)',
            padding: '13px 18px', cursor: 'pointer',
          }}
        >← 이전</button>
        <button
          type="button"
          className="fl-cta"
          onClick={handleSubmit}
          disabled={isLoading}
          style={{ flex: 1 }}
        >
          {isLoading ? '최종 분석 중...' : '최종 계약방법 확정'} <Icon name="arrow-right" size={17} />
        </button>
      </div>

      {/* F34 (2026-06-11): 중기간 경쟁제품 웹 검색 모달 */}
      <SmeProductSearchModal open={smeSearchOpen} onClose={() => setSmeSearchOpen(false)} />
    </div>
  )
}
