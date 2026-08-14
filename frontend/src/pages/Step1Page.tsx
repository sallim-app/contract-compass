import { useState, useEffect } from 'react'
import { useWizardStore } from '../store/wizardStore'
import { filterStep1, suggestContractType, getPossibleMethods, type MatrixMethod } from '../api/client'
import { saveFavorite, loadFavorites, deleteFavorite } from '../App'
import { track } from '../lib/track'
import Icon from '../components/Icon'
import type { OrgType, ServiceType } from '../types'

// 기관유형 3택 — 적용 법령·기준이 달라짐 (국가계약법 / 지방계약법 / 공기업·준정부기관 계약사무규칙)
const ORG_OPTIONS: { value: OrgType; label: string; hint: string }[] = [
  { value: 'national', label: '국가기관', hint: '국가를 당사자로 하는 계약에 관한 법률' },
  { value: 'local', label: '지방자치단체', hint: '지방자치단체를 당사자로 하는 계약에 관한 법률' },
  { value: 'public_corp', label: '공기업·준정부기관', hint: '공기업·준정부기관 계약사무규칙 (+국가계약법 준용)' },
]

// 디자이너(Zippt) v2 시안 적용 — flow-step1.jsx의 .fl-body·.fld·.preset/.fl-cta 구조 재사용.
// 비즈니스 로직(useWizardStore + filterStep1 + suggestContractType + 선이동/백그라운드)은 그대로 유지.

// 2026-06-01 사용자 의견 F2-4: 2천만·5천만·1억·2.3억·7.1억 추가 (고시금액 경계값)
const PRICE_PRESETS = [
  { label: '2천만', value: 20_000_000 },
  { label: '5천만', value: 50_000_000 },
  { label: '1억', value: 100_000_000 },
  { label: '2.3억', value: 230_000_000 },
  { label: '3억', value: 300_000_000 },
  { label: '5억', value: 500_000_000 },
  { label: '7.1억', value: 710_000_000 },
  { label: '10억', value: 1_000_000_000 },
  { label: '30억', value: 3_000_000_000 },
  { label: '50억', value: 5_000_000_000 },
  { label: '100억', value: 10_000_000_000 },
  { label: '300억', value: 30_000_000_000 },
]

const ONBOARDING_KEY = 'cc_onboarding_dismissed'

// 클라이언트 키워드 추론 — LLM 호출 전 즉시 Step2 이동용 임시 contract_type.
// 백그라운드 LLM 결과로 confidence·reason 보강 + 필요 시 정정.
function inferContractType(desc: string): 'construction' | 'service' | 'product' {
  const s = desc.toLowerCase()
  // 2026-06-02 F4-7: SVC 강신호 우선순위 도입 — "감리용역" 같은 명백한 service가 description의 "공사" 단어로 인해 construction으로 잘못 분류되는 문제 해결
  const SVC_STRONG = ['감리','컨설팅','정보시스템','유지관리','정보보호','보안관제','전산','SI','연구용역']
  if (SVC_STRONG.some(k => s.includes(k))) return 'service'
  const CST = ['공사','증설','신설','개량','보수공사','정비공사','전기공사','통신공사','소방','관로','관망','토목','건축','시공']
  // F3-1: SVC 키워드 보강
  const SVC = ['용역','연구','진단','운영','관리','점검','교육','교재',
    '소프트웨어 개발','sw개발','시스템 개발','분석','조사','평가',
    '데이터','클라우드','DB','데이터베이스',
    '시스템운영','시스템관리','정보화','웹','앱','어플리케이션']
  if (CST.some(k => s.includes(k))) return 'construction'
  if (SVC.some(k => s.includes(k))) return 'service'
  return 'product'  // 기본값 — 가장 흔함
}

