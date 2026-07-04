"""딥링크 생성 모듈 스텁.

타 담당자가 구현 예정.
파이프라인 연동을 위해 인터페이스와 더미 응답만 정의한다.
"""

from __future__ import annotations

import logging

from app.models import AnalysisResult, DeeplinkResult

logger = logging.getLogger(__name__)


async def generate_deeplinks(analysis: AnalysisResult) -> DeeplinkResult:
    """분석 결과를 바탕으로 지도/캘린더/메모 딥링크를 생성.

    현재는 스텁 — 실제 구현 시 이 함수 내부만 교체하면 된다.
    """
    logger.info("[STUB] 딥링크 생성 (category=%s)", analysis.category)

    return DeeplinkResult(
        map_deeplink=None,
        calendar_deeplink=None,
        memo_deeplink=None,
    )