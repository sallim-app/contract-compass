import axios from 'axios'
import type { Step1Input, Step1Response, Step2Response } from '../types'
import { getDeviceId } from '../lib/deviceId'

const api = axios.create({ baseURL: '/api/v1', timeout: 60000 })
api.interceptors.request.use((config) => {
  config.headers = config.headers ?? {}
  config.headers['X-Device-Id'] = getDeviceId()
  return config
})

// #23/#24: 다운로드 URL (a href 직접 링크)
export const SME_PRODUCTS_DOWNLOAD_URL = '/api/v1/classify/sme-products/download'
export const SME_PRODUCTS_DOWNLOAD_XLSX_URL = '/api/v1/classify/sme-products/download.xlsx'
export const SW_GUIDE_DOWNLOAD_URL = '/api/v1/classify/sw-guide/download'

// F34 (2026-06-11): 중기간 경쟁제품 JSON — 웹 검색 모달용
export type SmeProductItem = {
  code: string
  name: string
  category: string
  note: string
  direct_purchase: boolean
}
export const fetchSmeProducts = () =>
  api.get<{ total: number; items: SmeProductItem[] }>('/classify/sme-products/list').then((r) => r.data)

export const filterStep1 = (data: Step1Input) =>
  api.post<Step1Response>('/filter/step1', data).then((r) => r.data)

export const filterStep2 = (session_id: string, conditions: Record<string, unknown>, selected_rule_id?: string, selected_alternative_kind?: string) =>
  api.post<Step2Response>('/filter/step2', { session_id, additional_conditions: conditions, selected_rule_id, selected_alternative_kind }).then((r) => r.data)

// F13-1 (2026-06-09): 금액별 계약방법 매트릭스
export type MatrixMethod = {
  method: string; rule_id: string; priority: number; alternatives_count: number
  kind?: 'primary' | 'alternative' | 'default'
  reason?: string
}
export type MatrixBracket = { label: string; min: number; max: number }
export type MatrixCell = { bracket: string; min: number; max: number; methods: MatrixMethod[] }
export const getRulesMatrix = () =>
  api.get<{ contract_types: string[]; brackets: MatrixBracket[]; cells: Record<string, MatrixCell[]> }>(
    '/rules-public/matrix',
  ).then((r) => r.data)
export const getPossibleMethods = (contract_type: string, estimated_price: number) =>
  api.get<{ contract_type: string; estimated_price: number; methods: MatrixMethod[] }>(
    `/rules-public/possible-methods?contract_type=${contract_type}&estimated_price=${estimated_price}`,
  ).then((r) => r.data)

// 룰 의사결정트리 — 룰엔진을 도메인 검증용 동치 트리로 자동 도출(학습 DT 아님)
export type RuleTreeNode = {
  type: 'decision' | 'leaf'
  question?: string; dim?: string
  rule_id?: string | null; name?: string; method?: string
  bidder_selection?: string | null
  pass_score?: number | null; lower_limit_rate?: number | null
  legal_basis?: string[]; alternatives?: string[]
}
export type RuleTree = {
  contract_type: string; org_type: string
  dimensions: { id: string; label: string }[]
  root: string
  nodes: Record<string, RuleTreeNode>
  edges: { from: string; to: string; label: string }[]
  mermaid: string
  coverage: { cells: number; reproduced: number }
}
export const getRuleTree = (contract_type: string, org_type = 'public_corp') =>
  api.get<RuleTree>(`/rules-public/tree?contract_type=${contract_type}&org_type=${org_type}`).then((r) => r.data)

export const searchDocs = (query: string, contract_type: string, top_k = 5) =>
  api.post('/docs/search', { query, contract_type, top_k }).then((r) => r.data)