// 단순노무용역 자동 감지 (시행규칙 제23조의3 — 경비·청소·시설물관리 등).
// 소액수의 낙찰하한율이 일반 용역(87.995%)과 다른 89.995% 적용.
// 정밀 키워드 — '유지관리'(기술용역) 등 오탐 방지를 위해 '관리'/'시설관리' 등 광의어 제외
const SIMPLE_LABOR_KW = ['경비', '청소', '미화', '방호', '시설물관리', '위탁관리', '환경관리']
function inferSimpleLabor(desc: string): boolean {
  return SIMPLE_LABOR_KW.some(k => desc.includes(k))
}

// 용역 세부 종류 키워드 추천 (6/20 사용자 의견 "기술/기타용역 추천").
// 계약대장에 용역 세부분류 라벨 컬럼이 없어 "라벨 통계"는 불가 → 도메인 키워드 룰로 추천.
// 회귀 방지: 명백한 비-기술 신호가 있을 때만 분류하고, 신호 없으면 기존 기본값 'technical' 유지.
// (적격심사 점수·별표가 service_type에 따라 달라지므로 기본값을 함부로 바꾸지 않음)
const ST_IT = ['정보시스템', '소프트웨어', 'sw개발', 'sw 개발', '전산', '정보화', '홈페이지', '웹사이트', '어플리케이션', '애플리케이션', '데이터베이스', '클라우드', '정보보호', '보안관제', 'si구축']
const ST_ACADEMIC = ['연구', '학술', '조사연구', '타당성', '용역과제', '실태조사', '기본계획수립', '정책연구']
const ST_OTHER = ['청소', '미화', '경비', '방호', '시설물관리', '위탁관리', '환경관리', '단순노무', '운반', '급식', '행사대행']
const ST_TECHNICAL = ['감리', '진단', '검사', '점검', '설계', 'cm', '계측', '시험', '안전점검', '정밀안전']
export function inferServiceType(desc: string): ServiceType {
  const s = desc.toLowerCase()
  // 기술용역 강신호는 다른 분류보다 우선 (감리·진단 등은 IT/연구와 겹쳐도 기술용역)
  if (ST_TECHNICAL.some(k => s.includes(k))) return 'technical'
  if (ST_IT.some(k => s.includes(k))) return 'it_service'
  if (ST_ACADEMIC.some(k => s.includes(k))) return 'academic'
  if (ST_OTHER.some(k => s.includes(k))) return 'other'
  return 'technical'  // 신호 없음 → 기존 기본값 유지 (회귀 방지)
}

function priceText(price: number): string {
  if (!price) return ''
  const eok = Math.floor(price / 100_000_000)
  const man = Math.floor((price % 100_000_000) / 10_000)
  return [eok ? `${eok}억` : '', man ? `${man.toLocaleString()}만` : ''].filter(Boolean).join(' ') + '원'
}

