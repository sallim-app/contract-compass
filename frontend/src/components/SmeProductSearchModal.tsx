import { useEffect, useMemo, useState } from 'react'
import { fetchSmeProducts, SME_PRODUCTS_DOWNLOAD_XLSX_URL, type SmeProductItem } from '../api/client'
import Icon from './Icon'

/**
 * F34 (2026-06-11) — 중기간 경쟁제품 웹 검색 모달.
 * 사용자 의견: "다운로드 안받아도 내용을 웹에서 열고 검색을 할 수 있으면 좋겠음. 명칭을 모르면 검색이 어려운 문제."
 *
 * 동작: 코드·품명·특이사항 어디서나 부분 매칭. 직접구매 대상 필터. xlsx 백업 다운로드 링크.
 */
export default function SmeProductSearchModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [items, setItems] = useState<SmeProductItem[]>([])
  const [loading, setLoading] = useState(false)
  const [q, setQ] = useState('')
  const [onlyDirect, setOnlyDirect] = useState(false)

  useEffect(() => {
    if (!open || items.length) return
    setLoading(true)
    fetchSmeProducts()
      .then((d) => setItems(d.items))
      .catch(() => setItems([]))
      .finally(() => setLoading(false))
  }, [open, items.length])

  const filtered = useMemo(() => {
    // 공백 무시 매칭 — 품명 표기가 "순환상온아스팔트 콘크리트"처럼 공백이 섞여 있어
    // 사용자가 붙여 검색하면 놓치던 문제 (2026-07-16 점검)
    const norm = (s: string) => s.toLowerCase().replace(/\s+/g, '')
    const needle = norm(q)
    return items.filter((it) => {
      if (onlyDirect && !it.direct_purchase) return false
      if (!needle) return true
      return (
        norm(it.code).includes(needle) ||
        norm(it.name).includes(needle) ||
        norm(it.category || '').includes(needle) ||
        norm(it.note || '').includes(needle)
      )
    })
  }, [items, q, onlyDirect])

  if (!open) return null

  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, background: 'rgba(16,22,28,0.55)',
        display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 9998,
        padding: 16,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: 'white', borderRadius: 14, width: 'min(960px, 100%)', maxHeight: '85vh',
          display: 'flex', flexDirection: 'column', overflow: 'hidden',
          boxShadow: '0 20px 60px rgba(0,0,0,0.25)',
        }}
      >
        {/* 헤더 */}
        <div style={{
          padding: '14px 20px', borderBottom: '1px solid var(--line)',
          background: 'var(--brand)',
          color: 'white', display: 'flex', alignItems: 'center', gap: 10,
        }}>
          <Icon name="database" size={16} />
          <div style={{ flex: 1 }}>
            <div style={{ fontWeight: 800, fontSize: 15 }}>중소기업자간 경쟁제품 검색</div>
            <div style={{ fontSize: 11, opacity: 0.85 }}>코드·품명·특이사항 검색 · 발주 전 확인</div>
          </div>
          <button
            type="button" onClick={onClose}
            style={{ background: 'rgba(255,255,255,0.15)', border: 'none', color: 'white', padding: '6px 12px', borderRadius: 8, cursor: 'pointer', fontWeight: 600, fontSize: 12 }}
          >닫기</button>
        </div>

        {/* 검색 + 필터 */}
        <div style={{ padding: '12px 20px', borderBottom: '1px solid var(--line)', display: 'flex', flexDirection: 'column', gap: 8 }}>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <Icon name="search" size={14} />
            <input
              autoFocus
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="품명·코드·특이사항 검색 (예: PLC, 펌프, 8213160301)"
              style={{
                flex: 1, padding: '8px 12px', border: '1px solid var(--line-strong)',
                borderRadius: 8, fontSize: 14, outline: 'none',
              }}
            />
            <a
              href={SME_PRODUCTS_DOWNLOAD_XLSX_URL}
              style={{ fontSize: 11, color: 'var(--ink-2)', textDecoration: 'underline', whiteSpace: 'nowrap' }}
            >
              <Icon name="download" size={11} /> xlsx
            </a>
          </div>
          <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--ink-2)', cursor: 'pointer' }}>
            <input
              type="checkbox" checked={onlyDirect}
              onChange={(e) => setOnlyDirect(e.target.checked)}
            />
            🔨 공사용자재 직접구매 대상만 표시
          </label>
        </div>

        {/* 결과 */}
        <div style={{ flex: 1, overflowY: 'auto', padding: '4px 0' }}>
          {loading && <div style={{ padding: 24, textAlign: 'center', color: 'var(--ink-3)' }}>📊 목록 로딩 중…</div>}
          {!loading && filtered.length === 0 && (
            <div style={{ padding: 24, textAlign: 'center', color: 'var(--ink-3)' }}>
              일치하는 제품이 없습니다. 검색어를 바꿔보세요.
            </div>
          )}
          {!loading && filtered.length > 0 && (
            <table style={{ width: '100%', fontSize: 12.5, borderCollapse: 'collapse' }}>
              <thead style={{ position: 'sticky', top: 0, background: 'var(--surface-2)', zIndex: 1 }}>
                <tr>
                  <th style={{ textAlign: 'left', padding: '8px 14px', borderBottom: '1px solid var(--line)', width: 110, fontWeight: 700, color: 'var(--ink-2)' }}>분류번호</th>
                  <th style={{ textAlign: 'left', padding: '8px 12px', borderBottom: '1px solid var(--line)', fontWeight: 700, color: 'var(--ink-2)' }}>품명</th>
                  <th style={{ textAlign: 'left', padding: '8px 12px', borderBottom: '1px solid var(--line)', fontWeight: 700, color: 'var(--ink-2)' }}>특이사항</th>
                  <th style={{ textAlign: 'center', padding: '8px 8px', borderBottom: '1px solid var(--line)', width: 60, fontWeight: 700, color: 'var(--ink-2)' }}>직구</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((it) => (
                  <tr key={it.code} style={{ borderBottom: '1px solid var(--surface-2)' }}>
                    <td style={{ padding: '8px 14px', fontFamily: 'var(--font-mono)', color: 'var(--ink)', fontWeight: 600 }}>{it.code}</td>
                    <td style={{ padding: '8px 12px' }}>
                      <div style={{ fontWeight: 600, color: 'var(--ink)' }}>{it.name}</div>
                      {it.category && (
                        <div style={{ fontSize: 10.5, color: 'var(--ink-3)', marginTop: 2 }}>{it.category}</div>
                      )}
                    </td>
                    <td style={{ padding: '8px 12px', color: 'var(--ink-2)', fontSize: 11.5, lineHeight: 1.45 }}>{it.note || '—'}</td>
                    <td style={{ padding: '8px 8px', textAlign: 'center' }}>
                      {it.direct_purchase ? <span style={{ background: 'var(--brand-tint)', color: 'var(--ink)', fontSize: 10, fontWeight: 700, padding: '2px 6px', borderRadius: 4 }}>대상</span> : <span style={{ color: 'var(--line-strong)' }}>—</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        {/* 풋터 */}
        <div style={{ padding: '8px 20px', borderTop: '1px solid var(--line)', fontSize: 11, color: 'var(--ink-3)', background: 'var(--surface-2)', display: 'flex', justifyContent: 'space-between' }}>
          <span>전체 {items.length}건 / 표시 {filtered.length}건</span>
          <span>출처: 중소벤처기업부 고시 기준</span>
        </div>
      </div>
    </div>
  )
}
