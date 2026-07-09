"""딥링크 생성 모듈.

분석된 AnalysisResult를 바탕으로 네이버 지도 검색 딥링크를 생성한다.
"""

from __future__ import annotations

import logging
import urllib.parse
from app.models import AnalysisResult, DeeplinkResult

logger = logging.getLogger(__name__)

async def generate_deeplinks(analysis: AnalysisResult) -> DeeplinkResult:
    """분석 결과를 바탕으로 지도 앱 딥링크를 생성.
    
    장소명과 주소를 조합하여 네이버 지도 앱의 검색 화면으로 연결한다.
    """
    logger.info("딥링크 생성 시작 (category=%s)", analysis.category)

    # 1. 검색어 구성 (지역명, 주소, 장소명을 조합하여 정확도 확보)
    # 주소나 장소명이 None일 경우를 대비해 빈 문자열 처리
    query = f"{analysis.address or ''} {analysis.place_name or ''}".strip()
    
    # 2. URL 인코딩 (한글 주소/장소명을 URL 형식으로 변환)
    encoded_query = urllib.parse.quote(query)
    
    # 3. 네이버 지도 딥링크 생성
    # appname은 팀의 앱 식별자로 변경 가능
    map_url = f"nmap://search?query={encoded_query}&appname=com.yourteam.app"

    # 4. 결과 반환 (현재는 지도 딥링크만 구현)
    return DeeplinkResult(
        map_deeplink=map_url,
        calendar_deeplink=None,
        memo_deeplink=None,
    )