"""PDF → raw JSON 변환. PyMuPDF 기반 섹션/단락 구조 추출 + 법령 참조 메타데이터."""
import json
import re
from pathlib import Path
import fitz  # PyMuPDF


# 파일명 → 문서 유형(메타데이터) 힌트. 컬렉션 분기가 아니라 검색 필터·표시용 태그다.
# 매칭 실패 시 "general"로 둔다 — 어떤 공개 간행물이든 파이프라인에 넣을 수 있다.
CONTRACT_TYPE_MAP = {
    "공공계약 실무가이드": "general",
    "공공계약실무가이드": "general",
    "공공SW사업": "service",
    "공공sw사업": "service",
    "건설엔지니어링": "construction",
    "엔지니어링사업발주": "construction",
}

# 법령 참조 추출: 다양한 띄어쓰기/줄바꿈 허용
LAW_REF_PATTERN = re.compile(
    r'(?:국가계약법|국가를\s*당사자로\s*하는\s*계약에\s*관한\s*법률'
    r'|동법'
    r'|중소기업[^\s,。.]*?법'
    r'|공공기관\s*운영에\s*관한\s*법률|공공기관운영법'
    r'|공기업[^\s,。.]*?규칙'
    r'|소프트웨어\s*진흥법|소프트웨어산업\s*진흥법'
    r'|건설기술\s*진흥법'
    r'|조달사업[^\s,。.]*?법)'
    r'\s*(?:시행령\s*|시행규칙\s*)?제\s*\d+\s*조(?:의\s*\d+)?',
    re.UNICODE,
)

# 단독 "시행령 제N조" (앞에 법률명 없는 경우도 캡처 – 문맥에서 추론)
SIMPLE_LAW_REF = re.compile(r'시행령\s*제\s*\d+\s*조(?:의\s*\d+)?', re.UNICODE)


def _normalize_law_ref(raw: str) -> str:
    """법령 참조를 공백 정규화 후 표준 형식으로."""
    s = re.sub(r'\s+', ' ', raw).strip()
    s = re.sub(r'(국가계약법)(시행령)', r'\1 \2', s)
    s = re.sub(r'(시행령|규정|법률|진흥법)(제)', r'\1 \2', s)
    s = re.sub(r'(제\s*)(\d+)(\s*조)', lambda m: f"제{m.group(2)}조", s)
    return s


def extract_law_refs(text: str) -> list[str]:
    """텍스트에서 법령 참조 목록 추출 (정규화된 형식)."""
    refs = set()
    for m in LAW_REF_PATTERN.finditer(text):
        refs.add(_normalize_law_ref(m.group()))
    for m in SIMPLE_LAW_REF.finditer(text):
        refs.add(_normalize_law_ref(m.group()))
    return sorted(refs)


def _infer_contract_type(stem: str) -> tuple[str, str]:
    for key, val in CONTRACT_TYPE_MAP.items():
        if key.lower() in stem.lower():
            year_match = re.search(r'(20\d\d)', stem)
            if year_match:
                return val, f"{val}_{year_match.group(1)}"
            # 연도 없는 파일: 파일명 앞 6자 슬러그로 고유 ID 생성
            slug = re.sub(r'[^가-힣a-zA-Z0-9]', '', stem)[:6]
            return val, f"{val}_{slug}" if slug else f"{val}_etc"
    return "general", re.sub(r'[^가-힣a-zA-Z0-9_]', '_', stem)[:30]


def _is_junk(text: str) -> bool:
    """페이지 번호·특수기호만으로 된 줄 걸러내기."""
    stripped = text.strip()
    if not stripped:
        return True
    if re.match(r'^-?\s*\d+\s*-?\s*$', stripped):  # - 1 -
        return True
    # 의미없는 기호 (⦊ 등 단독)
    if len(stripped) <= 2 and not stripped.isalnum():
        return True
    return False


def _heading_level(size: float, h1_threshold: float) -> int:
    """폰트 크기 → 헤딩 레벨 (0=본문)."""
    if size >= h1_threshold:
        return 1
    if size >= 15.5:
        return 2
    return 0


# 제어문자 세정 — 감사원 가이드 원본엔 BEL(0x07)이 불릿 자리에 박혀 있다(쪽당 4개 내외).
# 서빙 계층에도 세정이 있지만(rag_service._quality_gate) **색인에 들어가지 않는 것이 근본**이다.
_CTRL_RE = re.compile("[\u0000-\u0008\u000b\u000c\u000e-\u001f\u200b-\u200f\u202a-\u202e\ufeff]")


def _clean_text(t: str) -> str:
    return _CTRL_RE.sub(" ", t or "")


