"""AnalysisResult로부터 각종 딥링크 생성."""
from __future__ import annotations

import logging
from urllib.parse import quote

import httpx

from app.models import AnalysisResult, DeeplinkResult
from app.naver_map import build_nmap_deeplink, build_nmap_search_deeplink

logger = logging.getLogger(__name__)


async def _build_map_deeplink(
    analysis: AnalysisResult,
    client: httpx.AsyncClient,
) -> str | None:
    """place 카테고리에 대한 지도 딥링크 생성.

    네이버 지역 검색 API(NAVER_CLIENT_ID/SECRET 키 필요)는 더 이상 필수가
    아니다.
      1) LLM이 좌표까지 준 경우 → 좌표 기반 nmap://place
      2) 좌표가 없으면 → 지번주소/상호명 중 있는 값만 조합해 nmap://search로
         바로 검색 결과가 뜨도록 한다 (키 발급 불필요).
         - 주소만 있으면 주소만
         - 상호명만 있으면 상호명만
         - 둘 다 있으면 둘을 조합해 검색 정확도를 높인다
    """
    if analysis.category != "place":
        return None

    name = analysis.place_name
    address = analysis.address

    if not name and not address:
        # 검색어로 쓸 정보가 아무것도 없음
        return None

    # 1) LLM이 이미 좌표를 준 경우 → 좌표 기반 딥링크
    if analysis.latitude is not None and analysis.longitude is not None:
        return build_nmap_deeplink(analysis.latitude, analysis.longitude, name or address)

    # 2) 좌표가 없으면 있는 정보(주소/상호명)만 조합해 검색 딥링크로 폴백
    query_parts = [p for p in (address, name) if p]
    query = " ".join(query_parts)
    return build_nmap_search_deeplink(query)


def _build_calendar_deeplink(analysis: AnalysisResult) -> str | None:
    """event 카테고리에 대한 캘린더 딥링크 (간단 버전)."""
    if analysis.category != "event" or not analysis.event_title:
        return None
    # 필요 시 확장. 지금은 제목만 담은 폴백 형태.
    title = quote(analysis.event_title)
    return f"calshow://?title={title}"


def _build_memo_deeplink(analysis: AnalysisResult) -> str | None:
    """요약을 메모 앱으로 넘기는 딥링크 (폴백)."""
    if not analysis.summary:
        return None
    return f"mobilenotes://new?text={quote(analysis.summary)}"


async def generate_deeplinks(
    analysis_result: AnalysisResult,
    client: httpx.AsyncClient | None = None,
) -> DeeplinkResult:
    """AnalysisResult → DeeplinkResult 변환 진입점."""
    # client가 주입되지 않으면 내부에서 생성
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=10.0)

    try:
        map_link = await _build_map_deeplink(analysis_result, client)
    finally:
        if own_client:
            await client.aclose()

    return DeeplinkResult(
        map_deeplink=map_link,
        calendar_deeplink=_build_calendar_deeplink(analysis_result),
        memo_deeplink=_build_memo_deeplink(analysis_result),
    )