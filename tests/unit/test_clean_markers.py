"""조문 본문 표지 정규화 회귀 — 중복 표지는 지우되 **개정일자는 건드리지 않는다**.

T-2026W33-162(2026-08-13 codex 탐침): 숫자 표지 규칙 `(\\d{1,2}\\.)\\s*\\2`가
"2011.11.23"의 "11.11."을 중복 표지로 오인해 "2011.23"으로, "1999.9.9"를 "1999.9"로
망가뜨렸다. 원문 XML은 멀쩡했고(실측) 반환 시점에 깨진 것이라, 조문 본문은 정상인데
개정 이력만 존재할 수 없는 날짜가 되어 감사 자료에 인용할 수 없었다.
"""
import pytest

from backend.api.v1.law import _clean_markers


@pytest.mark.parametrize("text", [
    "<개정 1999.9.9, 2006.5.25, 2011.11.23, 2013.12.11, 2017.12.26>",
    "<개정 2011.10.28, 2011.12.31, 2019.9.17>",
    "<신설 2018.12.4> <개정 2020.9.29>",
])
def test_revision_dates_survive(text):
    assert _clean_markers(text) == text


@pytest.mark.parametrize("dirty,clean", [
    ("① ① 각 중앙관서의 장은", "① 각 중앙관서의 장은"),
    ("3. 3. 물품의 제조", "3. 물품의 제조"),
    ("가. 가. 종합공사", "가. 종합공사"),
    ("1. 1. 100분의 30을 초과하는", "1. 100분의 30을 초과하는"),
])
def test_duplicate_markers_still_cleaned(dirty, clean):
    assert _clean_markers(dirty) == clean


def test_article_citation_untouched():
    ref = "제26조제1항제5호 가목2)에 따라"
    assert _clean_markers(ref) == ref
