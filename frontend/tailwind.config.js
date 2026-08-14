/** @type {import('tailwindcss').Config} */
// 디자인 토큰(src/design-tokens.css)으로 Tailwind 팔레트·서체·반경을 리맵 —
// 기존 text-gray-*/bg-blue-*/rounded-* 클래스가 한 번에 신규 정체성으로 렌더된다.
// v2(2026-08-15, T-2026W33-179): 웜 슬레이트+오렌지+둥근 모서리 → 쿨 슬레이트+감청+각진 모서리.
// 규칙: 액션·강조(blue계열)=브랜드 감청 / 회색=cool slate / 의미색=high·med·safe·teal.
const cool = { // gray/slate/zinc/neutral → cool slate
  50: '#F2F5F7', 100: '#E9EEF2', 200: '#DCE3E9', 300: '#C6CFD8',
  400: '#97A2AD', 500: '#5C6772', 600: '#38424D', 700: '#38424D',
  800: '#10161C', 900: '#10161C', 950: '#10161C',
}
const brand = { // blue/sky/indigo → 브랜드 감청(단일 액션색)
  50: '#E8EFF6', 100: '#D3E1EF', 200: '#D3E1EF', 300: '#1C5E9C',
  400: '#1C5E9C', 500: '#14497A', 600: '#14497A', 700: '#0E3557',
  800: '#0E3557', 900: '#0A2740', 950: '#0A2740',
}
// 300·400·950까지 채운다 — 빠뜨리면 Tailwind 기본색(예: purple-400 보라)이 그 자리로
// 새어 나와 정체성이 깨진다(2026-08-15 빌드 산출물에서 실측).
const safe = { 50: '#E3F1EA', 100: '#E3F1EA', 200: '#C8E3D6', 300: '#8FC4AC', 400: '#3E8F70', 500: '#0F6B4F', 600: '#0F6B4F', 700: '#0B5039', 800: '#0B5039', 900: '#0B5039', 950: '#083A29' }
const high = { 50: '#F7E7E4', 100: '#F7E7E4', 200: '#EFC9C3', 300: '#DE9A90', 400: '#C55446', 500: '#B32318', 600: '#B32318', 700: '#7F1D14', 800: '#7F1D14', 900: '#7F1D14', 950: '#5A140E' }
const med = { 50: '#F6EEDC', 100: '#F6EEDC', 200: '#E7D4A8', 300: '#D2B368', 400: '#B08A25', 500: '#96650B', 600: '#96650B', 700: '#6A4406', 800: '#6A4406', 900: '#6A4406', 950: '#4A2F04' }
// v1의 보라 자리 — 슬레이트 청록(보라는 이 제품에 없다)
const slateteal = { 50: '#E7EEF2', 100: '#E7EEF2', 200: '#CBDAE2', 300: '#A3B8C4', 400: '#6B8798', 500: '#3F5B6B', 600: '#3F5B6B', 700: '#294452', 800: '#294452', 900: '#294452', 950: '#1B2E38' }

export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        gray: cool, slate: cool, zinc: cool, neutral: cool, stone: cool,
        blue: brand, sky: brand, indigo: brand, cyan: brand,
        green: safe, emerald: safe, teal: safe, lime: safe,
        red: high, rose: high,
        amber: med, yellow: med, orange: brand,
        violet: slateteal, purple: slateteal, fuchsia: slateteal,
        brand: { DEFAULT: '#14497A', ink: '#0E3557', tint: '#E8EFF6' },
      },
      fontFamily: {
        sans: ['IBM Plex Sans KR', 'Noto Sans KR', '-apple-system', 'BlinkMacSystemFont', 'Malgun Gothic', 'sans-serif'],
        mono: ['JetBrains Mono', 'ui-monospace', 'monospace'],
      },
      // 각진 문서 모서리 — rounded-lg/xl/2xl/3xl이 v1의 12~24px로 렌더되지 않게 리맵
      borderRadius: {
        none: '0', sm: '2px', DEFAULT: '3px', md: '3px', lg: '5px',
        xl: '6px', '2xl': '8px', '3xl': '10px', full: '6px',
      },
    },
  },
  plugins: [],
}
