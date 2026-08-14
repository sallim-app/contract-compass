# 계약나침반 MCP 서버 명세

> Korean public procurement law MCP server. All tools are **LLM-free** — the server
> returns deterministic rulings and verifiable legal source text; reasoning and answer
> composition belong to the client agent.

- 원격(Streamable HTTP): `https://contract.sallim.app/mcp` (별칭 `contract.naru.build/mcp`) · 헬스: `/mcp/health`
- 로컬(stdio): `python3 mcp/server.py`
- 무료: IP당 50콜/일(전 도구) · 유료 키(`cc_live_*`): 한도 상향 — [요금](https://contract.sallim.app/mcp/pricing)
- 인증: `Authorization: Bearer cc_live_...` 헤더 또는 `?key=` 쿼리(ChatGPT 커넥터용)

## 설계 원칙

1. **무LLM** — 서버는 어떤 도구에서도 LLM을 호출하지 않는다. 판정은 룰엔진(결정론),
   검색은 임베딩+BM25, 판례는 law.go.kr 실시간 프록시. 클라이언트(당신의 AI)가 합성한다.
2. **근거 우선** — 모든 응답은 조문 원문·별표·판례 번호로 역추적 가능해야 한다.
   도구가 못 찾은 수치는 클라이언트가 지어내지 말아야 하며, instructions에 명시돼 있다.
3. **구조화 실패** — 오류·한도 초과는 예외가 아니라 `{"error", "message", "hint"}` dict로
   반환된다. 에이전트는 hint의 행동지침을 따르면 된다.

## 도구 명세 (9종)

### decide_contract_method — 계약방법 결정론 판정
룰엔진(94룰, 국가/지방/공기업 3프로파일)이 적용 가능한 계약방법 후보를 법령 근거와 반환.
- 입력: `contract_type`("construction"|"service"|"product"), `estimated_price`(원),
  `org_type`("national"|"local"|"public_corp"), 선택: `service_type`,
  `construction_specialty`, `is_sme_competition_product`, `negotiation_reason`
- 반환: `candidates[]`(method·rule_id·summary·key_params(적격심사 통과점수·낙찰하한율)·
  legal_basis), `omitted_candidates[]`, `practice_alternatives`, `explanation`(결정론 자료 팩), `laws_applied`
- **`candidates`는 최대 3건**이다. 상한으로 잘린 후보는 `omitted_candidates`
  (`{rule_id, method, summary}`)에 실린다 — 비어 있을 때만 "이게 전부"라고 말할 수 있다.
  슬롯은 **서로 다른 계약방법에 먼저** 배분한다(중복 경쟁룰이 유일한 수의 후보를 밀어내던
  결함 수리, 2026-08-14 · 회귀 R29·R30)
- 백엔드 LLM 보조설명은 `skip_llm`으로 생략 — 판정 결과는 동일, 비용 0

### search_law — 법령 조문 검색
- 입력: `query`(예: "수의계약", "시행령 제26조", 자연어 복합 쿼리 가능), `top_k`(≤20)
- 다단어는 부분매치 순위(2토큰 이상), 0건이면 시맨틱 폴백. 0건 시 재질의 `hint` 동봉
- 반환: `{hits: [{law_name, article, content, snippet, law_ref}], count, total_found}`
- **상한 조정은 공시한다**: 요청 `top_k`가 상한을 넘으면 `top_k_applied`
  (`{requested, applied, cap}`)가 실린다. 상한에 닿아 잘린 경우 `note`는 "top_k를 올려라"
  대신 **실행 가능한 다음 행동**(질의 좁히기·get_law_article 직접 조회)을 준다
  (2026-08-14 · 회귀 R39). `search_references`·`search_cases`도 같은 규약

### get_law_article — 조문 원문 전체 (현행)
- 입력: `ref`(예: "국가계약법 시행령 제26조", "지방계약법 시행규칙 제24조")
- 긴 조문은 항 단위 자식 청크를 조립해 전문 복원. 코퍼스에 없으면 구조화 404
- `assumption`: 법령명을 생략한 참조("시행령 제26조")를 국가계약법으로 **해석했을 때만**
  실린다 — 추정 근거·같은 조문번호를 가진 다른 시행령·재호출 지침 포함. 지방계약 질문이면
  이 필드가 틀린 법을 보고 있다는 신호다(2026-08-14 · 회귀 R40·R41)
- `notes[]`: **법률 자체의 미정비 상호인용** 경고(원문은 무수정). 예 — 지방보조금법
  제21조⑤가 "제2항 각 호"를 인용하나 제2항에 각 호가 없음(2023.4.11 개정으로 항이
  밀렸는데 인용 미정비, law.go.kr 현행도 동일). 탐지는 결정론·LLM 미사용

### get_law_article_asof — 특정 시점 시행 조문 (law.go.kr 연혁 라이브)
행위시법 원칙 — 과거 계약·처분·감사는 **그 당시 시행 조문**이 기준이다. 현행 조문을
과거 사건에 적용하면 겉보기 멀쩡한 오답이 된다.
- 입력: `ref`(조문 참조), `date`("YYYY-MM-DD"|"YYYYMMDD" — 계약 체결일 등)
- 반환: `content`(그 시점 전문), `effective_date`(적용된 판의 시행일자), `revision`,
  `is_current`(이후 개정 여부), `prev/next_effective_date`(개정 경계), `total_versions`, `notes[]`
- 선택 규칙: 시행일자 ≤ date 중 **가장 늦은 판**. 시행일 당일은 그 판이 적용(경계 포함)
- 약칭 자동 해석("국가계약법"→정식명). 코퍼스 밖 법령도 정식명이면 조회 가능
- 실측 근거: 국가계약법 제27조 시행 2018-03-20판 1,810자 ↔ 2026-06-11판 2,111자

### search_references — 전 코퍼스 통합 검색
법령+계약예규+조달청·행안부 적격심사 세부기준(별표 포함)+감사원 실무가이드.
낙찰하한율·적격심사 배점·부정당 제재기준처럼 **법령 본문 밖** 질의에 사용.
- 입력: `query`, `top_k`(≤12) · 반환: `{hits: [{source, section, source_type, excerpt, relevance}]}`

### search_cases — 판례·법령해석례 검색 (law.go.kr 실시간)
- 입력: `query`(핵심 명사 위주), `top_k`(종류당 ≤10), `kind`("prec"판례|"expc"해석례|"all")
- 반환: `{hits: [{kind, case_id, source_url, title, org, case_no, date}]}` — 본문은 get_case로

### estimate_delay_penalty — 지체상금·지연배상금 산정 (이행단계)
계약 체결 **이후** 축의 첫 판정 도구. 법정 요율·기준금액·30% 한도를 결정론으로 적용한다.
설계·근거표는 `docs/DELAY-PENALTY-AXIS.md`, 수치의 진실원은 `rules/delay_penalty_rules.json`.
- 입력: `contract_kind`(construction|product_manufacture|product_repair|service|
  military_food|transport_storage), `org_type`(**필수** — 국가/지방 요율이 다르다),
  `contract_amount`(원 · 장기계속은 **연차별** 금액), `delay_days`, 선택:
  `excluded_days`·`accepted_portion_amount`·`design_build_approved`
- 반환: `term`(국가=지체상금 / 지방=지연배상금)·`rate`(근거 호까지)·`base_amount`(계약금액
  −인수분)·`counted_days`(선언 지체−선언 면책)·`amount_raw`·`cap`(30% 한도)·`amount`·
  `warnings[]`·`legal_basis[]`
- **이 도구는 지체일수를 정하지 않는다** — 준공검사 소요기간·면책 사유 해당 여부는 사실
  판단이라 사용자 선언값을 그대로 쓰고 `counted_days.disclaimer`로 밝힌다. 면책 쟁점은
  `search_references`로 예규·감사원 실무가이드를 찾아 보강하라
- `rate.inferred:true`면 법문에 명시된 값이 아니라 우리 해석이다(지방 군용 음·식료품) —
  그대로 단정하지 말고 경고를 함께 전달할 것

### report_issue — 오류·개선 제보 (유일한 쓰기 도구)
- 입력: `category`(wrong_citation|outdated_law|wrong_ruling|tool_error|feature_request|other),
  `message`(10자 이상), 선택: `related_tool`·`related_query`·`expected`
- 웹 피드백과 같은 검토 파이프라인(feedback.jsonl→관리자 보드)으로 접수.
  non-readonly 어노테이션 — 대화형 클라이언트는 사용자 승인 후 호출

### get_case — 판례/해석례 본문
- 입력: `kind`, `case_id`(search_cases 결과)
- 반환(판례): 판시사항·판결요지·참조조문 / (해석례): 질의요지·회답·이유
- `source_url`: 국가법령정보센터 원문 주소 — 인용 시 함께 제시하라(본문 미제공
  오류 응답에도 실린다). 2026-08-14 · 회귀 R42
- 판례가 인용한 참조조문은 get_law_article로 교차확인 권장

## 권장 사용 흐름

- 계약방법 질문 → `decide_contract_method` → 근거 조문 `get_law_article` 검증
- **질문에 과거 시점이 있으면**(체결일·공고일·처분일) → `get_law_article_asof`로 그
  시점 조문 확인. 현행과 다르면 `is_current:false`가 신호
- 수치·기준 질문(하한율·보증금·제재기간) → `search_references`(별표) + `search_law`
- 분쟁·처분·"~해도 되나" → `search_cases` → `get_case` → 참조조문 교차확인
- 병렬 호출은 2~3개까지, 4개 이상 동시 다발 금지(단일 워커 오리진)

## 아키텍처

```
AI 클라이언트 (Claude/ChatGPT/Cursor/Codex — 합성·판단 담당)
  → CF 엣지 (contract-edge 워커: 장애 폴백 · law API 캐시 · 판례 엣지 파싱)
    → naru nginx (/lawproxy → law.go.kr 직결 | /mcp → :8403 | 그 외 → :8402)
      → MCP 서버(:8403) → 백엔드(:8402): 룰엔진 · ChromaDB 코퍼스(법령 5,820조문 +
        예규·별표 1,100+청크 + 실무가이드) · BM25 하이브리드
  [naru 장애] → 엣지 폴백 안내 → quant 콜드 스탠바이 수동 전환(docs/RUNBOOK-failover.md)
```

## 한도·요금

| 티어 | 한도 | 비고 |
|---|---|---|
| 무료 | IP당 50콜/일 (UTC 리셋) | 전 도구 동일 기능 |
| 유료 키 | 키당 2,000콜/일 | 수동 발급, 자동결제 없음 — pricing 페이지 참조 |

한도 초과 응답은 구조화 dict로 반환되며 결제·발급 안내 URL을 포함한다.

## 출처·면책

- 법령·별표·판례·해석례: 국가법령정보센터(law.go.kr) 국가법령정보 공동활용 Open API —
  본 서비스는 출처를 표시하며, 원문의 저작권 정책은 법제처 고지를 따른다.
- 본 서비스의 모든 응답은 정보 제공 목적이며 **법적 자문·유권해석이 아니다**. 실제
  발주·입찰·소송 전 반드시 소속 기관 계약부서·법률 전문가와 현행 법령을 확인할 것.
