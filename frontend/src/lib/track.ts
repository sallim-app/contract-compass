// 전환 이벤트 발화 — umami 커버리지 판정 ③단(핵심 전환 이벤트 이력)의 유일한 근거.
//
// 왜 큐가 필요한가: index.html의 script.js는 `defer`라 앱 첫 렌더 시점엔 window.umami가
// 아직 없다. 그냥 window.umami?.track()로 쏘면 홈 진입 직후 클릭(= wizard-start, 우리가
// 제일 보고 싶은 이벤트)이 조용히 사라진다. 그래서 로드 전 호출은 큐에 담고 로드 후 흘린다.
//
// 계측은 기능이 아니다 — 어떤 실패도 앱 흐름을 막지 않는다(전부 삼킨다).
// 이벤트 이름은 /data/ops/umami-coverage-check.sh 의 CORE 패턴(`wizard-|ask-`)과
// 접두사가 일치해야 커버리지 점검이 잡는다. 이름을 바꾸면 그쪽도 같은 턴에 바꿀 것.

type TrackFn = (name: string, data?: Record<string, unknown>) => void
type UmamiWindow = Window & { umami?: { track?: TrackFn } }

const MAX_QUEUE = 20
const MAX_WAIT_MS = 10_000
const RETRY_MS = 250

const pending: Array<[string, Record<string, unknown> | undefined]> = []
let waiting = false

function umamiTrack(): TrackFn | undefined {
  if (typeof window === 'undefined') return undefined
  return (window as UmamiWindow).umami?.track
}

function drain() {
  const fn = umamiTrack()
  if (!fn) return false
  while (pending.length) {
    const [name, data] = pending.shift()!
    try { fn(name, data) } catch { /* 계측 실패는 앱을 막지 않는다 */ }
  }
  return true
}

function waitAndDrain() {
  if (waiting) return
  waiting = true
  let elapsed = 0
  const tick = () => {
    if (drain()) { waiting = false; return }
    elapsed += RETRY_MS
    // 스크립트가 차단(광고차단기·오프라인)됐을 수 있다 — 무한 재시도하지 않고 포기한다.
    if (elapsed >= MAX_WAIT_MS) { waiting = false; pending.length = 0; return }
    setTimeout(tick, RETRY_MS)
  }
  setTimeout(tick, RETRY_MS)
}

/**
 * 전환 이벤트 1건 발화. 개인정보·자유입력(사업명·질문 원문)은 절대 넘기지 않는다 —
 * 넘겨도 되는 건 분류값(계약유형·기관유형)처럼 카디널리티가 낮은 라벨뿐이다.
 */
export function track(name: string, data?: Record<string, unknown>): void {
  const fn = umamiTrack()
  if (fn) {
    try { fn(name, data) } catch { /* 삼킨다 */ }
    return
  }
  if (pending.length < MAX_QUEUE) pending.push([name, data])
  waitAndDrain()
}
