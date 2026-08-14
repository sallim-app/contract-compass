# CLAUDE.md — contract-compass (계약나침반)

> 한국 공공계약 법령(국가·지방·공기업)을 **결정론 룰엔진 + 검증 가능한 조문 원문**으로
> 노출하는 웹서비스 + MCP 서버. 공개 저장소이므로 **특정 기관·업무 고유 정보는 넣지 않는다**
> (커밋 훅 `scripts/scan_confidential.sh`가 실제로 거부한다 — 우회하지 말고 표현을 고쳐라).

## 무엇이 살아있나

- **서비스 2개**: `contract-compass.service`(:8402 — FastAPI 백엔드 + 프런트 dist 서빙) ·
  `contract-mcp.service`(:8403 — MCP Streamable HTTP). 배포 = `sudo systemctl restart contract-compass|contract-mcp`
- **공개 주소**: `https://contract.sallim.app` · MCP `…/mcp` · 헬스 `…/mcp/health` · 가격 `…/mcp/pricing`
  (별칭 `contract.naru.build` 유지). 원격 git = `github.com/kwenhwang/contract-compass`(공개)
- **프런트는 커밋만으로 안 바뀐다** — `frontend/dist/`가 .gitignore인데 백엔드가 그걸 서빙한다.
  배포는 반드시 `scripts/deploy.sh`(생성→빌드→교체→판정). 백엔드 재시작 불필요
- **티어 게이트**: 무료 IP당 50콜/일, 유료 키 `cc_live_*`. 루프백·stdio는 무제한(야간 QA·
  회귀 하네스가 자기 쿼터에 막히는 사고 방지). 구현 = `mcp/auth.py`·`keystore.py` + `QuotaGate` 미들웨어
- **판정의 진실원은 `rules/contract_rules.json`(94룰)** — 코드에 금액·수치를 박지 마라.
  법령 원문 코퍼스는 chroma_db(법령 38건 + 예규·별표·실무가이드), 판례는 law.go.kr 실시간 프록시

## 이 저장소의 중심 규칙

1. **모든 MCP 도구는 무LLM** — 클라이언트가 이미 LLM이다. 서버는 결정론 판정과 원문만 준다.
   백엔드 LLM(웹 /ask 전용)을 MCP 경로로 되살리지 마라(일일 캡·비용 축이 갈린다).
2. **폴백·절단 은폐 금지.** 요청과 다른 것을 줬으면 응답에 적는다 — 상한으로 잘랐으면 무엇이
   잘렸는지(`omitted_candidates`), 요청 건수를 못 지켰으면 그 사실을, 코퍼스 밖 법령이면
   "없다"가 아니라 "우리가 못 본다"(`law_in_corpus:false`)를. 이 프로젝트에서 반복 재발한
   결함 계열이 정확히 이것이다(R7·R9·R21·R22·R27·R28·R29·R30·R31).
3. **후보를 상한으로 자를 때 방법 다양성이 우선한다** — 같은 계약방법 중복이 슬롯을 채우고
   유일한 다른 방법(예: 소액수의)이 잘리면 적법한 선택지가 통째로 사라진다(T-2026W33-158).
4. **코퍼스를 바꿨으면 재색인 → BM25 재구축 → 백엔드 재시작 → 회귀 확인** 순서를 지킨다.
   실행 중인 uvicorn이 옛 BM25 pickle·chroma 핸들을 물고 있어 삭제된 청크를 참조하면
   검색 경로가 **500을 낸다**(2026-08-14 실측 — 회귀가 backend_error로 잡았다).
5. **수리한 결함은 `tools/mcp_regression.py`에 `R*` 회귀로 박는다.** 기대값은 반드시 실측으로
   확정(지어낸 문항은 회귀를 헛돌게 한다). 사람이 읽는 문항지는 루트 `evaluation.xml`.
6. **도구·응답 필드가 바뀌면 `SERVER_VERSION` 마이너 범프 + `server.json` 동기.** 클라이언트는
   옛 도구 목록·응답 계약을 캐시하므로 `/mcp/health`의 version이 스테일 판별의 유일한 단서다.
7. **이 파일은 얇게 유지한다** — 상세는 `docs/`. 새 함정은 여기 쌓지 말고 해당 문서에 넣어라.

## 상세는 옆 문서에 (한 사실 한 곳)

- **`docs/MCP.md`** — 도구 8종 명세·입출력·인증·설계 원칙. 도구를 고치기 전에 읽는다.
- **`evaluation.xml`** — 평가셋(사람이 읽는 문항지). 전 답이 실측이고 알려진 미수리 결함을
  정직 공시한다. 외부가 우리 판정을 되짚는 경로 = `tools/mcp_regression.py --endpoint <공개주소>`.
- **`tests/question_bank.json`** — 공공계약 빈출 질문은행(배터리·회귀·콘텐츠 공용). 새 결함은
  여기에 질문 추가 → 수리 → `R*` 회귀 순서로 고정한다. 웹 /ask 회귀는 `tests/qa_bank.json`.
- **`docs/DELAY-PENALTY-AXIS.md`** — 이행단계 축(지체상금·지연배상금) 설계. 국가/지방 요율이
  다르다는 실측표와 인식 경계 계약(지체일수는 우리가 확정하지 않는다)이 여기 있다.
- **`docs/RUNBOOK-failover.md`**(장애 이관) · **`docs/POSITIONING.md`**(제품 위치) ·
  **`docs/PROGRAMMATIC_SEO.md`**(생성 페이지) · **`README.md`**(사용자용 도구 표).

## 품질 루프 — 결함은 이 3개 경로로만 들어온다

| 경로 | 무엇 | 주기 |
|---|---|---|
| 결정론 회귀 `tools/mcp_regression.py` | 수리한 결함의 재발(LLM 무소모) | 매일 04:30 `/data/ops/mcp-qa-nightly.sh` |
| 웹 회귀 `tools/qa_regression.py` | /ask 답변 품질(질문은행) | 같은 슬롯 |
| 외부 탐침 `mcp-claude-probe.sh` / `mcp-codex-probe.sh` | 고객 클라이언트 시선의 신규 결함 | 목(codex)·금(claude) |

탐침 발견은 `logs/claude_probe_last.txt`·`logs/codex_probe_last.txt`에 남고 ops 인박스로
보고된다. **발견을 읽고 수리→회귀까지 가야 끝난다** — 로그에 고이면 다음 회차가 같은 것을
다시 발견하고, 그 사이 사용자는 계속 틀린 답을 받는다.

## 검증

```bash
python3 tools/mcp_regression.py            # 전 케이스 PASS여야 한다 (rc=0)
python3 -m pytest tests -q                 # 유닛
curl -s https://contract.sallim.app/mcp/health
scripts/deploy.sh --check                  # 프런트 배포 상태만 판정(아무것도 안 씀)
```