export const submitFeedback = (data: {
  session_id: string
  rating: number
  comment?: string
  feedback_type?: 'qna' | 'general' | 'recommendation'
  question?: string
  answer?: string
  attachment?: File
  // 2026-06-02: 화면 컨텍스트 자동 첨부 (어떤 사업·금액·결과 보고 의견 줬는지 기록)
  page?: string
  step?: string
  project_name?: string
  contract_type?: string
  estimated_price?: number
  description?: string
  suggested_method?: string
  final_method?: string
  rule_id?: string
  // 2026-07-18: 원클릭 화면 캡처 (html2canvas PNG data URL) + 캡처 컨텍스트
  screenshot?: string
  url?: string
  viewport?: string
  user_agent?: string
}) => {
  const fd = new FormData()
  fd.append('session_id', data.session_id)
  fd.append('rating', String(data.rating))
  if (data.comment) fd.append('comment', data.comment)
  fd.append('feedback_type', data.feedback_type ?? 'general')
  if (data.question) fd.append('question', data.question)
  if (data.answer) fd.append('answer', data.answer)
  if (data.attachment) fd.append('attachment', data.attachment)
  // 컨텍스트 필드 — 있을 때만 추가
  if (data.page) fd.append('page', data.page)
  if (data.step) fd.append('step', data.step)
  if (data.project_name) fd.append('project_name', data.project_name)
  if (data.contract_type) fd.append('contract_type', data.contract_type)
  if (data.estimated_price != null) fd.append('estimated_price', String(data.estimated_price))
  if (data.description) fd.append('description', data.description)
  if (data.suggested_method) fd.append('suggested_method', data.suggested_method)
  if (data.final_method) fd.append('final_method', data.final_method)
  if (data.rule_id) fd.append('rule_id', data.rule_id)
  // 화면 캡처 (있을 때만) — 백엔드가 base64 PNG를 파일로 저장
  if (data.screenshot) fd.append('screenshot', data.screenshot)
  if (data.url) fd.append('url', data.url)
  if (data.viewport) fd.append('viewport', data.viewport)
  if (data.user_agent) fd.append('user_agent', data.user_agent)
  return api.post('/feedback', fd).then((r) => r.data)
}

export const getAdminStats = (token: string) =>
  api.get('/admin/stats', { headers: { 'X-Admin-Token': token } }).then((r) => r.data)

export const getTestResults = (token: string) =>
  api.get('/admin/test-results', { headers: { 'X-Admin-Token': token } }).then((r) => r.data)

export const runTests = (token: string) =>
  api.post('/admin/run-tests', {}, { headers: { 'X-Admin-Token': token } }).then((r) => r.data)

export const getRules = (token: string) =>
  api.get('/admin/rules', { headers: { 'X-Admin-Token': token } }).then((r) => r.data)

export type GlossaryTerm = { term: string; definition: string; related: string[] }

export const getGlossary = () => api.get<GlossaryTerm[]>('/glossary').then((r) => r.data)

export const searchGlossary = (q: string) =>
  api.get<GlossaryTerm[]>('/glossary/search', { params: { q } }).then((r) => r.data)

export type LawArticle = { law_name: string; article: string; content: string; law_ref: string }

export const getLawArticle = (ref: string) =>
  api.get<LawArticle>('/law/article', { params: { ref } }).then((r) => r.data)

export type LawSearchHit = {
  law_name: string
  article: string
  content: string
  snippet: string
  law_ref: string
}

export const searchLaw = (q: string) =>
  api.get<LawSearchHit[]>('/law/search', { params: { q } }).then((r) => r.data)

export type ProductCandidate = { code: string; name: string; confidence: number; note?: string; direct_purchase?: boolean }
export type ClassifyResponse = {
  g2b_candidates: ProductCandidate[]
  is_sme_competition: boolean
  sme_candidates: ProductCandidate[]
  reasoning: string
}

export const classifyProduct = (data: {
  session_id: string
  description: string
  contract_type: string
}) => api.post<ClassifyResponse>('/classify/product', data).then((r) => r.data)

// 단계 0: 계약유형 AI 추천 (#29)
export type ContractTypeCandidate = { contract_type: 'service' | 'product' | 'construction'; label: string; confidence: number }
export type ContractTypeResponse = {
  suggested: 'service' | 'product' | 'construction'
  confidence: number
  reason: string
  candidates: ContractTypeCandidate[]
  method: 'keyword' | 'llm'
}
export const suggestContractType = (description: string) =>
  api.post<ContractTypeResponse>('/classify/contract-type', { description }).then((r) => r.data)

export const recordClassificationApproval = (data: {
  session_id: string
  g2b_code: string | null
  sme_code: string | null
  decision: 'approved' | 'rejected'
  reviewer_note?: string
}) => api.post('/classify/approval', data).then((r) => r.data)

export type ClassifyByCodeResponse = {
  code: string
  name: string | null
  is_sme_competition: boolean
  applicable_standard: '조달청' | '중기부' | '직접발주' | null
  description: string
}

export const lookupByCode = (params: {
  code: string
  estimated_price?: number
  contract_type?: string
}) => {
  const { code, ...rest } = params
  return api.get<ClassifyByCodeResponse>(`/classify/by-code/${encodeURIComponent(code)}`, {
    params: rest,
  }).then((r) => r.data)
}

