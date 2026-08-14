import { useState, useRef, useEffect } from 'react'
import { submitFeedback } from '../api/client'
import { getDeviceId } from '../lib/deviceId'
import { authHeaders, login } from '../lib/auth'
import { track } from '../lib/track'
import AnnotatedText from '../components/AnnotatedText'
import { useSourceDrawer, SourceInlinePanel } from '../components/SourceDrawer'
import { tone } from '../components/designer'
import Icon from '../components/Icon'
import { EmptyState } from '../components/states'
import type { AskSource } from '../types'

// ── Types ───────────────────────────────────────────────────────────
interface Message {
  role: 'user' | 'assistant'
  text: string
  sources?: AskSource[]
  followUps?: string[]
  loading?: boolean
  question?: string
  feedbackSent?: 'up' | 'down'
  // 2026-06-01: 답변 신뢰도 — confidence 경고 + 환각 의심 조문
  unverifiedCitations?: string[]
  avgRelevance?: number | null
  // 2026-07-29: 익명 무료 소진/토큰 만료 — 로그인 유도 버튼 표시
  loginRequired?: boolean
}

const SESSION_ID = `qna-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`

// 출처 타입 → 디자이너 아이콘/톤 매핑 (SourceDrawer 내부와 동일 정책)
const SOURCE_META: Record<string, { icon: string; tone: string; label: string }> = {
  law:      { icon: 'scale',          tone: 'info',    label: '법령' },
  internal: { icon: 'landmark',       tone: 'purple',  label: '내부 사규' },
  textbook: { icon: 'book-open',      tone: 'accent',  label: '참고자료' },
  faq:      { icon: 'message-circle', tone: 'success', label: 'FAQ' },
  guide:    { icon: 'file-text',      tone: 'neutral', label: '가이드' },
}
const srcMeta = (t?: string) => SOURCE_META[t ?? ''] ?? { icon: 'file-text', tone: 'neutral', label: '문서' }

// ── Categories (live API 데이터에 사용) ──────────────────────────────
const CATEGORIES = [
  {
    id: 'method', label: '계약방법', glyph: '📋',
    questions: [
      '수의계약을 할 수 있는 경우는 언제인가요?',
      '소액수의계약 한도금액이 얼마인가요?',
      '협상에 의한 계약 적용 요건이 뭔가요?',
      '제한경쟁입찰 지역제한 요건이 뭔가요?',
    ],
  },
  {
    id: 'amount', label: '금액·보증금', glyph: '💰',
    questions: [
      '계약보증금률은 얼마인가요?',
      '선금 지급 한도는 얼마까지 가능한가요?',
      '하자보수보증금률은 어떻게 되나요?',
      '지체상금률 기준이 어떻게 되나요?',
    ],
  },
  {
    id: 'bid', label: '입찰·공고', glyph: '📢',
    questions: [
      '입찰공고 기간은 최소 몇 일인가요?',
      '적격심사 낙찰하한율은 얼마인가요?',
      '입찰참가자격 사전심사(PQ) 기준은?',
      '5억 이상 용역 계약 시 주요 절차는?',
    ],
  },
  {
    id: 'perform', label: '계약이행', glyph: '🔨',
    questions: [
      '물가변동으로 계약금액 조정 요건이 뭔가요?',
      '장기계속계약과 계속비계약의 차이는?',
      '검사 기간은 며칠 이내에 완료해야 하나요?',
      '하자담보책임기간은 얼마나 되나요?',
    ],
  },
  {
    id: 'design', label: '설계변경', glyph: '✏️',
    questions: [
      '설계변경 시 신규비목 단가는 어떻게 결정하나요?',
      '발주기관 요구로 설계변경할 때 협의단가는 어떻게 산정하나요?',
      '계약상대자 귀책사유로 설계변경 시 계약금액 조정 방법은?',
      '협상계약에서 설계변경 시 계약금액 조정 방법은?',
    ],
  },
  {
    id: 'sanction', label: '제재·관리', glyph: '⚠️',
    questions: [
      '부정당업자 제재 사유와 제재 기간은?',
      '계약이행보증금 납부 기준은?',
      '5천만원 이하 물품 계약 방법은?',
      '선금 지급 신청 절차가 어떻게 되나요?',
    ],
  },
]

const LOADING_MESSAGES = [
  '가이드·법령 검색 중',
  '관련 조문 확인 중',
  '내부 규정 대조 중',
  '답변 생성 중',
]

