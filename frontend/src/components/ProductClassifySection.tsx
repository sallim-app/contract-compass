import { useState, useEffect, useRef } from 'react'
import { classifyProduct, recordClassificationApproval, type ClassifyResponse, type ProductCandidate } from '../api/client'

// 2026-06-01 사용자 의견 반영:
// - F2-8: 규격 외 → 미대상 토글 추가 (체크 시 sme 미적용)
// - F2-9: 복수선택 — radio → checkbox (여러 제품 동시 발주 케이스, OR 의미)
// - F2-10: 휴먼 키워드 직접 입력 → 재검색 (1페이지 안 돌아가도 됨)
export default function ProductClassifySection({
  description, contractType, sessionId, onSelect,
}: {
  description: string
  contractType: string
  sessionId: string
  onSelect: (sel: {
    smeCode: string | null; smeName: string | null; isSme: boolean;
    applicableStandard: '조달청' | '중기부' | '직접발주' | null
    // F13-6 (2026-06-09): 다중 선택된 모든 코드·이름
    smeCodes?: string[]; smeNames?: string[]
    // 6/20 의견: 복수 품목 결합 조건 (2개 이상 선택 시)
    smeCombineMode?: 'or' | 'and'
  }) => void
}) {
  const [result, setResult] = useState<ClassifyResponse | null>(null)
  const [loading, setLoading] = useState(false)
  // F2-9: 다중 선택 (단일 string → string[])
  const [selectedCodes, setSelectedCodes] = useState<string[]>([])
  // 6/20 의견: 복수 품목 결합 조건 (기본 OR — 하나라도 중기간이면 적용)
  const [combineMode, setCombineMode] = useState<'or' | 'and'>('or')
  const [approved, setApproved] = useState(false)
  // F2-8: 규격 외 → 미대상 토글
  const [outOfSpec, setOutOfSpec] = useState(false)
  // F2-10: 키워드 직접 입력 fallback
  const [manualQuery, setManualQuery] = useState('')
  const lastRef = useRef<string>('')

  const runClassify = (query: string) => {
    if (query.trim().length < 5) return
    setLoading(true)
    setApproved(false)
    setOutOfSpec(false)
    classifyProduct({ session_id: sessionId, description: query.trim(), contract_type: contractType })
      .then((r) => {
        setResult(r)
        // 최상위 후보 자동 선택, 사용자가 복수 선택 가능
        setSelectedCodes(r.is_sme_competition && r.sme_candidates[0] ? [r.sme_candidates[0].code] : [])
      })
      .catch(() => setResult(null))
      .finally(() => setLoading(false))
  }

  // 사업개요 변경 시 자동 분류
  useEffect(() => {
    const key = `${contractType}|${description.trim()}`
    if (description.trim().length < 5 || key === lastRef.current) return
    lastRef.current = key
    runClassify(description)
  }, [description, contractType, sessionId])

  const toggleCode = (code: string) => {
    setSelectedCodes((p) => p.includes(code) ? p.filter((c) => c !== code) : [...p, code])
  }

  const confirm = async () => {
    if (!result) return
    setApproved(true)
    // F2-8: 규격 외 미대상 토글이 ON이면 sme 미적용
    const primaryCode = !outOfSpec && selectedCodes.length > 0 ? selectedCodes[0] : null
    const primary = primaryCode ? result.sme_candidates.find((c) => c.code === primaryCode) : null
    const isSme = result.is_sme_competition && !!primary && !outOfSpec
    await recordClassificationApproval({
      session_id: sessionId, g2b_code: null,
      sme_code: isSme ? primary!.code : null,
      decision: outOfSpec ? 'rejected' : 'approved',  // F2-8: 규격 외 → rejected
    }).catch(() => {})
    // F13-6 (2026-06-09): 다중 선택된 모든 코드·이름 함께 전달
    const allCandidates = result.sme_candidates || []
    const selectedDetails = selectedCodes
      .map((c) => allCandidates.find((cd) => cd.code === c))
      .filter(Boolean) as Array<{ code: string; name: string }>
    onSelect({
      smeCode: isSme ? primary!.code : null,
      smeName: isSme ? primary!.name : null,
      isSme,
      applicableStandard: null,
      smeCodes: isSme ? selectedDetails.map((d) => d.code) : [],
      smeNames: isSme ? selectedDetails.map((d) => d.name) : [],
      smeCombineMode: selectedCodes.length > 1 ? combineMode : undefined,
    })
  }

  // F13-6: 다시 검색 (입력 초기화 + 결과 reset)
  const resetSearch = () => {
    setResult(null)
    setSelectedCodes([])
    setCombineMode('or')
    setApproved(false)
    setOutOfSpec(false)
    setManualQuery('')
    lastRef.current = ''
  }

  if (description.trim().length < 5) return null

  return (
    <div
      style={{
        border: '1px solid var(--border-light)',
        borderRadius: 12,
        padding: 16,
        background: 'var(--bg-secondary)',
      }}
    >
      <div className="flex items-center gap-2 mb-3">
        <h3 className="text-sm font-bold" style={{ color: 'var(--text-primary)' }}>🤖 물품분류 · 중기간 경쟁제품 판정</h3>
        {loading && <span className="text-xs" style={{ color: 'var(--text-tertiary)' }}>분석 중...</span>}
      </div>

      {/* F2-10: 키워드 직접 입력 — 1페이지 돌아가지 않고 재검색 */}
      <div style={{ marginBottom: 10, display: 'flex', gap: 6 }}>
        <input
          type="text"
          value={manualQuery}
          onChange={(e) => setManualQuery(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && manualQuery.trim().length >= 5) runClassify(manualQuery) }}
          placeholder="검색이 안 되면 제품명을 직접 입력 (예: 수질계측기, PLC 판넬)"
          style={{
            flex: 1, padding: '7px 10px', fontSize: 12,
            border: '1px solid var(--border-light)', borderRadius: 8,
            fontFamily: 'inherit', background: 'var(--bg-primary)', color: 'var(--text-primary)',
          }}
        />
        <button
          onClick={() => manualQuery.trim().length >= 5 && runClassify(manualQuery)}
          disabled={loading || manualQuery.trim().length < 5}
          style={{
            padding: '7px 14px', fontSize: 12, fontWeight: 700,
            border: 'none', borderRadius: 8, background: 'var(--accent-primary)', color: '#fff',
            cursor: manualQuery.trim().length >= 5 ? 'pointer' : 'not-allowed',
            opacity: manualQuery.trim().length >= 5 ? 1 : 0.5,
            fontFamily: 'inherit',
          }}
        >
          재검색
        </button>
      </div>

      {result && !loading && (
        <>
          {result.is_sme_competition ? (
            <div
              style={{
                background: outOfSpec ? 'var(--bg-tertiary)' : 'var(--success-soft)',
                border: outOfSpec ? '1px solid var(--border-light)' : '1px solid rgba(15,107,79,0.28)',
                borderRadius: 10,
                padding: 12,
                marginBottom: 10,
                opacity: outOfSpec ? 0.6 : 1,
              }}
            >
              <p className="text-xs font-semibold mb-2" style={{ color: outOfSpec ? 'var(--text-tertiary)' : 'var(--success)' }}>
                {outOfSpec
                  ? '⊘ 규격 외 — 미대상으로 표시됨 (체크박스 다시 클릭 시 복원)'
                  : '✓ 중소기업자간 경쟁제품에 해당할 수 있습니다 — 복수 선택 가능'}
              </p>
              <div className="space-y-1.5">
                {result.sme_candidates.map((c: ProductCandidate) => {
                  const sel = selectedCodes.includes(c.code)
                  return (
                    <label key={c.code}
                      className="flex flex-col gap-1 text-xs rounded cursor-pointer"
                      style={{
                        padding: '8px 10px',
                        background: sel ? 'var(--bg-secondary)' : 'transparent',
                        border: sel ? '1px solid var(--success)' : '1px solid transparent',
                        transition: 'background 0.15s ease',
                        pointerEvents: outOfSpec ? 'none' : 'auto',
                      }}
                    >
                      <div className="flex items-center gap-2">
                        <input
                          type="checkbox"
                          checked={sel}
                          onChange={() => toggleCode(c.code)}
                          className="w-3.5 h-3.5"
                          style={{ accentColor: 'var(--success)' }}
                        />
                        <span className="font-mono" style={{ color: 'var(--text-tertiary)' }}>{c.code}</span>
                        <span className="font-medium" style={{ color: 'var(--text-primary)' }}>{c.name}</span>
                        {c.direct_purchase && (
                          <span className="text-[10px] font-semibold"
                            style={{ padding: '2px 6px', borderRadius: 999, background: 'var(--warning-soft)', color: 'var(--med-ink)' }}
                            title="관급자재 — 발주처가 직접 구매 후 공사업체에 지급 (사급자재는 공사업체 자체 구매)">
                            🏗️ 관급자재 (공사용자재 직접구매)
                          </span>
                        )}
                        <span className="ml-auto" style={{ color: 'var(--text-quaternary)' }}>{Math.round(c.confidence * 100)}%</span>
                      </div>
                      {c.note && (
                        <p className="text-[11px] leading-snug" style={{ color: 'var(--med-ink)', paddingLeft: 22 }}>⚠️ 특이사항: {c.note}</p>
                      )}
                    </label>
                  )
                })}
              </div>

              {/* 6/20 의견: 복수 품목 결합 조건 (2개 이상 선택 시) */}
              {selectedCodes.length > 1 && !outOfSpec && (
                <div className="mt-2" style={{ padding: '8px 10px', borderRadius: 8, background: 'rgba(0,0,0,0.03)' }}>
                  <p className="text-[11px] font-semibold mb-1.5" style={{ color: 'var(--text-secondary)' }}>
                    복수 품목({selectedCodes.length}개) 결합 조건
                  </p>
                  <div style={{ display: 'flex', gap: 6 }}>
                    {([
                      { v: 'or' as const, label: 'OR — 하나라도 중기간이면 적용' },
                      { v: 'and' as const, label: 'AND — 모두 중기간일 때만 적용' },
                    ]).map((m) => {
                      const on = combineMode === m.v
                      return (
                        <button key={m.v} type="button" onClick={() => setCombineMode(m.v)}
                          className="text-[11px] font-semibold rounded"
                          style={{
                            flex: 1, padding: '6px 8px', cursor: 'pointer', fontFamily: 'inherit',
                            border: on ? '1px solid var(--success)' : '1px solid var(--border-light)',
                            background: on ? 'var(--success-soft)' : 'var(--bg-primary)',
                            color: on ? 'var(--success)' : 'var(--text-secondary)',
                          }}>
                          {m.label}{on && ' ✓'}
                        </button>
                      )
                    })}
                  </div>
                  {combineMode === 'and' && (
                    <p className="text-[10px] mt-1.5" style={{ color: 'var(--med-ink)' }}>
                      ⚠️ 묶음에 중기간 경쟁제품이 아닌 품목이 있으면 '규격 외 → 미대상'으로 표시하세요 (일반경쟁 분기).
                    </p>
                  )}
                </div>
              )}

              {/* F2-8: 규격 외 미대상 토글 */}
              <label
                className="flex items-center gap-2 text-xs mt-2 cursor-pointer"
                style={{ padding: '6px 8px', borderRadius: 8, background: 'rgba(0,0,0,0.03)' }}
              >
                <input
                  type="checkbox"
                  checked={outOfSpec}
                  onChange={(e) => setOutOfSpec(e.target.checked)}
                  className="w-3.5 h-3.5"
                />
                <span style={{ color: 'var(--text-secondary)', fontWeight: 600 }}>
                  ⊘ 규격 외 — 제품명은 일치하지만 실제 규격이 달라 중기간 미대상
                </span>
              </label>
            </div>
          ) : (
            <div style={{ background: 'var(--bg-tertiary)', borderRadius: 10, padding: 12, display: 'flex', flexDirection: 'column', gap: 8 }}>
              <p className="text-xs" style={{ color: 'var(--text-tertiary)', margin: 0 }}>
                중소기업자간 경쟁제품 목록과 일치하는 품목이 없습니다 — 일반경쟁입찰 가능
              </p>
              {/* 2026-06-02 F3-3: 명시적 "해당 없음" confirm 버튼 */}
              {!approved && (
                <button
                  onClick={confirm}
                  className="w-full text-sm font-semibold py-2 rounded-lg"
                  style={{ background: 'var(--success)', color: '#fff', border: 'none', cursor: 'pointer', fontFamily: 'inherit' }}
                >
                  ✓ 해당 없음 — 일반경쟁으로 진행
                </button>
              )}
            </div>
          )}
          {result.reasoning && (
            <p className="text-[11px] mt-1" style={{ color: 'var(--text-quaternary)' }}>{result.reasoning}</p>
          )}

          {/* F3-3: is_sme_competition=true일 때만 기존 confirm 버튼 (false는 위에 별도 버튼) */}
          {result.is_sme_competition && !approved && (
            <button onClick={confirm}
              className="w-full text-sm font-semibold py-2 rounded-lg mt-2"
              style={{ background: 'var(--accent-primary)', color: '#fff' }}>
              이 분류로 확정
              {selectedCodes.length > 1 && !outOfSpec && ` (${selectedCodes.length}개 선택)`}
              {outOfSpec && ' — 미대상으로 확정'}
            </button>
          )}
          {approved && (
            <p className="text-center text-xs font-semibold mt-2" style={{ color: 'var(--success)' }}>
              ✓ 분류 확정됨 {outOfSpec ? '(규격 외 → 미대상)' : !result.is_sme_competition ? '(해당 없음 — 일반경쟁)' : selectedCodes.length > 1 ? `(${selectedCodes.length}개)` : ''}
            </p>
          )}

          {/* F13-6 (2026-06-09): 다시 검색 — 확정 후에도 가능 */}
          <button
            onClick={resetSearch}
            className="w-full text-xs font-semibold py-1.5 rounded-lg mt-2"
            style={{
              background: 'transparent', color: 'var(--text-secondary)',
              border: '1px dashed var(--border-light)', cursor: 'pointer', fontFamily: 'inherit',
            }}
          >
            🔄 다시 검색 (입력 초기화)
          </button>
        </>
      )}
    </div>
  )
}
