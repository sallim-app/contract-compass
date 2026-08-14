"""공공SW사업 법제도 가이드(공개 배포본) 2024.12 → 2025.11 갱신 인덱싱.

ETL 경로(parse_pdf→chunk_document)로 2025.11 버전을 청킹해 public_guides 컬렉션에
적재한다. document_id는 service_sw_guide_2025로 override, 기존 2024 청크는 교체 삭제.
원본 PDF는 repo에 포함되지 않는다 — 운영자가 data/source_docs/에 넣는다.

--dry  : 파싱·청킹만 (인덱싱 안 함, 청크 수·샘플 출력)
실인덱싱: 기존 2024 청크 삭제 → 2025 upsert → BM25 재구축은 별도(tools/build_bm25_index.py)

⛔ **2026-08-14 결정: 이 문서는 코퍼스에서 영구 제외한다**(사장님 확정, T-2026W33-174).
   이유: 배포본 PDF의 폰트 cmap이 손상돼 텍스트 레이어가 **판독 불가**다(83쪽에서 한글
   12자·제어문자 351개, 본문이 "˒߳ݧࡿ" 꼴). 텍스트 재추출로는 살릴 수 없고 OCR만이
   경로인데, SW 관련 질의는 소프트웨어 진흥법 조문(법령 코퍼스, 오염 0건)으로 커버된다.
   읽을 수 없는 텍스트를 '근거'로 내놓는 것은 인용이 아니라 소음이다(기치 ②).
   그래서 이 스크립트는 **기본적으로 실행을 거부**한다 — 원본 PDF는 되돌릴 근거로 남겨둔다.
   되돌리려면(OCR로 판독 가능한 PDF를 확보한 뒤): CC_ALLOW_SW_GUIDE_INDEX=1 로 실행하고
   rag_service._EXCLUDED_SOURCES에서 service_sw_guide_2025를 지운다.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from etl.parsers.pdf_parser import parse_pdf                       # noqa: E402
from etl.chunkers.semantic_chunker import chunk_document           # noqa: E402
from etl.loaders.chroma_loader import (                            # noqa: E402
    PUBLIC_GUIDES_COLLECTION, get_client, upsert_chunks,
)

SOURCE_DOCS_DIR = ROOT / "data" / "source_docs"
PDF_GLOB = "*공공SW사업*가이드*2025*.pdf"  # 예: (배포본)_공공SW사업_법제도관리감독_및_지원_가이드(2025.11).pdf
NEW_DOC = "service_sw_guide_2025"
OLD_DOC = "service_sw_guide_2024"
CHUNKS_OUT = ROOT / "etl" / "data" / "chunks" / f"chunks_{NEW_DOC}.jsonl"


def find_pdf() -> Path | None:
    if not SOURCE_DOCS_DIR.exists():
        return None
    matches = sorted(SOURCE_DOCS_DIR.glob(PDF_GLOB))
    return matches[0] if matches else None


def build_chunks(pdf: Path) -> list[dict]:
    raw = parse_pdf(pdf)
    print(f"parse_pdf: document_id={raw['document_id']} contract_type={raw['contract_type']} "
          f"sections={raw['total_sections']}")
    raw["contract_type"] = "service"  # 메타 태그 보장 (SW사업 = 용역 계열)
    chunks = chunk_document(raw)
    for c in chunks:
        c["document_id"] = NEW_DOC
        c["contract_type"] = "service"
    return chunks


def main() -> int:
    # 영구 제외 결정(T-2026W33-174) — 조용히 색인되지 않게 여기서 막는다. 색인 경로를
    # 막지 않고 서빙 필터에만 의존하면, 필터를 지나지 않는 소비자(search_knowledge_web
    # 등)나 다음 재색인에서 판독 불가 텍스트가 되살아난다.
    import os
    if os.environ.get("CC_ALLOW_SW_GUIDE_INDEX") != "1":
        print("⛔ 이 문서는 코퍼스에서 영구 제외됐다(2026-08-14, T-2026W33-174) — "
              "배포본 PDF의 폰트 cmap 손상으로 텍스트가 판독 불가다.")
        print("   OCR로 판독 가능한 PDF를 확보해 되돌리려면 CC_ALLOW_SW_GUIDE_INDEX=1로 "
              "실행하고 rag_service._EXCLUDED_SOURCES에서도 해당 항목을 지워라.")
        return 2

    pdf = find_pdf()
    if pdf is None:
        print(f"공공SW사업 가이드 2025 PDF를 찾지 못했습니다: {SOURCE_DOCS_DIR}/{PDF_GLOB}")
        print("  공개 배포본 PDF를 내려받아 위 경로에 넣은 뒤 재실행하세요.")
        return 1

    dry = "--dry" in sys.argv
    chunks = build_chunks(pdf)
    lens = [len(c["content"]) for c in chunks]
    print(f"\n청크 {len(chunks)}개 | 길이 min={min(lens)} avg={sum(lens)//len(lens)} max={max(lens)}")
    print("--- 섹션 제목 샘플 ---")
    seen = []
    for c in chunks:
        t = c["section_title"]
        if t not in seen:
            seen.append(t)
    for t in seen[:25]:
        print(f"  · {t[:50]}")

    CHUNKS_OUT.parent.mkdir(parents=True, exist_ok=True)
    CHUNKS_OUT.write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in chunks) + "\n", encoding="utf-8"
    )
    print(f"\n청크 저장: {CHUNKS_OUT.name}")

    if dry:
        print("\n[DRY-RUN] 인덱싱 생략. 검증 후 --dry 없이 재실행.")
        return 0

    # 실인덱싱: 기존 2024 삭제 → 2025 upsert
    client = get_client()
    col = client.get_or_create_collection(PUBLIC_GUIDES_COLLECTION)
    old = col.get(where={"document_id": OLD_DOC}, include=[])
    if old["ids"]:
        col.delete(ids=old["ids"])
        print(f"기존 {OLD_DOC} {len(old['ids'])}청크 삭제")
    upsert_chunks(client, CHUNKS_OUT)
    final = col.get(where={"document_id": NEW_DOC}, include=[])
    print(f"✅ {NEW_DOC} 인덱싱: {len(final['ids'])}청크 (전체 {len(chunks)}개)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
