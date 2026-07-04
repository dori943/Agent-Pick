"""LLM 분석 모듈 스텁.

타 담당자가 구현 예정.
파이프라인 연동을 위해 인터페이스와 더미 응답만 정의한다.
"""

from __future__ import annotations

import logging

from app.models import AnalysisResult, CrawlResult

logger = logging.getLogger(__name__)


async def analyze(crawl: CrawlResult) -> AnalysisResult:
    """크롤 결과를 LLM으로 분석하여 구조화된 결과를 반환.

    현재는 스텁 — 실제 구현 시 이 함수 내부만 교체하면 된다.
    """
    logger.info("[STUB] LLM 분석 호출 (url=%s)", crawl.url)

    return AnalysisResult(
        category="other",
        summary=crawl.og_description[:80] if crawl.og_description else "분석 대기",
        tags=[],
    )