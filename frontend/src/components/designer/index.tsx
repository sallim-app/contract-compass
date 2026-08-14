import type { CSSProperties } from 'react'
import Icon from '../Icon'

// 디자이너(Zippt AI) 공용 컴포넌트 — ShowcasePage 외 다른 페이지에서도 재사용.
// ds-tokens.css 변수와 app.css 클래스를 사용한다 (이미 글로벌 import).

// ───────────────────────────────────────────────────────────────
// Tone 매핑 — semantic 의미 → CSS 변수 묶음
// ───────────────────────────────────────────────────────────────
export const TONES: Record<string, { fg: string; bg: string; solid: string; line: string }> = {
  accent:  { fg: 'var(--accent-primary)', bg: 'var(--accent-soft)',    solid: 'var(--accent-primary)', line: 'rgba(20,73,122,0.25)' },
  success: { fg: 'var(--success)',        bg: 'var(--success-soft)',   solid: 'var(--success)',        line: 'rgba(15,107,79,0.28)' },
  warning: { fg: 'var(--med-ink)',               bg: 'var(--warning-soft)',   solid: 'var(--warning)',        line: 'rgba(150,101,11,0.30)' },
  danger:  { fg: 'var(--danger)',         bg: 'var(--danger-soft)',    solid: 'var(--danger)',         line: 'rgba(179,35,24,0.28)' },
  info:    { fg: 'var(--cat-compare-fg)', bg: 'var(--cat-compare-bg)', solid: 'var(--info)',           line: 'rgba(14,116,144,0.28)' },
  purple:  { fg: 'var(--cat-complex-fg)', bg: 'var(--cat-complex-bg)', solid: '#3F5B6B',               line: 'rgba(63,91,107,0.26)' },
  rose:    { fg: 'var(--cat-forecast-fg)',bg: 'var(--cat-forecast-bg)',solid: '#e11d48',               line: 'rgba(225,29,72,0.26)' },
  neutral: { fg: 'var(--text-tertiary)',  bg: 'var(--bg-tertiary)',    solid: 'var(--text-tertiary)',  line: 'var(--border-medium)' },
}
export const tone = (t?: string) => TONES[t ?? 'neutral'] ?? TONES.neutral

// ───────────────────────────────────────────────────────────────
// Sparkline (작은 트렌드 선)
// ───────────────────────────────────────────────────────────────
export function Sparkline({
  data, color, w = 132, h = 34, id,
}: { data: number[]; color: string; w?: number; h?: number; id: string }) {
  const min = Math.min(...data), max = Math.max(...data)
  const range = max - min || 1
  const pts = data.map((v, i) => [(i / (data.length - 1)) * w, h - 3 - ((v - min) / range) * (h - 6)] as [number, number])
  const line = pts.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(' ')
  const area = `${line} L${w} ${h} L0 ${h} Z`
  const gid = `spark-${id}`
  return (
    <svg viewBox={`0 0 ${w} ${h}`} width={w} height={h} className="spark" preserveAspectRatio="none">
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.22" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={area} fill={`url(#${gid})`} />
      <path d={line} fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx={pts[pts.length - 1][0]} cy={pts[pts.length - 1][1]} r="2.6" fill={color} />
    </svg>
  )
}

// ───────────────────────────────────────────────────────────────
// SectionHead — 섹션 제목·아이콘·서브타이틀·카운트 배지
// ───────────────────────────────────────────────────────────────
export function SectionHead({
  icon, title, sub, n,
}: { icon: string; title: string; sub?: string; n?: string | number }) {
  return (
    <div className="sec-head">
      <span className="sec-head-icon"><Icon name={icon} size={18} strokeWidth={2.2} /></span>
      <div>
        <h2 className="sec-title">{title}</h2>
        {sub && <p className="sec-sub">{sub}</p>}
      </div>
      {n != null && <span className="sec-count">{n}</span>}
    </div>
  )
}

// ───────────────────────────────────────────────────────────────
// MetricCard — 큰 숫자 + 진행 막대 + 보조 텍스트 + (옵션) description hover tooltip
// ───────────────────────────────────────────────────────────────
export function MetricCard({
  label, value, sub, pct, tone: tn = 'neutral', description,
}: { label: string; value: string; sub?: string; pct?: number; tone?: string; description?: string }) {
  const t = tone(tn)
  return (
    <div
      className="metric-card group"
      style={{ position: 'relative', cursor: description ? 'help' : 'default' }}
    >
      <p className="metric-label" style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
        {label}
        {description && <span style={{ color: 'var(--text-tertiary)', fontSize: '11px' }}>ⓘ</span>}
      </p>
      <p className="metric-value">{value}</p>
      {pct != null
        ? <div className="metric-bar-track"><div className="metric-bar" style={{ width: `${pct}%`, background: t.solid }} /></div>
        : <div className="metric-bar-spacer" />}
      {sub && <p className="metric-sub">{sub}</p>}
      {description && (
        <div
          className="hidden group-hover:block"
          style={{
            position: 'absolute', left: 0, right: 0, top: '100%', marginTop: '4px',
            background: '#0f172a', color: 'white', fontSize: '11px', lineHeight: 1.5,
            borderRadius: '8px', padding: '10px 12px', boxShadow: '0 8px 16px rgba(0,0,0,0.18)',
            zIndex: 50, pointerEvents: 'none',
          }}
        >{description}</div>
      )}
    </div>
  )
}