def _reading_order(blocks: list[dict], page_width: float) -> list[dict]:
    """블록을 **읽는 순서**로 정렬 — 2단 편집이면 왼쪽 단을 다 읽고 오른쪽 단으로.

    2026-08-14 T-2026W33-173: 종전엔 y좌표만으로 정렬해서, 2단 편집 페이지가 **행 단위로
    교차**됐다(감사원 「공공계약 실무가이드」 208쪽 전체). 실측 25쪽에서 목차·본문·다른 절의
    문단이 한 줄씩 번갈아 섞여 문장이 성립하지 않았고, 그 텍스트가 검색 근거로 나갔다.
    41쪽에서는 'Check! 다수공급자계약' 설명과 유권해석 Q&A가 교차했다.

    판별은 좌표로 한다(문서 이름·페이지 번호 같은 우연한 사실에 기대지 않는다):
      · 폭이 페이지의 62%를 넘는 블록 = 단을 가로지르는 제목·표 → 앞에 둔다
      · 나머지를 페이지 중앙으로 좌/우로 가르고, 한쪽이 3블록 미만이면 단일 단으로 본다
    같은 줄에 나란한 블록은 x 순서를 지킨다(종전 y 단독 정렬은 이것도 보장하지 않았다).
    """
    def key(b: dict) -> tuple:
        # y를 6pt 단위로 눌러 같은 줄로 취급 후 x 순 — 표 셀이 뒤섞이지 않게 한다
        return (round(b["bbox"][1] / 6), b["bbox"][0])

    mid = page_width / 2
    full = [b for b in blocks if (b["bbox"][2] - b["bbox"][0]) > page_width * 0.62]
    rest = [b for b in blocks if b not in full]
    left = [b for b in rest if b["bbox"][0] < mid]
    right = [b for b in rest if b["bbox"][0] >= mid]
    if len(left) < 3 or len(right) < 3:
        return sorted(blocks, key=key)
    return sorted(full, key=key) + sorted(left, key=key) + sorted(right, key=key)

def parse_pdf(file_path: Path) -> dict:
    """PDF 파일 → 섹션/단락 구조 dict. 법령 참조는 단락 메타로 포함."""
    doc = fitz.open(str(file_path))
    stem = file_path.stem
    contract_type, document_id = _infer_contract_type(stem)

    # 표지 제외 후 본문의 최대 폰트 크기로 h1 기준 설정
    max_body_size = 0.0
    for pg_num in range(2, min(12, len(doc))):
        page = doc[pg_num]
        for b in page.get_text("dict")["blocks"]:
            for l in b.get("lines", []):
                for s in l.get("spans", []):
                    t = s["text"].strip()
                    if len(t) > 4 and not _is_junk(t):
                        max_body_size = max(max_body_size, s["size"])
    h1_threshold = max(max_body_size * 0.88, 17.0)

    sections: list[dict] = []
    current_section: dict | None = None

    def ensure_section():
        nonlocal current_section
        if current_section is None:
            current_section = {
                "section_id": f"{document_id}_s000",
                "title": "서론",
                "level": 1,
                "paragraphs": [],
                "tables": [],
            }
            sections.append(current_section)

    for pg_num in range(1, len(doc)):   # 표지(0페이지) 건너뜀
        page = doc[pg_num]
        blocks = [b for b in page.get_text("dict")["blocks"] if b.get("type", 0) == 0]

        # 읽는 순서 정렬(2단 편집 대응) — 종전 y 단독 정렬은 단을 행 단위로 교차시켰다
        sorted_blocks = _reading_order(blocks, page.rect.width)

        for b in sorted_blocks:
            if b.get("type", 0) != 0:
                continue

            for line in b.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                line_text = _clean_text(" ".join(s["text"] for s in spans)).strip()
                if _is_junk(line_text):
                    continue

                max_size = max(s["size"] for s in spans)
                level = _heading_level(max_size, h1_threshold)

                # 헤딩 조건: 짧고 번호/특수문자로 시작하거나 명백히 큰 글씨
                is_heading = (
                    level > 0
                    and len(line_text) < 80
                    and not line_text.startswith("◦")
                    and not line_text.startswith("·")
                    and not line_text.startswith("•")
                )

                if is_heading:
                    sec_id = f"{document_id}_s{len(sections):03d}"
                    current_section = {
                        "section_id": sec_id,
                        "title": line_text,
                        "level": level,
                        "paragraphs": [],
                        "tables": [],
                    }
                    sections.append(current_section)
                else:
                    ensure_section()
                    current_section["paragraphs"].append({"text": line_text})

    doc.close()

    year_m = re.search(r'(20\d\d)', stem)
    return {
        "document_id": document_id,
        "source_file": file_path.name,
        "contract_type": contract_type,
        "version": year_m.group(1) if year_m else "",
        "total_sections": len(sections),
        "total_tables": 0,
        "sections": sections,
    }


if __name__ == "__main__":
    _root = Path(__file__).resolve().parents[2]
    src_dir = _root / "data" / "source_docs"
    out_dir = _root / "etl" / "data" / "raw"
    out_dir.mkdir(parents=True, exist_ok=True)

    if not src_dir.exists():
        raise SystemExit(f"소스 문서 디렉터리 없음: {src_dir} — 공개 간행물 PDF를 넣은 뒤 재실행")

    pdf_files = sorted(src_dir.glob("*.pdf"))
    if not pdf_files:
        print(f"PDF 없음: {src_dir}")
    for fp in pdf_files:
        print(f"파싱 중: {fp.name}")
        result = parse_pdf(fp)
        out_path = out_dir / f"raw_{result['document_id']}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"  → {out_path.name}  섹션:{result['total_sections']}")
