import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import { getDeviceId } from './lib/deviceId'
import './index.css'
// 정체성 토큰·페이지·Drawer 스타일
import './styles/designer/ds-tokens.css'
import './styles/designer/app.css'
import './styles/designer/source-drawer.css'
import './styles/designer/flow.css'
import './styles/designer/flow-responsive.css'
import './styles/designer/states.css'
import './styles/designer/glossary.css'

// umami 방문자 identify — 쿠키리스 기본값은 (IP+UA) 일별 해시라 재방문·리텐션이 안 잡히고
// NAT 공유 IP 뒤 여러 사람이 하나로 뭉침. 기존 익명 deviceId를 연결(쿠키·PII 없음).
// 스크립트가 defer라 로드 전일 수 있어 짧게 재시도.
{
  let tries = 0
  const tick = () => {
    const umami = (window as { umami?: { identify?: (id: string) => void } }).umami
    if (umami?.identify) umami.identify(getDeviceId())
    else if (tries++ < 20) setTimeout(tick, 250)
  }
  tick()
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
)
