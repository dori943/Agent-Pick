"""AnalysisResult로부터 각종 딥링크 생성."""
from __future__ import annotations

import logging
from urllib.parse import quote

import httpx

from app.models import AnalysisResult, DeeplinkResult
from app.naver_map import (
    build_nmap_deeplink,
    extract_coords,
    naver_search_web_url,
    search_place,
)

logger = logging.getLogger(__name__)


async def _build_map_deeplink(
    analysis: AnalysisResult,
    client: httpx.AsyncClient,
) -> str | None:
    """place 카테고리에 대한 지도 딥링크 생성."""
    # place가 아니거나 상호명이 없으면 지도 링크 불필요
    if analysis.category != "place" or not analysis.place_name:
        return None

    name = analysis.place_name

    # 1) LLM이 이미 좌표를 준 경우 → 네이버 검색 생략
    if analysis.latitude is not None and analysis.longitude is not None:
        return build_nmap_deeplink(analysis.latitude, analysis.longitude, name)

    # 2) 좌표가 없으면 네이버 검색으로 보강
    try:
        matched = await search_place(name, analysis.region, client)
    except Exception as e:
        logger.warning("네이버 검색 예외: %s", e)
        matched = None

    if matched is None:
        # 매칭 실패 → 검색창 웹 URL로 폴백
        return naver_search_web_url(name)

    coords = extract_coords(matched)
    if coords is None:
        return naver_search_web_url(name)

    lat, lng = coords
    return build_nmap_deeplink(lat, lng, matched["title"])


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