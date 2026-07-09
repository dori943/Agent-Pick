"""네이버 지역 검색 API 매칭 모듈."""
from __future__ import annotations

import logging
import os
import re
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

_NAVER_LOCAL_URL = "https://openapi.naver.com/v1/search/local.json"


def _naver_headers() -> dict[str, str]:
    client_id = os.getenv("NAVER_CLIENT_ID")
    client_secret = os.getenv("NAVER_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 미설정")
    return {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret,
    }


def _strip_html(text: str) -> str:
    """네이버 결과의 <b> 태그 등 제거."""
    return re.sub(r"<[^>]+>", "", text)


async def search_place(
    place_name: str,
    region: str | None,
    client: httpx.AsyncClient,
) -> dict | None:
    """상호명(+지역명)으로 네이버 지역 검색.

    반환: 매칭된 첫 결과 dict 또는 None (매칭 실패)
    """
    query = f"{region} {place_name}" if region else place_name
    params = {"query": query, "display": 5, "sort": "random"}

    resp = await client.get(_NAVER_LOCAL_URL, params=params, headers=_naver_headers())
    resp.raise_for_status()
    items = resp.json().get("items", [])

    if not items:
        logger.info("네이버 검색 결과 없음: query=%s", query)
        return None

    # ── 지역명 대조 검증 ──
    if region:
        for item in items:
            address = item.get("roadAddress", "") or item.get("address", "")
            if region in address:
                item["title"] = _strip_html(item["title"])
                return item
        logger.info("지역명 대조 실패: region=%s, query=%s", region, query)
        return None

    items[0]["title"] = _strip_html(items[0]["title"])
    return items[0]


def extract_coords(item: dict) -> tuple[float, float] | None:
    """네이버 응답의 mapx/mapy를 (위도, 경도)로 변환.

    ⚠️ 좌표 스케일 확인 구간. 아래 경고 로그로 실제 값을 먼저 확인하세요.
    실패 시 None 반환.
    """
    raw_x = item.get("mapx")
    raw_y = item.get("mapy")
    if raw_x is None or raw_y is None:
        return None

    logger.warning("좌표 원본값 확인 필요 → mapx=%s, mapy=%s", raw_x, raw_y)

    try:
        x = float(raw_x)
        y = float(raw_y)
    except (ValueError, TypeError):
        return None

    # 값이 정수 스케일(예: 1270000000)이면 /1e7, 이미 소수면 그대로
    if x > 1000:
        x /= 1e7
        y /= 1e7
    return y, x  # (lat, lng)


def build_nmap_deeplink(lat: float, lng: float, name: str) -> str:
    """좌표 + 상호명으로 네이버 지도 앱 딥링크 생성."""
    return (
        f"nmap://place?lat={lat}&lng={lng}"
        f"&name={quote(name)}&appname=agent_pick"
    )


def naver_search_web_url(place_name: str) -> str:
    """예외처리용: 네이버 지도 검색창 웹 URL."""
    return f"https://map.naver.com/v5/search/{quote(place_name)}"