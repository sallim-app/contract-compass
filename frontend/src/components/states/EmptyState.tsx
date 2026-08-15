// 디자이너(Zippt) 빈 상태 컴포넌트 — search · bookmark · history 변형.
// 일러스트는 SVG 인라인 (Zippt 팔레트: 오렌지 accent + slate neutrals).
// 출처: design_preview_v2/states.jsx

import type { ReactNode } from 'react'

// ── SVG illustrations ────────────────────────────────────────────────
function IllustEmptySearch() {
  return (
    <svg viewBox="0 0 120 100" className="illust" width="132" height="110">
      <ellipse cx="60" cy="90" rx="40" ry="6" fill="var(--bg-tertiary)" />
      <rect x="30" y="22" width="46" height="58" rx="7" fill="var(--bg-secondary)" stroke="var(--border-medium)" strokeWidth="2" />
      <line x1="38" y1="36" x2="62" y2="36" stroke="var(--border-strong)" strokeWidth="2.5" strokeLinecap="round" />
      <line x1="38" y1="46" x2="56" y2="46" stroke="var(--border-medium)" strokeWidth="2.5" strokeLinecap="round" />
      <line x1="38" y1="56" x2="60" y2="56" stroke="var(--border-medium)" strokeWidth="2.5" strokeLinecap="round" />
      <circle cx="74" cy="62" r="17" fill="var(--accent-soft)" stroke="var(--accent-primary)" strokeWidth="3" />
      <line x1="86" y1="74" x2="96" y2="84" stroke="var(--accent-primary)" strokeWidth="4" strokeLinecap="round" />
      <line x1="68" y1="62" x2="80" y2="62" stroke="var(--accent-primary)" strokeWidth="2.5" strokeLinecap="round" />
    </svg>
  )
}
function IllustEmptyBookmark() {
  return (
    <svg viewBox="0 0 120 100" className="illust" width="132" height="110">
      <ellipse cx="60" cy="90" rx="38" ry="6" fill="var(--bg-tertiary)" />
      <path d="M44 24h32a4 4 0 0 1 4 4v50l-20-12-20 12V28a4 4 0 0 1 4-4z" fill="var(--bg-secondary)" stroke="var(--border-medium)" strokeWidth="2" strokeLinejoin="round" />
      <path d="M60 40l3.2 6.5 7.2 1-5.2 5.1 1.2 7.1-6.4-3.4-6.4 3.4 1.2-7.1-5.2-5.1 7.2-1z" fill="var(--accent-soft)" stroke="var(--accent-primary)" strokeWidth="2" strokeLinejoin="round" />
    </svg>
  )
}
function IllustEmptyHistory() {
  return (
    <svg viewBox="0 0 120 100" className="illust" width="132" height="110">
      <ellipse cx="60" cy="90" rx="38" ry="6" fill="var(--bg-tertiary)" />
      <circle cx="60" cy="50" r="30" fill="var(--bg-secondary)" stroke="var(--border-medium)" strokeWidth="2" />
      <circle cx="60" cy="50" r="22" fill="none" stroke="var(--border-light)" strokeWidth="1.5" strokeDasharray="3 4" />
      <line x1="60" y1="50" x2="60" y2="34" stroke="var(--accent-primary)" strokeWidth="3" strokeLinecap="round" />
      <line x1="60" y1="50" x2="72" y2="56" stroke="var(--accent-primary)" strokeWidth="3" strokeLinecap="round" />
      <circle cx="60" cy="50" r="3" fill="var(--accent-primary)" />
    </svg>
  )
}

export type EmptyVariant = 'search' | 'bookmark' | 'history'

const EMPTY_VARIANTS: Record<EmptyVariant, {
  illust: () => JSX.Element
  title: string
  sub: string
}> = {
  search:   { illust: IllustEmptySearch,   title: '검색 결과가 없습니다',     sub: '다른 키워드로 다시 검색해 보세요.' },
  bookmark: { illust: IllustEmptyBookmark, title: '아직 북마크가 없습니다',   sub: '분석 결과에서 북마크 버튼을 눌러 저장하세요.' },
  history:  { illust: IllustEmptyHistory,  title: '최근 이력이 없습니다',     sub: '분석한 사업이 여기에 시간순으로 쌓입니다.' },
}

export type EmptyStateProps = {
  variant?: EmptyVariant
  title?: ReactNode
  sub?: ReactNode
  action?: { label: string; onClick: () => void }
}

export default function EmptyState({ variant = 'search', title, sub, action }: EmptyStateProps) {
  const v = EMPTY_VARIANTS[variant] || EMPTY_VARIANTS.search
  const Illust = v.illust
  return (
    <div className="empty-state">
      <Illust />
      <p className="empty-title">{title ?? v.title}</p>
      <p className="empty-sub">{sub ?? v.sub}</p>
      {action && (
        <button className="empty-action" onClick={action.onClick}>{action.label}</button>
      )}
    </div>
  )
}