export type FeatureItem = { id: string; name: string; status: '운영중' | '베타' | '계획'; desc: string }
export type ChangelogItem = { date: string; items: string[] }
export type RoadmapItem = { label: string; priority: 'high' | 'medium' | 'low' }
export type TopicStat = { topic: string; count: number }
export type LedgerMetrics = {
  total: number; normalized: number; evaluated: number
  method_correct: number; method_accuracy: number
  method_alt_aware_correct?: number; method_alt_aware_accuracy?: number
  qualification_total: number; pass_score_covered: number; pass_score_coverage: number
  award_rate_evaluated: number; award_rate_ok: number; award_rate_consistency: number
  report_path?: string | null
}
export type JudgeEvalMetrics = {
  version: string
  faithfulness: number; answer_relevancy: number; context_precision: number
  evaluated_at?: string | null
  judge_variance_note?: string | null
}
export type StatusMetrics = {
  test_pass: number
  test_total: number
  test_run_at: string | null
  chunk_counts: Record<string, number>
  chunk_total: number
  glossary_count: number
  feedback_count: number
  topic_top10?: TopicStat[]
  topic_tagged_total?: number
  ledger?: LedgerMetrics | null
  judge_eval?: JudgeEvalMetrics | null
  user_feedback_e2e_pass?: number
  user_feedback_e2e_total?: number
  user_feedback_e2e_at?: string | null
  // F14 + F17-B/D (2026-06-09): 자료 정합성 + 커버리지 + 결정론
  textbook_consistency?: {
    rule_audit_pass?: number
    rule_audit_total?: number
    notice_form_pass?: number
    notice_form_total?: number
    coverage_total?: number
    coverage_pass?: number
    coverage_partial?: number
    coverage_fail?: number
    coverage_pass_pct?: number
    coverage_effective_pct?: number
    determinism_total?: number
    determinism_pass?: number
    determinism_runs_per_case?: number
    audit_at?: string | null
  }
}
export type TestCase = { case_id: string; passed: boolean; duration_ms?: number | null }
export type ArchitectureBlock = { title: string; detail: string }
export type IssueItem = { label: string; severity: 'high' | 'medium' | 'low'; detail?: string | null }

export type TrackItem = { name: string; progress: number; status: '완료' | '운영중' | '진행' | '보류' | '예정'; note?: string }
export type MilestoneItem = {
  date: string
  title: string
  metric_before?: string | null
  metric_after?: string | null
  summary: string
  impact: 'critical' | 'major' | 'milestone'
}
export type PublicStatusResponse = {
  milestones?: MilestoneItem[]
  tracks?: TrackItem[]
  features: FeatureItem[]
  changelog: ChangelogItem[]
  roadmap: RoadmapItem[]
  metrics: StatusMetrics
  test_cases: TestCase[]
  architecture: ArchitectureBlock[]
  known_issues: IssueItem[]
}

export const getProjectStatus = () =>
  api.get<PublicStatusResponse>('/status/public').then((r) => r.data)

// 사용자 의견 보드 — 작성자에게 반영 상태 투명 공개
export type FeedbackBoardItem = {
  ts_kst: string
  sid: string
  category: string
  subcategory: string
  summary: string
  full_comment: string
  status: 'reflected' | 'reviewing' | 'deferred' | 'open'
  status_detail: string
  // 2026-06-02: 작성 시점 화면 컨텍스트 (사업명·금액·계약유형·AI추천)
  context?: {
    page?: string
    step?: string
    project_name?: string
    contract_type?: string
    estimated_price?: number
    description?: string
    suggested_method?: string
    final_method?: string
    rule_id?: string
  } | null
}
export type FeedbackBoardResponse = {
  total: number
  stats: { reflected: number; reviewing: number; deferred: number; open: number }
  items: FeedbackBoardItem[]
}
// 2026-07-27: /feedback/board 는 관리자 전용(무인증 401). AdminPage 로그인 시 저장된
// admin_token 을 재사용한다 — 토큰이 없으면 서버가 401을 주고 화면은 안내 문구로 대체된다.
export const getStoredAdminToken = () => {
  try { return localStorage.getItem('admin_token') || '' } catch { return '' }
}
export const getFeedbackBoard = () =>
  api.get<FeedbackBoardResponse>('/feedback/board', {
    headers: { 'X-Admin-Token': getStoredAdminToken() },
  }).then((r) => r.data)
