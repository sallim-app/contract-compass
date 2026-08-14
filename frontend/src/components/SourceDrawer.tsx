import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from 'react'
import Icon from './Icon'
import PdfViewer from './PdfViewer'

// 디자이너(Zippt AI) 디자인 시스템 — semantic tone 매핑 (ds-tokens.css 변수 사용)
const TONES: Record<string, { fg: string; bg: string; solid: string; line: string }> = {
  accent:  { fg: 'var(--accent-primary)', bg: 'var(--accent-soft)',    solid: 'var(--accent-primary)', line: 'rgba(20,73,122,0.25)' },
  success: { fg: 'var(--success)',        bg: 'var(--success-soft)',   solid: 'var(--success)',        line: 'rgba(15,107,79,0.28)' },
  warning: { fg: 'var(--med-ink)',               bg: 'var(--warning-soft)',   solid: 'var(--warning)',        line: 'rgba(150,101,11,0.30)' },
  danger:  { fg: 'var(--danger)',         bg: 'var(--danger-soft)',    solid: 'var(--danger)',         line: 'rgba(179,35,24,0.28)' },
  info:    { fg: 'var(--cat-compare-fg)', bg: 'var(--cat-compare-bg)', solid: 'var(--info)',           line: 'rgba(14,116,144,0.28)' },
  purple:  { fg: 'var(--cat-complex-fg)', bg: 'var(--cat-complex-bg)', solid: 'var(--violet)',               line: 'rgba(63,91,107,0.26)' },
  rose:    { fg: 'var(--cat-forecast-fg)',bg: 'var(--cat-forecast-bg)',solid: 'var(--high)',               line: 'rgba(225,29,72,0.26)' },
  neutral: { fg: 'var(--text-tertiary)',  bg: 'var(--bg-tertiary)',    solid: 'var(--text-tertiary)',  line: 'var(--border-medium)' },
}
const tone = (t?: string) => TONES[t ?? 'neutral'] ?? TONES.neutral

const SOURCE_TYPE: Record<string, { icon: string; tone: string; label: string }> = {
  law:      { icon: 'scale',          tone: 'info',    label: '법령' },
  internal: { icon: 'landmark',       tone: 'purple',  label: '내부 사규' },
  textbook: { icon: 'book-open',      tone: 'accent',  label: '참고자료' },
  faq:      { icon: 'message-circle', tone: 'success', label: 'FAQ' },
  guide:    { icon: 'file-text',      tone: 'neutral', label: '가이드' },
  admin:    { icon: 'file-text',      tone: 'neutral', label: '행정' },
}
const srcMeta = (t?: string) => SOURCE_TYPE[t ?? ''] ?? { icon: 'file-text', tone: 'neutral', label: '문서' }

export type DrawerSource = {
  title: string
  content: string
  subtitle?: string
  relevance?: number  // 0~1
  badges?: { label: string; tone?: string }[]
  externalLink?: { href: string; label: string }
  sourceType?: string  // textbook/law/internal/faq/guide/admin
  highlight?: string   // 답변 인용 구간(원문 내 정확 일치 문자열)
  documentId?: string  // 원본 PDF 표시용 — backend /api/v1/docs/source/{id} 매핑
}

type Ctx = {
  source: DrawerSource | null
  open: (src: DrawerSource) => void
  close: () => void
  splitMode: boolean
  setSplitMode: (v: boolean) => void
}

const SourceDrawerContext = createContext<Ctx | null>(null)

export function useSourceDrawer(): Ctx {
  const ctx = useContext(SourceDrawerContext)
  if (!ctx) throw new Error('useSourceDrawer는 SourceDrawerProvider 안에서만')
  return ctx
}

export function SourceDrawerProvider({ children }: { children: ReactNode }) {
  const [source, setSource] = useState<DrawerSource | null>(null)
  const [splitMode, setSplitMode] = useState(false)
  const open = useCallback((src: DrawerSource) => setSource(src), [])
  const close = useCallback(() => setSource(null), [])
  return (
    <SourceDrawerContext.Provider value={{ source, open, close, splitMode, setSplitMode }}>
      {children}
      <SourceDrawerPanel />
    </SourceDrawerContext.Provider>
  )
}

function OriginalText({ content, highlight }: { content: string; highlight?: string }) {
  if (!highlight || !content.includes(highlight)) {
    return <p className="src-body">{content}</p>
  }
  const [before, after] = content.split(highlight)
  return (
    <p className="src-body">
      {before}
      <mark className="src-mark">{highlight}</mark>
      {after}
    </p>
  )
}

function Relevance({ value }: { value: number }) {
  const pct = Math.round(value * 100)
  return (
    <div className="src-rel" title={`관련도 ${pct}%`}>
      <svg width="40" height="40" viewBox="0 0 36 36" className="src-rel-ring">
        <circle cx="18" cy="18" r="15" fill="none" stroke="var(--border-light)" strokeWidth="3" />
        <circle cx="18" cy="18" r="15" fill="none" stroke="var(--success)" strokeWidth="3"
          strokeLinecap="round" strokeDasharray={`${(pct / 100) * 94.2} 94.2`}
          transform="rotate(-90 18 18)" />
      </svg>
      <span className="src-rel-pct">{pct}<span>%</span></span>
    </div>
  )
}

