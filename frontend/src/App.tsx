import { useState, useEffect, useRef } from 'react'
import { useWizardStore } from './store/wizardStore'
import Icon from './components/Icon'
import ChatSidebar from './components/ChatSidebar'
import AuthButton from './components/AuthButton'
import Step1Page from './pages/Step1Page'
import Step2Page from './pages/Step2Page'
import Step3Page from './pages/Step3Page'
import Step4Page from './pages/Step4Page'
import AdminPage from './pages/AdminPage'
import AskPage from './pages/AskPage'
import GlossaryPage from './pages/GlossaryPage'
// 룰트리는 mermaid(대용량) 의존 → 열 때만 지연 로드(초기 번들 경량 유지)
// 룰 결정트리는 비공개 전환(2026-07-29) — RuleTreePage 진입점 제거, API도 admin 전용
import HomeDashboard from './pages/HomeDashboard'
import { SourceDrawerProvider } from './components/SourceDrawer'
import { submitFeedback } from './api/client'
import { track } from './lib/track'
import type { Step1Input } from './types'

const APP_SESSION_ID = `app-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`

// 로그인 복귀 컨텍스트(?return=ask|decide) — 채팅에서 로그인해도 홈으로 떨어지지 않게
// 원래 화면으로 되돌린다(login()이 심는 쿼리). 소비 후 URL에서 제거.
// ⚠️ 모듈 레벨 즉시실행 금지: import가 main.tsx의 captureAuthFromHash()보다 먼저 돌아
// replaceState가 #access_token 해시를 지워버린다 — 첫 렌더(useState 초기화) 시점 지연 소비.
let _loginReturn: string | null | undefined
function consumeLoginReturn(): string | null {
  if (_loginReturn === undefined) {
    const p = new URLSearchParams(window.location.search)
    const r = p.get('return')
    if (r) {
      p.delete('return')
      const qs = p.toString()
      history.replaceState(null, '', window.location.pathname + (qs ? `?${qs}` : '') + (r === 'decide' ? '#decide' : ''))
    }
    _loginReturn = r
  }
  return _loginReturn
}

const ALLOWED_EXT = ['.csv', '.xlsx', '.xls', '.pdf', '.jpg', '.jpeg', '.png', '.txt', '.json', '.docx', '.doc']
const MAX_FILE_BYTES = 10 * 1024 * 1024