// ───────────────────────────────────────────────────────────────
// Badge — semantic tone에 맞는 작은 배지
// ───────────────────────────────────────────────────────────────
export function Badge({
  children, tone: tn = 'neutral', icon, size = 'sm',
}: { children: React.ReactNode; tone?: string; icon?: string; size?: 'sm' | 'md' }) {
  const t = tone(tn)
  const pad = size === 'md' ? '5px 10px' : '3px 8px'
  const fz = size === 'md' ? '12px' : '11px'
  return (
    <span
      className="ds-badge"
      style={{ background: t.bg, color: t.fg, padding: pad, fontSize: fz, borderRadius: '999px', display: 'inline-flex', alignItems: 'center', gap: '4px', fontWeight: 600 }}
    >
      {icon && <Icon name={icon} size={size === 'md' ? 14 : 12} strokeWidth={2.4} />}
      {children}
    </span>
  )
}

// ───────────────────────────────────────────────────────────────
// Card — 디자이너 톤의 일반 카드 컨테이너
// ───────────────────────────────────────────────────────────────
export function Card({
  children, tone: tn, padding = 'md', className = '', style,
}: { children: React.ReactNode; tone?: string; padding?: 'sm' | 'md' | 'lg'; className?: string; style?: CSSProperties }) {
  const pad = padding === 'lg' ? '20px' : padding === 'sm' ? '12px' : '16px'
  const t = tn ? tone(tn) : null
  return (
    <div
      className={`ds-card ${className}`}
      style={{
        background: 'var(--bg-secondary)',
        border: t ? `1px solid ${t.line}` : '1px solid var(--border-light)',
        borderRadius: '12px',
        padding: pad,
        ...style,
      }}
    >
      {children}
    </div>
  )
}

// ───────────────────────────────────────────────────────────────
// MilestoneCard — 큰 도약 카드 (before → after)
// ───────────────────────────────────────────────────────────────
const IMPACT_META: Record<string, { tone: string; label: string }> = {
  critical: { tone: 'accent', label: 'CRITICAL' },
  major: { tone: 'info', label: 'MAJOR' },
  milestone: { tone: 'success', label: 'MILESTONE' },
}
const MILESTONE_ICON_BY_TITLE = (title: string): string => {
  if (title.includes('정답') || title.includes('정확도') || title.includes('도약')) return 'target'
  if (title.includes('제한경쟁') || title.includes('alternatives')) return 'scale'
  if (title.includes('RAG') || title.includes('검색')) return 'search'
  if (title.includes('Cold start') || title.includes('전환')) return 'zap'
  if (title.includes('내부사규') || title.includes('한계')) return 'landmark'
  if (title.includes('입력') || title.includes('UX')) return 'sparkles'
  if (title.includes('데이터') || title.includes('확장')) return 'database'
  return 'check-circle'
}

export type MilestoneData = {
  date: string
  title: string
  metric_before?: string | null
  metric_after?: string | null
  summary: string
  impact?: 'critical' | 'major' | 'milestone'
}

export function MilestoneCard({ m }: { m: MilestoneData }) {
  const imp = IMPACT_META[m.impact ?? 'milestone'] ?? IMPACT_META.milestone
  const t = tone(imp.tone)
  return (
    <div className="ms-card" style={{ '--mt': t.solid, '--mt-bg': t.bg } as CSSProperties}>
      <div className="ms-rail" />
      <div className="ms-body">
        <div className="ms-top">
          <span className="ms-icon" style={{ background: t.bg, color: t.solid }}>
            <Icon name={MILESTONE_ICON_BY_TITLE(m.title)} size={18} strokeWidth={2.2} />
          </span>
          <div className="ms-top-text">
            <span className="ms-impact" style={{ color: t.solid }}>{imp.label}</span>
            <h3 className="ms-title">{m.title}</h3>
          </div>
          <span className="ms-date">{(m.date || '').replace(/^2026-/, '').replace('-', '.')}</span>
        </div>
        {(m.metric_before || m.metric_after) && (
          <div className="ms-delta">
            {m.metric_before && <span className="ms-before">{m.metric_before}</span>}
            {m.metric_before && m.metric_after && <Icon name="arrow-right" size={14} className="ms-arrow" />}
            {m.metric_after && <span className="ms-after" style={{ color: t.solid, borderColor: t.line }}>{m.metric_after}</span>}
          </div>
        )}
        <p className="ms-summary">{m.summary}</p>
      </div>
    </div>
  )
}

// ───────────────────────────────────────────────────────────────
// Donut — SimilarCases·기타 분포 시각화용
// ───────────────────────────────────────────────────────────────
export function Donut({
  segments, size = 108,
}: { segments: { ratio: number; tone: string }[]; size?: number }) {
  const r = 15.915
  let offset = 25
  return (
    <svg viewBox="0 0 42 42" width={size} height={size} className="donut">
      <circle cx="21" cy="21" r={r} fill="none" stroke="var(--bg-tertiary)" strokeWidth="5" />
      {segments.map((s, i) => {
        const len = s.ratio * 100
        const dash = `${len} ${100 - len}`
        const el = (
          <circle key={i} cx="21" cy="21" r={r} fill="none"
            stroke={tone(s.tone).solid} strokeWidth="5"
            strokeDasharray={dash} strokeDashoffset={offset} className="donut-seg" />
        )
        offset -= len
        return el
      })}
    </svg>
  )
}
