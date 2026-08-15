"""fail-closed 가드 테스트 (2026-07-20 dataloss-audit).

핵심 불변식: LLM 빈 응답은 유효 답변으로 승격되지 않는다.
※ 웹 Q&A(/api/v1/ask)의 0-hit fail-closed·캐시 오염 방지 케이스는 엔드포인트 폐지와
   함께 삭제됐다(D-2026W33-22, T-2026W33-180). 판정 경로(filter/classify)는 RAG 생성을
   하지 않으므로 이 불변식의 적용 대상이 아니다.
"""
import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_openai_empty_response_raises():
    from backend.services.llm import openai_provider as op

    class _Msg:
        content = "   "

    class _Choice:
        message = _Msg()
        finish_reason = "stop"

    class _Resp:
        choices = [_Choice()]

    class _Completions:
        async def create(self, **kwargs):
            return _Resp()

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    prov = object.__new__(op.OpenAIProvider)
    prov._model = "test-model"
    prov._client = _Client()
    with pytest.raises(RuntimeError, match="빈 응답"):
        asyncio.run(prov.complete("system", "user"))