function getFollowUps(question: string): string[] {
  if (/설계변경|신규비목|협의단가|단가 결정/.test(question))
    return CATEGORIES.find(c => c.id === 'perform')!.questions.slice(0, 2)
  if (/수의계약|소액|경쟁입찰|협상계약/.test(question))
    return CATEGORIES.find(c => c.id === 'amount')!.questions.slice(0, 2)
  if (/보증금|선금|지체상금/.test(question))
    return CATEGORIES.find(c => c.id === 'bid')!.questions.slice(0, 2)
  if (/입찰|공고|적격심사|낙찰/.test(question))
    return CATEGORIES.find(c => c.id === 'perform')!.questions.slice(0, 2)
  if (/검사|하자|장기계속|물가변동/.test(question))
    return CATEGORIES.find(c => c.id === 'design')!.questions.slice(0, 2)
  if (/부정당|제재|이행보증/.test(question))
    return CATEGORIES.find(c => c.id === 'method')!.questions.slice(0, 2)
  return CATEGORIES[1].questions.slice(0, 2)
}

// ── Bookmarks (localStorage, 50건 한도) ─────────────────────────────
const BOOKMARKS_KEY = 'cc_qna_bookmarks'

type Bookmark = {
  ts: number
  question: string
  answer: string
  sources?: AskSource[]
}

function loadBookmarks(): Bookmark[] {
  try { return JSON.parse(localStorage.getItem(BOOKMARKS_KEY) || '[]') } catch { return [] }
}
function saveBookmark(b: Bookmark) {
  const all = loadBookmarks()
  const filtered = all.filter(x => x.question !== b.question)
  filtered.unshift(b)
  localStorage.setItem(BOOKMARKS_KEY, JSON.stringify(filtered.slice(0, 50)))
}
function removeBookmark(question: string) {
  const all = loadBookmarks().filter(x => x.question !== question)
  localStorage.setItem(BOOKMARKS_KEY, JSON.stringify(all))
}
function isBookmarked(question: string): boolean {
  return loadBookmarks().some(x => x.question === question)
}

// ── Source badge ────────────────────────────────────────────────────
function SourceBadge({ source }: { source: AskSource }) {
  const meta = srcMeta(source.source_type)
  const t = tone(meta.tone)
  const fullText = source.content && source.content.length > source.excerpt.length
    ? source.content
    : source.excerpt
  const { open } = useSourceDrawer()
  return (
    <button
      className="ask-src"
      onClick={() => open({
        title: source.section_title || '출처',
        content: fullText,
        subtitle: source.document_id ? source.document_id : source.chunk_id,
        relevance: source.relevance_score,
        sourceType: source.source_type,
        documentId: source.document_id,
      })}
    >
      <span className="ask-src-ic" style={{ background: t.bg, color: t.fg }}>
        <Icon name={meta.icon} size={13} />
      </span>
      <span className="ask-src-text">
        <b>{source.section_title || '출처'}</b>
        <span>{source.excerpt}</span>
        {source.matched_via === 'doc2query' && source.matched_question && (
          <em style={{
            display: 'block', marginTop: 4, fontSize: 11, fontStyle: 'normal',
            color: 'var(--accent-secondary)', fontWeight: 600,
          }}>
            💡 가상질문 매칭: {source.matched_question}
          </em>
        )}
      </span>
      <span className="ask-src-rel">
        {source.matched_via === 'law_refs'
          ? '인용 조문'
          : `${Math.round((source.relevance_score || 0) * 100)}%`}
      </span>
      <Icon name="arrow-up-right" size={13} className="ask-src-go" />
    </button>
  )
}

