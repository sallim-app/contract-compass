import { useState, useEffect } from 'react'
import { getAdminStats, getTestResults, runTests, getRules } from '../api/client'

export default function AdminPage({ onClose }: { onClose: () => void }) {
  const [token, setToken] = useState(localStorage.getItem('admin_token') || '')
  const [authed, setAuthed] = useState(false)
  const [stats, setStats] = useState<any>(null)
  const [testResult, setTestResult] = useState<any>(null)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState('')
  const [rules, setRules] = useState<any[]>([])
  const [rulesTotal, setRulesTotal] = useState(0)
  const [ruleSearch, setRuleSearch] = useState('')
  const [ruleTypeFilter, setRuleTypeFilter] = useState('')
  const [showRules, setShowRules] = useState(false)

  const login = async () => {
    try {
      const data = await getAdminStats(token)
      setStats(data)
      localStorage.setItem('admin_token', token)
      setAuthed(true)
      setError('')
    } catch {
      setError('토큰이 올바르지 않습니다.')
    }
  }

  const loadTestResult = async () => {
    const data = await getTestResults(token)
    setTestResult(data)
  }

  const handleRunTests = async () => {
    setRunning(true)
    await runTests(token)
    setTimeout(async () => {
      await loadTestResult()
      setRunning(false)
    }, 5000)
  }

  useEffect(() => {
    if (authed) {
      loadTestResult()
      getRules(token).then((d) => { setRules(d.rules); setRulesTotal(d.total) }).catch(() => {})
    }
  }, [authed])

  if (!authed) {
    return (
      <div className="fixed inset-0 z-50 bg-black/60 flex items-center justify-center p-4">
        <div className="bg-white rounded-xl shadow-2xl max-w-sm w-full p-6 space-y-4">
          <div className="flex justify-between items-center">
            <h2 className="text-lg font-bold text-gray-800">관리자 로그인</h2>
            <button onClick={onClose} className="text-gray-400 hover:text-gray-600">✕</button>
          </div>
          <input
            type="password"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && login()}
            placeholder="Admin 토큰 입력"
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-blue-500"
          />
          {error && <p className="text-sm text-red-600">{error}</p>}
          <button onClick={login} className="w-full bg-blue-600 hover:bg-blue-700 text-white font-semibold py-2.5 rounded-lg text-sm">
            확인
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="fixed inset-0 z-50 bg-black/60 flex items-start justify-center p-4 overflow-y-auto overscroll-contain">
      <div className="bg-white rounded-xl shadow-2xl max-w-2xl w-full mt-8 mb-8">
        <div className="flex justify-between items-center px-6 py-4 border-b border-gray-100">
          <h2 className="text-lg font-bold text-gray-800">관리자 대시보드</h2>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 text-xl">✕</button>
        </div>

        <div className="p-6 space-y-6">
          {/* 사용 통계 */}
          {stats && (
            <div className="space-y-3">
              <h3 className="text-sm font-semibold text-gray-700">사용 통계</h3>
              <div className="grid grid-cols-3 gap-3">
                <div className="bg-blue-50 rounded-lg p-3 text-center">
                  <p className="text-2xl font-bold text-blue-700">{stats.total}</p>
                  <p className="text-xs text-gray-500 mt-0.5">전체 분석</p>
                </div>
                <div className="bg-green-50 rounded-lg p-3 text-center">
                  <p className="text-2xl font-bold text-green-700">{stats.today}</p>
                  <p className="text-xs text-gray-500 mt-0.5">오늘</p>
                </div>
                <div className="bg-purple-50 rounded-lg p-3 text-center">
                  <p className="text-2xl font-bold text-purple-700">{stats.this_week}</p>
                  <p className="text-xs text-gray-500 mt-0.5">이번 주</p>
                </div>
              </div>

              {stats.performance && (
                <div className="bg-gray-50 rounded-lg p-3 space-y-1">
                  <p className="text-xs font-semibold text-gray-600 mb-1">응답시간</p>
                  <div className="grid grid-cols-2 gap-2 text-xs text-gray-600">
                    <span>Step1 P99: <b>{stats.performance.step1_p99_ms != null ? `${stats.performance.step1_p99_ms}ms` : '-'}</b></span>
                    <span>Step1 평균: <b>{stats.performance.step1_avg_ms != null ? `${stats.performance.step1_avg_ms}ms` : '-'}</b></span>
                    <span>Step2 P99: <b>{stats.performance.step2_p99_ms != null ? `${stats.performance.step2_p99_ms}ms` : '-'}</b></span>
                    <span>Step2 평균: <b>{stats.performance.step2_avg_ms != null ? `${stats.performance.step2_avg_ms}ms` : '-'}</b></span>
                  </div>
                  {stats.llm_failure_rate && stats.llm_failure_rate.calls > 0 && (
                    <div className={`mt-2 text-xs font-medium ${stats.llm_failure_rate.rate >= 0.5 ? 'text-red-600' : 'text-gray-500'}`}>
                      LLM 실패율: {(stats.llm_failure_rate.rate * 100).toFixed(0)}%
                      ({stats.llm_failure_rate.failures}/{stats.llm_failure_rate.calls})
                      {stats.llm_failure_rate.rate >= 0.5 && ' ⚠️ 점검 필요'}
                    </div>
                  )}
                </div>
              )}

              {Object.keys(stats.by_contract_type || {}).length > 0 && (
                <div className="bg-gray-50 rounded-lg p-3">
                  <p className="text-xs font-semibold text-gray-600 mb-2">계약유형별</p>
                  <div className="flex gap-4 flex-wrap">
                    {Object.entries(stats.by_contract_type).map(([k, v]) => (
                      <span key={k} className="text-xs text-gray-600">
                        {k === 'service' ? '용역' : k === 'product' ? '물품' : '공사'}: <b>{String(v)}</b>
                      </span>
                    ))}
                  </div>
                </div>
              )}

              {stats.feedback && stats.feedback.total > 0 && (
                <div className="bg-gray-50 rounded-lg p-3 space-y-2">
                  <p className="text-xs font-semibold text-gray-600 mb-1">사용자 피드백</p>
                  <div className="flex gap-4 text-sm">
                    <span className="text-green-600 font-bold">👍 {stats.feedback.good}</span>
                    <span className="text-red-500 font-bold">👎 {stats.feedback.bad}</span>
                    <span className="text-gray-500 text-xs mt-0.5">
                      만족률 {stats.feedback.total > 0 ? Math.round(stats.feedback.good / stats.feedback.total * 100) : 0}%
                    </span>
                  </div>
                  {stats.feedback.recent?.length > 0 && (
                    <div className="space-y-1 mt-1">
                      {stats.feedback.recent.map((r: any, i: number) => (
                        <div key={i} className="text-xs text-gray-500 flex gap-2 items-start">
                          <span>{r.rating === 1 ? '👍' : '👎'}</span>
                          <span className="flex-1">{r.comment || '(의견 없음)'}</span>
                          <span className="shrink-0 text-gray-300">{r.ts ? new Date(r.ts).toLocaleDateString('ko-KR') : ''}</span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {stats.recent?.length > 0 && (
                <div className="space-y-1">
                  <p className="text-xs font-semibold text-gray-600">최근 분석</p>
                  <div className="max-h-40 overflow-y-auto space-y-1">
                    {stats.recent.map((r: any, i: number) => (
                      <div key={i} className="text-xs text-gray-500 bg-gray-50 rounded px-3 py-1.5">
                        <span className="font-medium text-gray-700">{r.event}</span>
                        {' · '}
                        {r.contract_type || ''} {r.rule_id ? `[${r.rule_id}]` : ''}
                        {' · '}
                        {r.ts ? new Date(r.ts).toLocaleString('ko-KR') : ''}
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}

          {/* 테스트 결과 */}
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-semibold text-gray-700">자동화 테스트</h3>
              <button
                onClick={handleRunTests}
                disabled={running}
                className="text-xs bg-gray-800 hover:bg-gray-900 disabled:bg-gray-400 text-white font-semibold px-3 py-1.5 rounded-lg"
              >
                {running ? '실행 중...' : '테스트 실행'}
              </button>
            </div>
            {testResult && (
              <div className="bg-gray-50 rounded-lg p-3 space-y-2">
                {testResult.message ? (
                  <p className="text-sm text-gray-500">{testResult.message}</p>
                ) : (
                  <>
                    <div className="flex gap-4 text-sm">
                      <span className="text-green-600 font-bold">✓ {testResult.passed ?? 0} PASS</span>
                      <span className="text-red-500 font-bold">✗ {testResult.failed ?? 0} FAIL</span>
                      <span className="text-gray-500">총 {testResult.total ?? 0}건</span>
                    </div>
                    {testResult.cases && (
                      <div className="max-h-48 overflow-y-auto space-y-1 mt-2">
                        {testResult.cases.map((c: any, i: number) => (
                          <div key={i} className={`text-xs flex gap-2 px-2 py-1 rounded ${c.status === 'PASS' ? 'bg-green-50 text-green-700' : 'bg-red-50 text-red-600'}`}>
                            <span>{c.status === 'PASS' ? '✓' : '✗'}</span>
                            <span className="font-medium">{c.name}</span>
                            {c.status !== 'PASS' && c.reason && <span className="text-gray-500">{c.reason}</span>}
                          </div>
                        ))}
                      </div>
                    )}
                  </>
                )}
              </div>
            )}
          </div>

          {/* 규칙 목록 */}
          {rules.length > 0 && (
            <div className="space-y-3">
              <button
                onClick={() => setShowRules(!showRules)}
                className="w-full flex justify-between items-center text-sm font-semibold text-gray-700 hover:text-gray-900"
              >
                <span>계약방법 규칙 목록 ({rulesTotal}개)</span>
                <span className="text-gray-400 text-xs">{showRules ? '▲ 접기' : '▼ 펼치기'}</span>
              </button>
              {showRules && (
                <div className="space-y-2">
                  <div className="flex gap-2">
                    <input
                      type="text"
                      value={ruleSearch}
                      onChange={(e) => setRuleSearch(e.target.value)}
                      placeholder="규칙 ID 또는 계약방법 검색"
                      className="flex-1 border border-gray-300 rounded-lg px-3 py-1.5 text-xs focus:ring-2 focus:ring-blue-500"
                    />
                    <select
                      value={ruleTypeFilter}
                      onChange={(e) => setRuleTypeFilter(e.target.value)}
                      className="border border-gray-300 rounded-lg px-2 py-1.5 text-xs"
                    >
                      <option value="">전체</option>
                      <option value="service">용역</option>
                      <option value="product">물품</option>
                      <option value="construction">공사</option>
                    </select>
                  </div>
                  <div className="max-h-64 overflow-y-auto space-y-1">
                    {rules
                      .filter((r) =>
                        (!ruleTypeFilter || r.contract_type === ruleTypeFilter) &&
                        (!ruleSearch || r.rule_id?.toLowerCase().includes(ruleSearch.toLowerCase()) ||
                          r.method?.includes(ruleSearch))
                      )
                      .map((r, i) => (
                        <div key={i} className="bg-gray-50 border border-gray-200 rounded-lg px-3 py-2">
                          <div className="flex justify-between items-start gap-2">
                            <span className="text-xs font-mono font-semibold text-blue-700">{r.rule_id}</span>
                            <span className="text-xs text-gray-500 shrink-0">
                              {r.contract_type === 'service' ? '용역' : r.contract_type === 'product' ? '물품' : r.contract_type === 'construction' ? '공사' : r.contract_type}
                            </span>
                          </div>
                          <p className="text-xs text-gray-700 mt-0.5">{r.method}</p>
                          {r.legal_basis?.[0] && (
                            <p className="text-xs text-indigo-600 mt-0.5 truncate">{r.legal_basis[0]}</p>
                          )}
                          {r.source?.document && (
                            <p className="text-xs text-gray-400 mt-0.5">출처: {r.source.document}{r.source.page ? ` ${r.source.page}p` : ''}</p>
                          )}
                        </div>
                      ))
                    }
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