function DrawerInner({ source, onClose, embedded }: { source: DrawerSource; onClose?: () => void; embedded?: boolean }) {
  const meta = srcMeta(source.sourceType)
  const t = tone(meta.tone)
  const [pdfOpen, setPdfOpen] = useState(false)
  return (
    <div className="src-inner">
      <header className="src-head">
        <div className="src-head-icon" style={{ background: t.bg, color: t.fg }}>
          <Icon name={meta.icon} size={22} strokeWidth={2.2} />
        </div>
        <div className="src-head-text">
          <div className="src-head-kicker" style={{ color: t.fg }}>{meta.label}</div>
          <p className="src-head-title">{source.title}</p>
          {source.subtitle && <p className="src-head-sub">{source.subtitle}</p>}
        </div>
        {!embedded && (
          <button className="src-close" onClick={onClose} aria-label="닫기">
            <Icon name="x" size={20} />
          </button>
        )}
      </header>

      {(source.badges?.length || source.relevance != null) && (
        <div className="src-meta">
          {source.relevance != null && (
            <div className="src-rel-group">
              <Relevance value={source.relevance} />
              <span className="src-rel-cap">관련도</span>
            </div>
          )}
          <div className="src-badges">
            {source.badges?.map((b, i) => {
              const bt = tone(b.tone)
              return <span key={i} className="src-badge" style={{ background: bt.bg, color: bt.fg }}>{b.label}</span>
            })}
          </div>
        </div>
      )}

      <div className="src-scroll">
        <div className="src-orig-label">
          <span>원문</span>
          {source.highlight && (
            <span className="src-match-hint">
              <span className="src-match-dot" /> 답변 인용 구간 강조
            </span>
          )}
        </div>
        <OriginalText content={source.content} highlight={source.highlight} />
      </div>

      {(source.documentId || source.externalLink) && (
        <footer className="src-foot" style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          {source.documentId && (
            <button
              onClick={() => setPdfOpen(true)}
              className="src-ext"
              style={{ background: 'var(--accent-soft)', color: 'var(--accent-secondary)', border: 'none', cursor: 'pointer', fontFamily: 'inherit' }}
            >
              <Icon name="file-text" size={15} />
              원문 PDF 보기
            </button>
          )}
          {source.externalLink && (
            <a href={source.externalLink.href} target="_blank" rel="noreferrer" className="src-ext">
              <Icon name="external-link" size={15} />
              {source.externalLink.label}
            </a>
          )}
        </footer>
      )}
      {pdfOpen && source.documentId && (
        <PdfViewer
          documentId={source.documentId}
          searchText={source.highlight || source.content?.slice(0, 80)}
          onClose={() => setPdfOpen(false)}
        />
      )}
    </div>
  )
}

function SourceDrawerPanel() {
  const { source, close, splitMode } = useSourceDrawer()
  const [drag, setDrag] = useState(0)
  const startX = useRef<number | null>(null)
  if (!source || splitMode) return null

  const onTouchStart = (e: React.TouchEvent) => { startX.current = e.touches[0].clientX }
  const onTouchMove = (e: React.TouchEvent) => {
    if (startX.current == null) return
    const dx = e.touches[0].clientX - startX.current
    if (dx > 0) setDrag(dx)
  }
  const onTouchEnd = () => {
    if (drag > 90) close()
    setDrag(0); startX.current = null
  }

  return (
    <>
      <div className="src-scrim" onClick={close} />
      <aside
        className="src-panel"
        style={drag ? { transform: `translateX(${drag}px)`, transition: 'none' } : undefined}
        onTouchStart={onTouchStart} onTouchMove={onTouchMove} onTouchEnd={onTouchEnd}
      >
        <div className="src-grabber" aria-hidden="true" />
        <DrawerInner source={source} onClose={close} />
      </aside>
    </>
  )
}

export function SourceInlinePanel({ source }: { source: DrawerSource | null }) {
  if (!source) {
    return (
      <div className="src-empty">
        <div className="src-empty-art" aria-hidden="true">
          <div className="src-empty-doc">
            <span className="src-empty-line w70" />
            <span className="src-empty-line w90" />
            <span className="src-empty-line w50" />
            <span className="src-empty-line w80" />
            <span className="src-empty-line w40" />
          </div>
          <div className="src-empty-cursor"><Icon name="arrow-right" size={16} /></div>
        </div>
        <p className="src-empty-title">출처를 클릭하면 원문이 여기에 표시됩니다</p>
        <p className="src-empty-sub">분할 보기 모드 — 답변과 법령 원문을 나란히 비교하세요</p>
      </div>
    )
  }
  return <div className="src-inline"><DrawerInner source={source} embedded /></div>
}