function FeedbackModal({ onClose }: { onClose: () => void }) {
  const [rating, setRating] = useState<1 | -1 | 0>(0)
  const [comment, setComment] = useState('')
  const [attachment, setAttachment] = useState<File | null>(null)
  const [fileError, setFileError] = useState<string | null>(null)
  const [sent, setSent] = useState(false)
  const [sending, setSending] = useState(false)
  // 2026-06-02: 의견 작성 시점의 wizard 컨텍스트 자동 첨부
  const { currentStep, step1Input, step1Result, step2Result } = useWizardStore()

  // 2026-06-02 F3-4: 드래그 가능한 모달 위치 (localStorage 저장)
  const [pos, setPos] = useState<{ x: number; y: number } | null>(() => {
    try {
      const raw = localStorage.getItem('cc_feedback_modal_pos')
      return raw ? JSON.parse(raw) : null
    } catch { return null }
  })
  const dragRef = useRef<{ dx: number; dy: number; dragging: boolean }>({ dx: 0, dy: 0, dragging: false })

  const onDragStart = (e: React.MouseEvent) => {
    const target = e.currentTarget.getBoundingClientRect()
    dragRef.current = {
      dx: e.clientX - target.left,
      dy: e.clientY - target.top,
      dragging: true,
    }
    document.body.style.userSelect = 'none'
  }
  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!dragRef.current.dragging) return
      const nx = Math.max(0, Math.min(window.innerWidth - 100, e.clientX - dragRef.current.dx))
      const ny = Math.max(0, Math.min(window.innerHeight - 80, e.clientY - dragRef.current.dy))
      setPos({ x: nx, y: ny })
    }
    const onUp = () => {
      if (dragRef.current.dragging) {
        dragRef.current.dragging = false
        document.body.style.userSelect = ''
        try {
          if (pos) localStorage.setItem('cc_feedback_modal_pos', JSON.stringify(pos))
        } catch {}
      }
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
    return () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
    }
  }, [pos])

  const onFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0]
    if (!f) {
      setAttachment(null)
      setFileError(null)
      return
    }
    const ext = '.' + (f.name.split('.').pop() || '').toLowerCase()
    if (!ALLOWED_EXT.includes(ext)) {
      setFileError(`허용되지 않은 형식 (${ext})`)
      setAttachment(null)
      return
    }
    if (f.size > MAX_FILE_BYTES) {
      setFileError(`파일이 너무 큽니다 (${(f.size / 1024 / 1024).toFixed(1)}MB / 최대 10MB)`)
      setAttachment(null)
      return
    }
    setFileError(null)
    setAttachment(f)
  }

  const handleSubmit = async () => {
    if (!comment.trim() && rating === 0 && !attachment) return
    setSending(true)
    try {
      await submitFeedback({
        session_id: APP_SESSION_ID,
        rating,
        comment: comment.trim() || undefined,
        feedback_type: 'general',
        attachment: attachment ?? undefined,
        // 화면 컨텍스트 자동 첨부 — 사용자가 어떤 사업·금액·결과 보며 의견 줬는지 기록
        page: typeof window !== 'undefined' ? (document.querySelector('[data-screen-label]')?.getAttribute('data-screen-label') || undefined) : undefined,
        step: currentStep ? String(currentStep) : undefined,
        project_name: step1Input?.project_name || undefined,
        contract_type: step1Input?.contract_type || undefined,
        estimated_price: step1Input?.estimated_price || undefined,
        description: step1Input?.description || undefined,
        suggested_method: step1Result?.candidates?.[0]?.method || undefined,
        final_method: step2Result?.method || undefined,
        rule_id: step1Result?.candidates?.[0]?.rule_id || undefined,
      })
      setSent(true)
      setTimeout(onClose, 1500)
    } catch {
      alert('의견 전송에 실패했습니다. 잠시 후 다시 시도해 주세요.')
    } finally {
      setSending(false)
    }
  }

  // F3-4: pos가 있으면 절대 위치, 없으면 중앙 정렬 (초기 기본)
  const innerStyle: React.CSSProperties = pos
    ? { position: 'fixed', left: pos.x, top: pos.y, maxWidth: '28rem', width: '100%' }
    : {}

  return (
    <div className={`fixed inset-0 z-50 ${pos ? '' : 'bg-black/60 flex items-center justify-center p-4'}`} onClick={pos ? undefined : onClose}>
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-md" style={innerStyle} onClick={(e) => e.stopPropagation()}>
        <div
          className="flex items-center justify-between px-5 py-4 border-b border-gray-100"
          onMouseDown={onDragStart}
          style={{ cursor: 'move', userSelect: 'none' }}
          title="드래그해서 이동"
        >
          <h2 className="text-base font-bold text-gray-900">💡 의견 보내기 <span style={{ fontSize: 11, fontWeight: 400, color: 'var(--ink-3)' }}>· 드래그 이동</span></h2>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 text-xl leading-none">✕</button>
        </div>
        {sent ? (
          <div className="p-8 text-center">
            <p className="text-3xl mb-2">✓</p>
            <p className="text-sm text-gray-700">소중한 의견 감사합니다.</p>
          </div>
        ) : (
          <div className="p-5 space-y-3">
            <p className="text-xs text-gray-500">불편하거나 개선하면 좋을 점, 추가하고 싶은 기능을 알려주세요.</p>
            <div className="flex gap-2">
              <button
                onClick={() => setRating(1)}
                className={`flex-1 text-xs rounded-lg py-2 border transition-colors ${
                  rating === 1 ? 'bg-green-50 border-green-300 text-green-700' : 'bg-gray-50 border-gray-200 text-gray-500 hover:border-green-300'
                }`}
              >
                👍 만족
              </button>
              <button
                onClick={() => setRating(-1)}
                className={`flex-1 text-xs rounded-lg py-2 border transition-colors ${
                  rating === -1 ? 'bg-red-50 border-red-300 text-red-700' : 'bg-gray-50 border-gray-200 text-gray-500 hover:border-red-300'
                }`}
              >
                👎 불편
              </button>
            </div>
            <textarea
              value={comment}
              onChange={(e) => setComment(e.target.value)}
              placeholder="자유롭게 의견을 적어주세요..."
              rows={4}
              className="w-full text-sm border border-gray-200 rounded-lg px-3 py-2 outline-none focus:border-blue-300 resize-none"
            />

            {/* 파일 첨부 */}
            <div>
              <label className="block text-xs font-semibold text-gray-600 mb-1.5">
                📎 데이터 파일 첨부 <span className="text-gray-400 font-normal">(선택, 최대 10MB)</span>
              </label>
              {!attachment ? (
                <label className="block border border-dashed border-gray-300 hover:border-blue-300 rounded-lg px-3 py-2.5 cursor-pointer text-center transition-colors">
                  <span className="text-xs text-gray-500">클릭하여 파일 선택</span>
                  <input
                    type="file"
                    accept={ALLOWED_EXT.join(',')}
                    onChange={onFileChange}
                    className="hidden"
                  />
                </label>
              ) : (
                <div className="flex items-center gap-2 bg-blue-50 border border-blue-200 rounded-lg px-3 py-2">
                  <span className="text-xs text-blue-700 flex-1 truncate" title={attachment.name}>
                    📄 {attachment.name}
                  </span>
                  <span className="text-[10px] text-blue-500 shrink-0">
                    {(attachment.size / 1024).toFixed(1)}KB
                  </span>
                  <button
                    onClick={() => { setAttachment(null); setFileError(null) }}
                    className="text-blue-400 hover:text-red-600 text-xs"
                    title="제거"
                  >
                    ✕
                  </button>
                </div>
              )}
              {fileError && <p className="text-xs text-red-600 mt-1">{fileError}</p>}
              <p className="text-[10px] text-gray-400 mt-1">
                허용: CSV · Excel · PDF · 이미지(JPG/PNG) · TXT · JSON
              </p>
            </div>

            <div className="flex justify-end gap-2">
              <button
                onClick={onClose}
                className="text-xs text-gray-500 hover:text-gray-700 px-3 py-1.5"
              >
                취소
              </button>
              <button
                onClick={handleSubmit}
                disabled={sending || (!comment.trim() && rating === 0 && !attachment)}
                className="text-xs bg-blue-600 hover:bg-blue-700 disabled:bg-gray-300 text-white rounded-lg px-3 py-1.5 transition-colors"
              >
                {sending ? '전송 중...' : '보내기'}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

const HISTORY_KEY = 'cc_analysis_history'
const FAVORITES_KEY = 'cc_favorites'

export type HistoryEntry = {
  ts: string
  project_name: string
  contract_type: string
  estimated_price: number
  method?: string
  input: Partial<Step1Input>
}

export function loadHistory(): HistoryEntry[] {
  try { return JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]') } catch { return [] }
}

export function saveHistory(entry: HistoryEntry) {
  const hist = loadHistory()
  hist.unshift(entry)
  localStorage.setItem(HISTORY_KEY, JSON.stringify(hist.slice(0, 20)))
}

export function loadFavorites(): { name: string; input: Partial<Step1Input> }[] {
  try { return JSON.parse(localStorage.getItem(FAVORITES_KEY) || '[]') } catch { return [] }
}

export function saveFavorite(name: string, input: Partial<Step1Input>) {
  const favs = loadFavorites()
  favs.unshift({ name, input })
  localStorage.setItem(FAVORITES_KEY, JSON.stringify(favs.slice(0, 10)))
}

export function deleteFavorite(name: string) {
  const favs = loadFavorites().filter((f) => f.name !== name)
  localStorage.setItem(FAVORITES_KEY, JSON.stringify(favs))
}

// ── 데스크탑 2-col shell — 디자이너 flow-shell.jsx 이식 ──

const CONTRACT_LABEL: Record<string, string> = {
  service: '용역', construction: '공사', product: '물품',
}

function priceText(price: number): string {
  if (!price) return ''
  const eok = Math.floor(price / 100_000_000)
  const man = Math.floor((price % 100_000_000) / 10_000)
  return [eok ? `${eok}억` : '', man ? `${man.toLocaleString()}만` : ''].filter(Boolean).join(' ') + '원'
}

function scrollToId(id: string) {
  const el = document.getElementById(id)
  if (!el) return
  const y = el.getBoundingClientRect().top + window.scrollY - 84
  window.scrollTo({ top: y, behavior: 'smooth' })
}

function TopStepper({ step, onJump }: { step: number; onJump: (n: number) => void }) {
  const labels = ['입력', 'AI 분석', '결정 요약']
  return (
    <div className="dt-steps">
      {labels.map((l, i) => {
        const n = i + 1
        const state = n < step ? 'done' : n === step ? 'active' : 'todo'
        return (
          <div key={l} style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
            <button className={`dt-step ${state}`} onClick={() => onJump(n)}>
              <span className="dt-step-dot">{state === 'done' ? <Icon name="check" size={13} strokeWidth={3} /> : n}</span>
              <span className="dt-step-label">{l}</span>
            </button>
            {i < labels.length - 1 && <span className={`dt-step-line ${n < step ? 'done' : ''}`} />}
          </div>
        )
      })}
    </div>
  )
}

const STEP2_NAV = [
  { id: 's-type', n: '0', label: '계약유형' },
  { id: 's-class', n: '1', label: '분류·중기간' },
  { id: 's-method', n: '3', label: '계약방법' },
  { id: 's-restrict', n: '4', label: '제한경쟁' },
  { id: 's-ref', n: '·', label: '참조 규정' },
]

function Rail({ step, step1Input }: { step: number; step1Input: Partial<Step1Input> }) {
  const [active, setActive] = useState<string | null>(null)

  useEffect(() => {
    if (step !== 2) return
    const ids = STEP2_NAV.map((x) => x.id)
    const onScroll = () => {
      let cur: string | null = null
      for (const id of ids) {
        const el = document.getElementById(id)
        if (el && el.getBoundingClientRect().top < 160) cur = id
      }
      setActive(cur)
    }
    window.addEventListener('scroll', onScroll, { passive: true })
    onScroll()
    return () => window.removeEventListener('scroll', onScroll)
  }, [step])

  const ctLabel = step1Input.contract_type ? CONTRACT_LABEL[step1Input.contract_type] : ''

  return (
    <aside className="dt-rail">
      {step >= 2 && (
        <div className="rail-sum">
          <span className="rail-sum-kicker">분석 대상</span>
          <p className="rail-sum-name">{step1Input.project_name || '(사업명 없음)'}</p>
          <p className="rail-sum-price">
            {priceText(step1Input.estimated_price || 0)}
            {ctLabel ? ` · ${ctLabel}` : ''}
          </p>
          {step1Input.description && <p className="rail-sum-desc">{step1Input.description}</p>}
        </div>
      )}

      {step === 2 && (
        <nav className="rail-nav">
          <span className="rail-nav-title">페이지 구성</span>
          {STEP2_NAV.map((x) => (
            <button key={x.id} className={`rail-nav-item ${active === x.id ? 'on' : ''}`} onClick={() => scrollToId(x.id)}>
              <span className="rail-nav-n">{x.n}</span>
              <span className="rail-nav-label">{x.label}</span>
            </button>
          ))}
        </nav>
      )}

      {step >= 3 && (
        <div className="rail-tip">
          <Icon name="check-circle" size={18} />
          <p>분석이 완료되었습니다. 결정 요약 리포트를 인쇄해 결재·검토에 활용하세요.</p>
        </div>
      )}

      <div className="rail-foot">
        <Icon name="info" size={13} />
        <span>AI는 부정확할 수 있습니다. 중요한 결정 시 법령·실무 기준을 확인하세요.</span>
      </div>
    </aside>
  )
}

function HistoryPanel({ onSelect, onClose }: { onSelect: (entry: HistoryEntry) => void; onClose: () => void }) {
  const history = loadHistory()
  const favorites = loadFavorites()

  return (
    <div className="fixed inset-0 z-50 bg-black/60 flex items-start justify-center p-4 overflow-y-auto overscroll-contain" onClick={onClose}>
      <div className="bg-white rounded-xl shadow-2xl max-w-lg w-full mt-8 mb-8" onClick={(e) => e.stopPropagation()}>
        <div className="flex justify-between items-center px-5 py-4 border-b border-gray-100">
          <h2 className="text-base font-bold text-gray-800">최근 분석 / 즐겨찾기</h2>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600">✕</button>
        </div>
        <div className="p-5 space-y-5">
          {favorites.length > 0 && (
            <div>
              <p className="text-xs font-semibold text-yellow-600 mb-2">⭐ 즐겨찾기</p>
              <div className="space-y-1">
                {favorites.map((f, i) => (
                  <button key={i} onClick={() => { onSelect({ ts: '', project_name: f.name, contract_type: f.input.contract_type ?? '', estimated_price: f.input.estimated_price ?? 0, input: f.input }); onClose() }}
                    className="w-full text-left bg-yellow-50 hover:bg-yellow-100 border border-yellow-200 rounded-lg px-3 py-2 text-sm text-gray-700"
                  >
                    {f.name}
                  </button>
                ))}
              </div>
            </div>
          )}

          {history.length > 0 ? (
            <div>
              <p className="text-xs font-semibold text-gray-500 mb-2">최근 분석 (최대 20건)</p>
              <div className="space-y-1 max-h-80 overflow-y-auto">
                {history.map((h, i) => (
                  <button key={i} onClick={() => { onSelect(h); onClose() }}
                    className="w-full text-left bg-gray-50 hover:bg-blue-50 border border-gray-200 rounded-lg px-3 py-2 text-sm"
                  >
                    <div className="flex justify-between">
                      <span className="font-medium text-gray-800">{h.project_name || '(사업명 없음)'}</span>
                      <span className="text-xs text-gray-400">{h.ts ? new Date(h.ts).toLocaleDateString('ko-KR') : ''}</span>
                    </div>
                    <div className="text-xs text-gray-500 mt-0.5">
                      {h.contract_type === 'service' ? '용역' : h.contract_type === 'product' ? '물품' : '공사'}
                      {h.estimated_price ? ` · ${(h.estimated_price / 100_000_000).toFixed(1)}억원` : ''}
                      {h.method ? ` · ${h.method}` : ''}
                    </div>
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <p className="text-sm text-gray-400 text-center py-4">분석 히스토리가 없습니다.</p>
          )}
        </div>
      </div>
    </div>
  )
}

export default function App() {
  const { currentStep, reset, setStep, step1Input, setStep1Input } = useWizardStore()
  const [showAdmin, setShowAdmin] = useState(false)
  const [showHistory, setShowHistory] = useState(false)
  const [showAsk, setShowAsk] = useState(() => consumeLoginReturn() === 'ask')
  const [showGlossary, setShowGlossary] = useState(false)
  const [showFeedback, setShowFeedback] = useState(false)
  // 홈/대시보드 — 해시 없음('') 또는 '#home'이 홈, 위저드는 '#decide'로 진입.
  const isHomeHash = (h: string) => h === '' || h === '#home'
  const [showHome, setShowHome] = useState(() =>
    // 채팅 복귀(return=ask) 시 홈 오버레이(z 1000)가 Ask 모달(z 50)을 덮으므로 홈 생략
    consumeLoginReturn() !== 'ask' &&
    typeof window !== 'undefined' && isHomeHash(window.location.hash)
  )
  useEffect(() => {
    const onHash = () => {
      setShowHome(isHomeHash(window.location.hash))
    }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  // 단계 전환 시 화면 맨 위로 (6/20 사용자 의견) — 자동/수동 전환 모두 중앙에서 처리
  useEffect(() => {
    window.scrollTo({ top: 0, behavior: 'auto' })
  }, [currentStep])

  // 홈↔위저드 전환도 같은 문서를 스크롤하므로(T-2026W33-178 이후) 전환 시 맨 위로.
  // 안 하면 위저드 중간에서 홈으로 오면 홈이 중간부터 보인다.
  useEffect(() => {
    window.scrollTo({ top: 0, behavior: 'auto' })
  }, [showHome])

  // 전환 ④: 결정 요약(step4) 도달 = 위저드 완주. 단계 전환 경로가 여럿(Step3 버튼·
  // 스테퍼 점프)이라 각 호출지가 아니라 여기서 한 번만 잡는다. 뒤로 갔다 다시 오면
  // 재발화하지만 umami 이벤트는 세션 단위로 집계되므로 판정에 해롭지 않다.
  useEffect(() => {
    if (currentStep === 4) track('wizard-complete')
  }, [currentStep])

  const handleHistorySelect = (entry: HistoryEntry) => {
    reset()
    setStep1Input(entry.input)
  }

  const renderStep = () => {
    switch (currentStep) {
      case 1: return <Step1Page />
      case 2: return <Step2Page />
      case 3: return <Step3Page />
      case 4: return <Step4Page />
      default: return <Step1Page />
    }
  }

  const showRail = currentStep >= 2
  const jumpStep = (n: number) => {
    setStep(n as 1 | 2 | 3 | 4)
    // scroll-to-top은 currentStep 변경 감지 useEffect가 중앙 처리
  }

  return (
    <SourceDrawerProvider>
    <div className={`dt-app step-${currentStep}${showHome ? ' home-open' : ''}`}>
      <header className="dt-top">
        <button
          onClick={() => { window.location.hash = '#home' }}
          className="dt-brand"
          style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 0, fontFamily: 'inherit' }}
          title="홈으로"
        >
          <span className="dt-brand-glyph"><Icon name="scale" size={17} strokeWidth={2.4} /></span>
          <div className="dt-brand-text">
            <span className="dt-brand-name">계약나침반</span>
            <span className="dt-brand-sub">공공계약 방법 결정 도우미</span>
          </div>
        </button>
        <TopStepper step={Math.min(currentStep, 3)} onJump={jumpStep} />
        <div className="dt-top-right" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <AuthButton />
          <button className="dt-help" onClick={() => setShowFeedback(true)}>
            <Icon name="message-circle" size={15} /> <span>의견 보내기</span>
          </button>
        </div>
      </header>

      <div className={`dt-cols ${showRail ? 'has-rail' : 'no-rail'}`}>
        <ChatSidebar />
        {showRail && <Rail step={currentStep} step1Input={step1Input} />}
        <main className="dt-main">
          <div className={`dt-content ${currentStep === 1 ? 'narrow' : ''}`}>
            {currentStep === 1 ? (
              <div className="bg-white rounded-2xl shadow-sm border border-gray-100" style={{ padding: 24 }}>
                {renderStep()}
              </div>
            ) : (
              renderStep()
            )}

            <div className="flex justify-center gap-3 mt-2 flex-wrap" style={{ paddingTop: 8 }}>
              {currentStep > 1 && (
                <button onClick={reset} className="text-xs text-gray-400 hover:text-gray-600 underline">
                  처음부터 다시 시작
                </button>
              )}
              <button onClick={() => setShowAsk(true)} className="text-xs text-blue-500 hover:text-blue-700 font-semibold underline">
                💬 계약 Q&A
              </button>
              <button onClick={() => setShowGlossary(true)} className="text-xs text-purple-500 hover:text-purple-700 font-semibold underline">
                📖 용어사전
              </button>
              <button onClick={() => setShowHistory(true)} className="text-xs text-gray-400 hover:text-blue-600 underline">
                최근 분석 / 즐겨찾기
              </button>
              <button onClick={() => setShowAdmin(true)} className="text-xs text-gray-400 hover:text-gray-600 underline">
                관리자
              </button>
            </div>
          </div>
        </main>
      </div>

      {showHistory && (
        <HistoryPanel
          onSelect={handleHistorySelect}
          onClose={() => setShowHistory(false)}
        />
      )}
      {showAdmin && <AdminPage onClose={() => setShowAdmin(false)} />}
      {showAsk && <AskPage onClose={() => setShowAsk(false)} />}
      {showGlossary && <GlossaryPage onClose={() => setShowGlossary(false)} />}
      {showHome && <HomeDashboard
        // 전환 ①: 홈 카드에서 위저드/Q&A로 들어간 순간. 방문(pageview)과 '쓰기 시작'을
        // 가르는 지점이라 유입 품질 판정의 분모가 된다.
        onDecision={() => { track('wizard-start'); setShowHome(false); window.location.hash = '#decide' }}
        onAsk={() => { track('ask-open', { from: 'home' }); setShowAsk(true) }}
        onGlossary={() => { setShowGlossary(true) }} />}
      {showFeedback && <FeedbackModal onClose={() => setShowFeedback(false)} />}

      {/* 홈에서는 숨긴다 — 예전 홈은 고정 오버레이라 이 FAB을 덮고 있었다. 문서 스크롤로
          바꾸면서(T-2026W33-178) 드러나 표준 피드백 위젯(index.html)과 겹쳐 보였다. */}
      {!showHome && (
      <button
        onClick={() => setShowFeedback(true)}
        title="의견 보내기"
        className="fixed bottom-6 right-6 z-40 bg-white hover:bg-blue-50 border border-gray-200 hover:border-blue-300 text-gray-600 hover:text-blue-700 rounded-full shadow-lg px-4 py-2.5 text-sm font-medium transition-colors"
      >
        💡 의견
      </button>
      )}
    </div>
    </SourceDrawerProvider>
  )
}
