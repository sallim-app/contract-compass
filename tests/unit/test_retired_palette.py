"""시각 정체성 v2(T-2026W33-179) 폐기 팔레트 잔존 가드.

v1(웜 오렌지)은 타 조직 제출물과 동일 시안이라 전면 폐기됐다 — 새 값의 존재 확인만으로는
잔존을 못 잡는다(fresh-eyes 규약). hex·rgba 십진 표기 양쪽을 훑는다(실사고: 집행기가
hex만 바꿔 rgba(232,89,12) 글로우가 살아남았다, 2026-08-15).
"""
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"
RETIRED = ["e8590c", "E8590C", "232,89,12", "232, 89, 12"]


def test_no_retired_orange_in_frontend_src():
    hits = []
    for f in SRC.rglob("*"):
        if f.suffix not in (".css", ".tsx", ".ts", ".html") or not f.is_file():
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        for tok in RETIRED:
            if tok in text:
                hits.append(f"{f.relative_to(SRC)}: {tok}")
    assert hits == [], f"폐기 팔레트 잔존: {hits}"