// ── Bookmarks modal ─────────────────────────────────────────────────
function BookmarksModal({ onClose, onSelect }: { onClose: () => void; onSelect: (b: Bookmark) => void }) {
  const [bookmarks, setBookmarks] = useState(() => loadBookmarks())
  const remove = (question: string) => {
    removeBookmark(question)
    setBookmarks(loadBookmarks())
  }
  return (
    <div className="ask-modal-scrim" onClick={onClose}>
      <div className="ask-modal" onClick={(e) => e.stopPropagation()}>
        <header className="ask-modal-head">
          <div>
            <h3>북마크한 답변</h3>
            <p>{bookmarks.length}건 · 브라우저에 저장 (최대 50건)</p>
          </div>
          <button className="ask-modal-x" onClick={onClose} aria-label="닫기">
            <Icon name="x" size={18} />
          </button>
        </header>
        <div className="ask-modal-body">
          {bookmarks.length === 0 ? (
            <EmptyState
              variant="bookmark"
              title="아직 북마크한 답변이 없습니다"
              sub="Q&A 답변의 북마크 버튼을 눌러 저장하세요"
            />
          ) : bookmarks.map((b) => (
            <div key={b.ts} className="bm-row">
              <button className="bm-q" onClick={() => { onSelect(b); onClose() }}>{b.question}</button>
              <button className="bm-del" onClick={() => remove(b.question)} title="삭제">
                <Icon name="x" size={14} />
              </button>
              <p className="bm-a">{b.answer}</p>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

// ── Assistant bubble ────────────────────────────────────────────────
function AssistantBubble({
  msg, onFollowUp, onFeedback, onBookmark, bookmarked,
}: {
  msg: Message
  onFollowUp: (q: string) => void
  onFeedback: (rating: 1 | -1, comment?: string) => void
  onBookmark: () => void
  bookmarked: boolean
}) {
  const [loadStep, setLoadStep] = useState(0)
  const [showSrc, setShowSrc] = useState(false)
  const [showComment, setShowComment] = useState(false)
  const [comment, setComment] = useState('')

  useEffect(() => {
    if (!msg.loading) return
    const t = setInterval(() => setLoadStep((s) => (s + 1) % LOADING_MESSAGES.length), 1100)
    return () => clearInterval(t)
  }, [msg.loading])

  const sources = msg.sources ?? []
  const followUps = msg.followUps ?? []

  return (
    <div className="ask-row assistant">
      <div className="ask-avatar"><Icon name="sparkles" size={15} /></div>
      <div className="ask-bubble-wrap">
        {/* 2026-06-01: 답변 신뢰도 경고 — relevance 낮거나 환각 의심 조문 있을 때 */}
        {!msg.loading && msg.text && msg.question && (
          (msg.avgRelevance != null && msg.avgRelevance < 0.60) || (msg.unverifiedCitations && msg.unverifiedCitations.length > 0)
        ) && (
          <div
            style={{
              background: 'var(--warning-soft)',
              border: '1px solid rgba(150,101,11,0.35)',
              borderRadius: 10,
              padding: '8px 12px',
              marginBottom: 8,
              fontSize: 12,
              color: '#92400e',
            }}
          >
            {msg.unverifiedCitations && msg.unverifiedCitations.length > 0 && (
              <div style={{ fontWeight: 700, marginBottom: 4 }}>
                ⚠️ 본문에 인용된 조문 중 RAG 출처에서 확인되지 않은 조문이 있습니다: {' '}
                {msg.unverifiedCitations.slice(0, 3).map((c, i) => (
                  <span key={i} style={{ background: '#fff', padding: '1px 6px', borderRadius: 4, marginRight: 4, fontFamily: 'monospace' }}>{c}</span>
                ))}
                <span style={{ fontWeight: 500 }}> — 법령 조문 번호는 직접 확인 권장</span>
              </div>
            )}
            {msg.avgRelevance != null && msg.avgRelevance < 0.60 && (
              <div>
                💡 이 답변은 매칭 정확도가 낮습니다 (관련도 {Math.round(msg.avgRelevance * 100)}%) — 더 구체적인 질문으로 다시 시도하시면 정확도가 올라갑니다.
              </div>
            )}
          </div>
        )}
        <div className="ask-bubble ai">
          {msg.loading && !msg.text ? (
            <div className="ask-loading">
              <span className="ask-load-dots"><i /><i /><i /></span>
              <span className="ask-load-text" key={loadStep}>{LOADING_MESSAGES[loadStep]}</span>
            </div>
          ) : msg.question ? (
            <AnnotatedText text={msg.text} />
          ) : (
            <span style={{ whiteSpace: 'pre-wrap' }}>{msg.text}</span>
          )}
          {msg.loginRequired && (
            <button
              onClick={() => login('ask')}
              style={{ display: 'block', marginTop: 10, padding: '8px 16px', borderRadius: 8, border: '1px solid #d1d5db', background: '#fff', cursor: 'pointer', fontWeight: 700 }}
            >
              Google로 로그인
            </button>
          )}
        </div>

        {!msg.loading && msg.text && msg.question && (
          <>
            <div className="ask-actions">
              <button className="ask-act" onClick={() => navigator.clipboard?.writeText(msg.text)}>
                <Icon name="copy" size={13} /> 복사
              </button>
              <button className={`ask-act ${bookmarked ? 'on' : ''}`} onClick={onBookmark}>
                <Icon name="tag" size={13} /> {bookmarked ? '저장됨' : '북마크'}
              </button>
              {sources.length > 0 ? (
                <button className="ask-act" onClick={() => setShowSrc((v) => !v)}>
                  <Icon name="book-open" size={13} /> 출처 {sources.length}
                  <Icon name={showSrc ? 'chevron-up' : 'chevron-down'} size={12} />
                </button>
              ) : msg.sources !== undefined ? (
                <span className="ask-act muted">근거 자료 없음</span>
              ) : null}
              {msg.feedbackSent ? (
                <span className="ask-fb-done">
                  {msg.feedbackSent === 'up' ? '👍' : '👎'} 의견 감사합니다
                </span>
              ) : (
                <span className="ask-fb">
                  <button
                    className="ask-fb-btn up"
                    onClick={() => onFeedback(1)}
                    title="유용한 답변"
                  >
                    <Icon name="check" size={13} />
                  </button>
                  <button
                    className="ask-fb-btn down"
                    onClick={() => setShowComment((v) => !v)}
                    title="개선 의견"
                  >
                    <Icon name="message-circle" size={13} />
                  </button>
                </span>
              )}
            </div>

            {showComment && !msg.feedbackSent && (
              <div className="ask-comment">
                <textarea
                  value={comment}
                  onChange={(e) => setComment(e.target.value)}
                  rows={2}
                  placeholder="어느 부분이 부정확하거나 부족했나요? (선택)"
                />
                <div className="ask-comment-foot">
                  <button
                    className="ask-comment-cancel"
                    onClick={() => { setShowComment(false); setComment('') }}
                  >
                    취소
                  </button>
                  <button
                    className="ask-comment-send"
                    onClick={() => {
                      onFeedback(-1, comment.trim() || undefined)
                      setShowComment(false)
                      setComment('')
                    }}
                  >
                    의견 보내기
                  </button>
                </div>
              </div>
            )}

            {showSrc && sources.length > 0 && (
              <div className="ask-srcs">
                {sources.map((s) => <SourceBadge key={s.chunk_id} source={s} />)}
              </div>
            )}

            {followUps.length > 0 && (
              <div className="ask-follow">
                <p className="ask-follow-label">이어서 물어보기</p>
                {followUps.map((q) => (
                  <button key={q} className="ask-follow-btn" onClick={() => onFollowUp(q)}>
                    <Icon name="message-circle" size={12} /> {q}
                  </button>
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}

// ── Initial intro ───────────────────────────────────────────────────
const INITIAL_MESSAGES: Message[] = [
  {
    role: 'assistant',
    text: '안녕하세요! 공공계약 업무 AI 어시스턴트입니다.\n계약 절차·법령 해석·실무 처리 방법을 자유롭게 물어보세요.',
  },
]

// ── AskPage ─────────────────────────────────────────────────────────
export default function AskPage({ onClose }: { onClose: () => void }) {
  const [messages, setMessages] = useState<Message[]>(INITIAL_MESSAGES)
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [activeCategory, setActiveCategory] = useState(CATEGORIES[0].id)
  const [showBookmarks, setShowBookmarks] = useState(false)
  const [markedMap, setMarkedMap] = useState<Record<string, boolean>>({})
  const bottomRef = useRef<HTMLDivElement>(null)
  const messagesRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const { splitMode, setSplitMode, source } = useSourceDrawer()

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const autoGrow = (el: HTMLTextAreaElement | null) => {
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 120) + 'px'
  }

  const resetConversation = () => {
    setMessages(INITIAL_MESSAGES)
    setInput('')
    setActiveCategory(CATEGORIES[0].id)
    if (inputRef.current) inputRef.current.style.height = 'auto'
    inputRef.current?.focus()
  }

  const send = async (question: string) => {
    const q = question.trim()
    if (!q || loading) return

    setMessages((prev) => [...prev, { role: 'user', text: q }])
    setInput('')
    if (inputRef.current) inputRef.current.style.height = 'auto'
    setLoading(true)
    setMessages((prev) => [...prev, { role: 'assistant', text: '', loading: true }])
    // 전환: 법령 Q&A 질문 전송. 질문 원문은 자유입력(기관 내부 사업정보가 섞인다)이라
    // 절대 넘기지 않고, 길이 구간과 대화 차례만 남긴다.
    track('ask-submit', { len_bucket: q.length < 20 ? 'short' : q.length < 80 ? 'mid' : 'long' })

    try {
      // SSE 스트리밍 — 글자 단위 실시간 출력
      const resp = await fetch('/api/v1/ask/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Device-Id': getDeviceId(), ...authHeaders() },
        body: JSON.stringify({ question: q }),
      })
      if (resp.status === 401) {
        // 익명 무료 소진/토큰 만료 (2026-07-29) — 로그인 유도
        let msg = '계속 이용하려면 Google 로그인이 필요합니다.'
        try { msg = (await resp.json()).detail?.message || msg } catch { /* ignore */ }
        setMessages((prev) =>
          prev.map((m, i) => i === prev.length - 1
            ? { role: 'assistant', text: msg, loginRequired: true }
            : m
          )
        )
        return
      }
      if (!resp.ok || !resp.body) throw new Error('stream failed')
      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      let accumText = ''
      let accumSources: AskSource[] = []
      let accumUnverified: string[] = []
      let accumAvgRel: number | null = null

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n\n')
        buffer = lines.pop() || ''
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          try {
            const event = JSON.parse(line.slice(6))
            if (event.type === 'sources') {
              accumSources = event.sources
              setMessages((prev) =>
                prev.map((m, i) => i === prev.length - 1
                  ? { ...m, sources: accumSources, loading: true }
                  : m
                )
              )
            } else if (event.type === 'token') {
              accumText += event.text
              setMessages((prev) =>
                prev.map((m, i) => i === prev.length - 1
                  ? { ...m, text: accumText, loading: false }
                  : m
                )
              )
            } else if (event.type === 'verify') {
              accumUnverified = event.unverified_citations || []
              accumAvgRel = event.avg_relevance ?? null
            } else if (event.type === 'done') {
              setMessages((prev) =>
                prev.map((m, i) => i === prev.length - 1
                  ? {
                      role: 'assistant', text: accumText, sources: accumSources,
                      followUps: getFollowUps(q), question: q,
                      unverifiedCitations: accumUnverified,
                      avgRelevance: accumAvgRel,
                    }
                  : m
                )
              )
            }
          } catch { /* ignore parse errors */ }
        }
      }
    } catch {
      setMessages((prev) =>
        prev.map((m, i) =>
          i === prev.length - 1
            ? { role: 'assistant', text: '죄송합니다. 오류가 발생했습니다. 다시 시도해 주세요.' }
            : m
        )
      )
    } finally {
      setLoading(false)
    }
  }

  const handleFeedback = (msgIdx: number, rating: 1 | -1, comment?: string) => {
    const msg = messages[msgIdx]
    if (!msg || msg.role !== 'assistant' || !msg.question) return
    setMessages((prev) =>
      prev.map((m, i) => (i === msgIdx ? { ...m, feedbackSent: rating === 1 ? 'up' : 'down' } : m))
    )
    submitFeedback({
      session_id: SESSION_ID,
      rating,
      comment,
      feedback_type: 'qna',
      question: msg.question,
      answer: msg.text,
    }).catch(() => {})
  }

  const toggleBookmark = (msgIdx: number) => {
    const m = messages[msgIdx]
    if (!m || !m.question) return
    if (isBookmarked(m.question)) {
      removeBookmark(m.question)
      setMarkedMap((s) => ({ ...s, [m.question!]: false }))
    } else {
      saveBookmark({ ts: Date.now(), question: m.question, answer: m.text, sources: m.sources })
      setMarkedMap((s) => ({ ...s, [m.question!]: true }))
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      send(input)
    }
  }

  const activeCat = CATEGORIES.find(c => c.id === activeCategory)!
  const fresh = messages.length === 1

  return (
    <div className="ask-modal-scrim" onClick={onClose} style={{ position: 'fixed', zIndex: 50 }}>
      <div
        className="ask-modal-card"
        style={{
          background: 'var(--bg-primary)',
          borderRadius: 'var(--radius-2xl, 18px)',
          boxShadow: 'var(--shadow-2xl, 0 25px 50px -12px rgba(0,0,0,0.25))',
          width: '100%',
          maxWidth: splitMode ? '1100px' : '720px',
          height: '85dvh',
          maxHeight: splitMode ? '760px' : '720px',
          overflow: 'hidden',
          display: 'flex',
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className={`ask-shell ${splitMode ? 'split' : ''}`} style={{ width: '100%', position: 'relative' }}>
          <div className="ask-pane">
            {/* header */}
            <header className="ask-head">
              <div className="ask-head-title">
                <span className="ask-head-glyph">
                  <Icon name="message-circle" size={16} strokeWidth={2.2} />
                </span>
                <div>
                  <h2>계약 Q&amp;A</h2>
                  <p>공공계약 법령·예규·실무가이드 원문 기반 답변</p>
                </div>
              </div>
              <div className="ask-head-actions">
                <button
                  className={`ask-split-toggle hidden sm:inline-flex ${splitMode ? 'on' : ''}`}
                  onClick={() => setSplitMode(!splitMode)}
                  title="답변 ↔ 원문 분할 보기"
                >
                  <Icon name="maximize" size={14} /> <span>분할 보기</span>
                </button>
                <button
                  className="ask-head-btn"
                  onClick={() => setShowBookmarks(true)}
                  title="북마크"
                  aria-label="북마크"
                >
                  <Icon name="tag" size={15} />
                </button>
                {messages.length > 1 && (
                  <button
                    className="ask-head-btn"
                    onClick={resetConversation}
                    title="새 대화"
                    aria-label="새 대화"
                  >
                    <Icon name="repeat" size={15} />
                  </button>
                )}
                <button
                  className="ask-head-btn"
                  onClick={onClose}
                  title="닫기"
                  aria-label="닫기"
                >
                  <Icon name="x" size={16} />
                </button>
              </div>
            </header>

            {/* messages */}
            <div className="ask-messages" ref={messagesRef}>
              {messages.map((msg, i) => (
                msg.role === 'user' ? (
                  <div key={i} className="ask-row user">
                    <div className="ask-bubble user">{msg.text}</div>
                  </div>
                ) : (
                  <AssistantBubble
                    key={i}
                    msg={msg}
                    onFollowUp={send}
                    onFeedback={(r, c) => handleFeedback(i, r, c)}
                    onBookmark={() => toggleBookmark(i)}
                    bookmarked={msg.question ? (markedMap[msg.question] ?? isBookmarked(msg.question)) : false}
                  />
                )
              ))}
              <div ref={bottomRef} className="ask-msg-end" />
            </div>

            {/* category discovery (fresh only) */}
            {fresh && (
              <div className="ask-discover">
                <div className="ask-cats">
                  {CATEGORIES.map((cat) => (
                    <button
                      key={cat.id}
                      className={`ask-cat ${activeCategory === cat.id ? 'on' : ''}`}
                      onClick={() => setActiveCategory(cat.id)}
                    >
                      <span className="ask-cat-glyph">{cat.glyph}</span> {cat.label}
                    </button>
                  ))}
                </div>
                <div className="ask-suggests">
                  {activeCat.questions.map((q) => (
                    <button key={q} className="ask-suggest" onClick={() => send(q)}>
                      {q}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {/* input */}
            <div className="ask-input-area">
              <div className="ask-input-box">
                <textarea
                  ref={inputRef}
                  value={input}
                  rows={1}
                  onChange={(e) => { setInput(e.target.value); autoGrow(e.target) }}
                  onKeyDown={handleKeyDown}
                  disabled={loading}
                  placeholder="계약 관련 궁금한 점을 입력하세요…"
                />
                <button
                  className="ask-send"
                  onClick={() => send(input)}
                  disabled={!input.trim() || loading}
                  aria-label="전송"
                >
                  <Icon name="arrow-up" size={18} strokeWidth={2.6} />
                </button>
              </div>
              <p className="ask-input-hint">
                Shift+Enter 줄바꿈 · AI 답변은 참고용이며 최종 판단은 담당자가 확인하세요
              </p>
            </div>
          </div>

          {/* split: source original */}
          {splitMode && (
            <div className="ask-split-pane">
              <SourceInlinePanel source={source} />
            </div>
          )}

          {showBookmarks && (
            <BookmarksModal
              onClose={() => setShowBookmarks(false)}
              onSelect={(b) => {
                setMessages([
                  INITIAL_MESSAGES[0],
                  { role: 'user', text: b.question },
                  {
                    role: 'assistant',
                    text: b.answer,
                    sources: b.sources,
                    question: b.question,
                    followUps: getFollowUps(b.question),
                  },
                ])
              }}
            />
          )}
        </div>
      </div>
    </div>
  )
}
