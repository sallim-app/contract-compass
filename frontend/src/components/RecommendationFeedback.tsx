/**
 * 추천 결과 inline 피드백 위젯 — Step2 추천 카드 옆에 배치.
 *
 * 2026-06-02 사용자 요청: "추천 결과가 맞는지 틀린지 사용자가 바로 우측에서 즉시 피드백"
 * - 👍 정확해요 / 👎 틀려요 / 💬 의견 토글
 * - 제출 시 wizardStore 컨텍스트 자동 첨부 (page, project_name, suggested_method, rule_id 등)
 * - feedback_type='recommendation' 으로 board에서도 식별 가능
 */
import { useState } from 'react'
import { submitFeedback } from '../api/client'
import { useWizardStore } from '../store/wizardStore'
import Icon from './Icon'

export default function RecommendationFeedback({
  sessionId,
  recommendedMethod,
  ruleId,
  compact = false,
  page = 'Step2Page',
  label = '이 추천이 맞나요?',
}: {
  sessionId: string
  recommendedMethod: string
  ruleId?: string
  compact?: boolean
  page?: string
  label?: string
}) {
  const { currentStep, step1Input, step1Result, step2Result } = useWizardStore()
  const [rating, setRating] = useState<1 | -1 | 0>(0)
  const [showComment, setShowComment] = useState(false)
  const [comment, setComment] = useState('')
  const [sending, setSending] = useState(false)
  const [sent, setSent] = useState(false)

  const send = async (r: 1 | -1, cmt?: string) => {
    setSending(true)
    setRating(r)
    try {
      await submitFeedback({
        session_id: sessionId,
        rating: r,
        comment: cmt?.trim() || (r === 1 ? `추천 정확 (${page} inline)` : `추천 부정확 (${page} inline)`),
        feedback_type: 'recommendation',
        // 화면 컨텍스트 자동 첨부
        page,
        step: currentStep ? String(currentStep) : undefined,
        project_name: step1Input?.project_name || undefined,
        contract_type: step1Input?.contract_type || undefined,
        estimated_price: step1Input?.estimated_price || undefined,
        description: step1Input?.description || undefined,
        suggested_method: recommendedMethod || step1Result?.candidates?.[0]?.method || undefined,
        final_method: step2Result?.method || undefined,
        rule_id: ruleId || step1Result?.candidates?.[0]?.rule_id || undefined,
      })
      setSent(true)
    } finally {
      setSending(false)
    }
  }

  if (sent) {
    return (
      <div
        style={{
          padding: '8px 12px', borderRadius: 10,
          background: rating === 1 ? 'var(--success-soft)' : 'var(--warning-soft)',
          color: rating === 1 ? 'var(--success)' : 'var(--med-ink)',
          fontSize: 12, fontWeight: 700,
          display: 'inline-flex', alignItems: 'center', gap: 6,
        }}
      >
        <Icon name="check-circle" size={14} />
        {rating === 1 ? '👍 정확 피드백 감사합니다' : '👎 피드백 감사 — 다음 cycle에 반영합니다'}
      </div>
    )
  }

  return (
    <div
      style={{
        display: 'inline-flex', flexDirection: 'column', gap: 6,
        padding: compact ? '6px 10px' : '8px 12px',
        background: 'var(--bg-tertiary)', border: '1px solid var(--border-light)',
        borderRadius: 12,
      }}
      data-recfb
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, fontWeight: 700, color: 'var(--text-secondary)' }}>
        <Icon name="message-circle" size={12} /> {label}
      </div>
      <div style={{ display: 'flex', gap: 4 }}>
        <button
          onClick={() => send(1)}
          disabled={sending}
          title="정확합니다"
          style={{
            flex: 1, padding: '6px 10px', fontSize: 12, fontWeight: 700,
            background: 'var(--success-soft)', color: 'var(--success)',
            border: '1px solid rgba(15,107,79,0.28)', borderRadius: 8,
            cursor: sending ? 'wait' : 'pointer', fontFamily: 'inherit',
            display: 'inline-flex', alignItems: 'center', gap: 4, justifyContent: 'center',
          }}
        >
          👍 정확
        </button>
        <button
          onClick={() => { setRating(-1); setShowComment(true) }}
          disabled={sending}
          title="틀렸어요"
          style={{
            flex: 1, padding: '6px 10px', fontSize: 12, fontWeight: 700,
            background: 'var(--warning-soft)', color: 'var(--med-ink)',
            border: '1px solid rgba(150,101,11,0.30)', borderRadius: 8,
            cursor: sending ? 'wait' : 'pointer', fontFamily: 'inherit',
            display: 'inline-flex', alignItems: 'center', gap: 4, justifyContent: 'center',
          }}
        >
          👎 틀림
        </button>
      </div>

      {showComment && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          <textarea
            rows={2}
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            placeholder="어떻게 틀렸나요? (예: 47억 IT용역은 협상이 표준)"
            style={{
              fontFamily: 'inherit', fontSize: 11, padding: '6px 8px',
              border: '1px solid var(--border-light)', borderRadius: 6,
              background: 'var(--bg-primary)', color: 'var(--text-primary)', resize: 'vertical',
            }}
          />
          <div style={{ display: 'flex', gap: 4, justifyContent: 'flex-end' }}>
            <button
              onClick={() => { setShowComment(false); setRating(0) }}
              style={{
                fontSize: 11, padding: '4px 10px', background: 'transparent',
                border: '1px solid var(--border-light)', borderRadius: 6,
                color: 'var(--text-tertiary)', cursor: 'pointer', fontFamily: 'inherit',
              }}
            >취소</button>
            <button
              onClick={() => send(-1, comment)}
              disabled={sending}
              style={{
                fontSize: 11, fontWeight: 700, padding: '4px 12px',
                background: 'var(--accent-primary)', color: '#fff',
                border: 'none', borderRadius: 6,
                cursor: sending ? 'wait' : 'pointer', fontFamily: 'inherit',
              }}
            >보내기</button>
          </div>
        </div>
      )}
    </div>
  )
}