export default function Step1Page() {
  const { step1Input, setStep1Input, setStep1Result, setSessionId, setStep, setLoading, setError, isLoading, error } = useWizardStore()

  const [projectName, setProjectName] = useState(step1Input.project_name || '')
  const [description, setDescription] = useState(step1Input.description || '')
  const [price, setPrice] = useState<number>(step1Input.estimated_price || 0)
  const [orgType, setOrgType] = useState<OrgType>(step1Input.org_type || 'public_corp')
  const [favorites, setFavorites] = useState(loadFavorites())
  const [onboardOpen, setOnboardOpen] = useState(() => {
    try { return !localStorage.getItem(ONBOARDING_KEY) } catch { return true }
  })
  const dismissOnboard = () => {
    try { localStorage.setItem(ONBOARDING_KEY, '1') } catch {}
    setOnboardOpen(false)
  }

  useEffect(() => { setFavorites(loadFavorites()) }, [])

  // F13-1 (2026-06-09): 금액·키워드 변경 시 가능한 계약방법 라이브 조회
  const [possibleMethods, setPossibleMethods] = useState<MatrixMethod[]>([])
  useEffect(() => {
    if (!price || description.trim().length < 5) {
      setPossibleMethods([])
      return
    }
    const ct = inferContractType(description.trim())
    const tid = setTimeout(() => {
      getPossibleMethods(ct, price).then((d) => setPossibleMethods(d.methods)).catch(() => null)
    }, 350)
    return () => clearTimeout(tid)
  }, [price, description])

  const handleSubmit = async () => {
    if (!projectName || !price) {
      setError('사업명과 추정가격을 입력해주세요.')
      return
    }
    if (description.trim().length < 5) {
      setError('사업개요를 5자 이상 입력해주세요 — 계약유형 자동 판단에 사용됩니다.')
      return
    }
    setError(null)
    setLoading(true)

    // 클라이언트 키워드 추론으로 즉시 contract_type 확정 → Step2 즉시 이동
    // LLM(suggestContractType)·filterStep1은 백그라운드로 → 대기 체감 0
    const desc = description.trim()
    const ctGuess = inferContractType(desc)
    const simpleLabor = inferSimpleLabor(desc)
    const stGuess = inferServiceType(desc)
    const input = {
      contract_type: ctGuess,
      estimated_price: price,
      org_type: orgType,
      service_type: ctGuess === 'service' ? stGuess : undefined,
      construction_specialty: ctGuess === 'construction' ? ('general' as const) : undefined,
      is_sme_competition_product: false,
      // 단순노무용역(경비·청소·시설물관리 등) 자동 감지 — 용역일 때만, 소액수의 89.995%
      is_simple_labor: ctGuess === 'service' && simpleLabor,
      project_name: projectName,
      description: desc,
      suggested_contract_type: ctGuess,
      suggested_confidence: 0.7,  // 키워드 추론 baseline, LLM 결과로 보강
      suggested_reason: '키워드 자동 추론 — AI 분석 진행 중',
    }
    setStep1Input(input)
    setStep(2)  // 즉시 이동
    // 전환 ②: 입력 검증을 통과해 분석에 들어간 순간(= 위저드를 실제로 쓴 사람).
    // 사업명·사업개요는 자유입력이라 넘기지 않는다 — 분류값만.
    track('wizard-submit', { contract_type: ctGuess, org_type: orgType })

    // 백그라운드 1: LLM contract_type 보강
    suggestContractType(desc)
      .then((sug) => {
        const refined = { ...input, contract_type: sug.suggested,
          service_type: sug.suggested === 'service' ? stGuess : undefined,
          construction_specialty: sug.suggested === 'construction' ? ('general' as const) : undefined,
          is_simple_labor: sug.suggested === 'service' && simpleLabor,
          suggested_contract_type: sug.suggested,
          suggested_confidence: sug.confidence,
          suggested_reason: sug.reason }
        setStep1Input(refined)
      })
      .catch(() => { /* LLM 실패해도 키워드 추론으로 계속 진행 */ })

    // 백그라운드 2: filterStep1 (rule engine)
    filterStep1(input)
      .then((result) => {
        setStep1Result(result)
        setSessionId(result.session_id)
        // 전환 ③: 룰엔진 판정이 실제로 손에 들어온 순간. wizard-submit과의 차이가
        // 백엔드 실패율이다 — 두 이벤트가 갈라지면 판정 API가 조용히 죽고 있다는 신호.
        track('wizard-result', { candidates: result.candidates?.length ?? 0 })
      })
      .catch((e: any) => {
        setError(e.response?.data?.detail || '오류가 발생했습니다.')
        setStep(1)
        track('wizard-error')
      })
      .finally(() => setLoading(false))
  }

  return (
    <div className="fl-body" style={{ padding: 0 }}>
      {/* 온보딩 카드 — 시안의 .ob-card */}
      {onboardOpen ? (
        <div className="ob-card">
          <button className="ob-close" onClick={dismissOnboard} aria-label="닫기"><Icon name="x" size={16} /></button>
          <div className="ob-head"><Icon name="sparkles" size={15} /> 처음 사용하시나요?</div>
          <ol className="ob-list">
            <li><b>사업명·사업개요·추정가격</b> 3가지만 입력하세요</li>
            <li><b>AI 분석</b>을 누르면 계약유형부터 분류·계약방법·제한경쟁까지 한 번에 추천합니다</li>
            <li>추천 결과는 다음 화면에서 <b>확인·변경</b>할 수 있습니다</li>
          </ol>
        </div>
      ) : (
        <button onClick={() => setOnboardOpen(true)} className="dl-link" style={{ color: 'var(--accent-primary)' }}>
          <Icon name="sparkles" size={13} /> 사용 방법 다시 보기
        </button>
      )}

      {/* 기관유형 — 3택, 기본 공기업·준정부기관 */}
      <div className="fld">
        <label className="fld-label">
          기관유형 <span className="fld-hint">적용 법령·기준이 달라집니다</span>
        </label>
        <div className="spec-row" style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
          {ORG_OPTIONS.map((o) => {
            const sel = orgType === o.value
            return (
              <button
                key={o.value}
                type="button"
                title={o.hint}
                className={`preset ${sel ? 'on' : ''}`}
                onClick={() => { setOrgType(o.value); setStep1Input({ org_type: o.value }) }}
              >
                {sel ? '✓ ' : ''}{o.label}
              </button>
            )
          })}
        </div>
      </div>

      {/* 사업명 */}
      <div className="fld">
        <div className="fld-head">
          <label className="fld-label">사업명</label>
          {projectName && price > 0 && (
            <button
              type="button"
              className="fld-fav"
              onClick={() => {
                const name = `${projectName} (${(price / 100_000_000).toFixed(1)}억)`
                saveFavorite(name, { estimated_price: price, project_name: projectName, description })
                setFavorites(loadFavorites())
              }}
            >
              <Icon name="tag" size={12} /> 즐겨찾기 저장
            </button>
          )}
        </div>
        <input
          className="fl-input"
          value={projectName}
          onChange={(e) => setProjectName(e.target.value)}
          placeholder="예: 댐 정밀안전진단 용역"
        />
        {favorites.length > 0 && !projectName && (
          <div className="fav-row">
            {favorites.map((f, i) => (
              <span key={i} style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                <button
                  type="button"
                  className="fav-chip"
                  onClick={() => {
                    setPrice(f.input.estimated_price || 0)
                    setProjectName(f.input.project_name || '')
                    setDescription(f.input.description || '')
                  }}
                >
                  <Icon name="repeat" size={11} /> {f.name}
                </button>
                <button
                  type="button"
                  onClick={() => { deleteFavorite(f.name); setFavorites(loadFavorites()) }}
                  style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-quaternary)', fontSize: 12 }}
                  aria-label="즐겨찾기 삭제"
                >✕</button>
              </span>
            ))}
          </div>
        )}
      </div>

      {/* 사업개요 */}
      <div className="fld">
        <label className="fld-label">
          사업개요 <span className="fld-req">필수 · 계약유형·분류 자동 판단</span>
        </label>
        <div className="ta-wrap">
          <textarea
            className="fl-textarea"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={3}
            placeholder="예: PLC 판넬 4면 제조구매설치 / 정수장 운영관리 / 상수관로 신설공사"
          />
          <span className="ta-ai"><Icon name="sparkles" size={13} /></span>
        </div>
        {/* F34 (2026-06-11): 중기간 경쟁제품 검색 — Step2로 이동 (사용자 의견 반영) */}
        <p className="dl-link" style={{ color: 'var(--ink-3)', fontSize: 11 }}>
          <Icon name="info" size={12} /> 중기간 경쟁제품은 다음 단계(2페이지)에서 웹 검색·다운로드 가능합니다
        </p>
      </div>

      {/* 추정가격 — 부가세 제외 금액 기준 */}
      <div className="fld">
        <label className="fld-label">
          추정가격 <span className="fld-hint">원 단위 · 부가세 제외 금액</span>
        </label>
        <div className="price-display">{price ? priceText(price) : '금액을 선택하거나 직접 입력하세요'}</div>
        <input
          type="text"
          inputMode="numeric"
          className="price-input"
          placeholder="예: 350,000,000 (원 단위, 쉼표 자동)"
          value={price ? price.toLocaleString() : ''}
          onChange={(e) => {
            const digits = e.target.value.replace(/[^\d]/g, '')
            setPrice(digits ? Number(digits) : 0)
          }}
          aria-label="추정가격 직접 입력 (원, 부가세 제외)"
        />
        <div className="preset-row">
          {PRICE_PRESETS.map((p) => (
            <button
              key={p.value}
              type="button"
              className={`preset ${price === p.value ? 'on' : ''}`}
              onClick={() => setPrice(p.value)}
            >
              {p.label}
            </button>
          ))}
        </div>
        {/* F13-1 (2026-06-09): 금액·키워드 기준 가능 계약방법 라이브 칩 — primary vs alternative 구분 */}
        {possibleMethods.length > 0 && (
          <div style={{
            marginTop: 10, padding: 10,
            background: 'var(--brand-tint)',
            border: '1px solid var(--line-2)',
            borderRadius: 8,
          }}>
            <p style={{ fontSize: 11, fontWeight: 800, color: 'var(--ink)', margin: '0 0 6px' }}>
              💡 이 금액에서 가능한 계약방법 ({possibleMethods.length}개)
            </p>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
              {possibleMethods.slice(0, 10).map((m, i) => {
                const kind = m.kind ?? (i === 0 ? 'primary' : 'alternative')
                const isPrimary = kind === 'primary'
                return (
                  <span key={i} title={m.reason || ''} style={{
                    fontSize: 11, fontWeight: isPrimary ? 700 : 600,
                    padding: '3px 8px', borderRadius: 999,
                    background: isPrimary ? 'var(--brand)' : '#fff',
                    color: isPrimary ? '#fff' : 'var(--ink)',
                    border: isPrimary ? 'none' : `1px ${kind === 'default' ? 'dashed' : 'solid'} var(--line-2)`,
                    opacity: kind === 'default' ? 0.75 : 1,
                  }}>
                    {isPrimary ? '✓ ' : (kind === 'default' ? '○ ' : '+ ')}{m.method}
                  </span>
                )
              })}
              {possibleMethods.length > 10 && (
                <span style={{ fontSize: 11, color: 'var(--brand)' }}>+{possibleMethods.length - 10}</span>
              )}
            </div>
            <p style={{ fontSize: 10, color: 'var(--text-tertiary)', marginTop: 4, margin: 0 }}>
              <b>✓ 1순위(AI 추천)</b> · <b>+ 실무 옵션</b>(사정 선택) · <b>○ 시행령 7조 default</b>(일반경쟁)
            </p>
          </div>
        )}
      </div>

      {error && (
        <div style={{
          background: 'var(--danger-soft)', border: '1px solid rgba(239,68,68,0.28)',
          color: 'var(--danger)', padding: '10px 14px', borderRadius: 'var(--radius-lg)',
          fontSize: 'var(--text-xs)', fontWeight: 600,
        }}>{error}</div>
      )}

      <div className="fl-cta-wrap">
        <button className="fl-cta" onClick={handleSubmit} disabled={isLoading}>
          <Icon name="sparkles" size={18} /> {isLoading ? 'AI 분석 중...' : 'AI 분석 시작'}
        </button>
        <p className="fl-disclaimer">AI는 부정확할 수 있습니다. 중요한 결정 시 법령·실무 기준을 확인하세요.</p>
      </div>
    </div>
  )
}
