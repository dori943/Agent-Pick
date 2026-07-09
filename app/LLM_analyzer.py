import os
import json
import urllib.parse
from typing import Any, Optional
from pydantic import BaseModel
from anthropic import AsyncAnthropic

# 환경 변수에서 Claude API 키 로드
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")

if not CLAUDE_API_KEY:
    raise ValueError("환경 변수 'CLAUDE_API_KEY'가 설정되지 않았습니다. .env 파일을 확인해주세요.")

# 깃허브 스펙(스크린샷 2번)에 맞춰 비정기 Claude 클라이언트 초기화
anthropic_client = AsyncAnthropic(api_key=CLAUDE_API_KEY)


# 팀원들이 스크린샷 2번에서 정의한 출력 데이터 스펙 (LLMAnalysisResult)
class LLMAnalysisResult(BaseModel):
    category: str = "맛집"
    place_name: str
    address: str
    naver_deeplink: Optional[str] = None  # [기능 6, 7] 단축어 및 네이버 지도 딥링크 연동을 위해 추가


# [팀 공용 인터페이스] 크롤링된 결과를 받아 Claude API로 파싱하는 핵심 함수
async def analyze(crawl_result: Any) -> LLMAnalysisResult:
    """
    Claude API를 호출하여 크롤링된 데이터(og_description)에서 
    상호명, 주소, 카테고리를 정형 JSON으로 파싱하고 네이버 지도 딥링크를 생성합니다.
    """
    # 크롤러가 넘겨준 본문 텍스트 추출 (스크린샷 2번 로직 반영)
    raw_text = getattr(crawl_result, "og_description", "")
    if not raw_text and isinstance(crawl_result, dict):
        raw_text = crawl_result.get("og_description", "")

    # Claude에게 줄 프롬프트 설정 (맛집명, 주소, 카테고리 추출 요청)
    prompt = f"""
    아래 인스타그램 피드 본문에서 '상호명(맛집 이름)'과 '실제 도로명 또는 지번 주소'를 찾아내어 지정된 JSON 형식으로만 응답해줘.
    응답에는 다른 설명이나 마크다운 가이드(```json) 없이 오직 순수한 JSON 데이터만 반환해야 돼.

    [응답 형식]
    {{
        "category": "맛집",
        "place_name": "상호명",
        "address": "정확한 주소"
    }}

    [피드 본문]
    {raw_text}
    """

    # 스크린샷 3번에 정의된 비동기 Claude API 호출 방식 그대로 적용
    response = await anthropic_client.messages.create(
        model="claude-3-5-sonnet-20241022",  # 최신 Sonnet 지정
        max_tokens=1000,
        temperature=0.0,
        messages=[
            {"role": "user", "content": prompt}
        ]
    )

    # Claude 응답을 딕셔너리로 변환
    response_text = response.content[0].text.strip()
    try:
        data = json.loads(response_text)
    except json.JSONDecodeError:
        # 예외 발생 시 기본값 방어 코드
        data = {}

    # 데이터 추출 (팀 스펙 구조 가동)
    category = data.get("category", "맛집")
    place_name = data.get("place_name", "알 수 없음")
    address = data.get("address", "주소 없음")

    # [기능 6] 실행형 지도 앱 딥링크 생성 (nmap://)
    # 파싱된 장소 이름과 주소를 조합하여 네이버 지도 앱에서 바로 검색/길찾기가 되도록 URL 인코딩
    naver_map_deeplink = None
    if place_name != "알 수 없음":
        search_query = f"{address} {place_name}" if address != "주소 없음" else place_name
        encoded_query = urllib.parse.quote(search_query)
        naver_map_deeplink = f"nmap://search?query={encoded_query}&appname=com.yourteam.app"

    # 최종 결과를 팀 공용 Pydantic 모델에 담아서 리턴
    return LLMAnalysisResult(
        category=category,
        place_name=place_name,
        address=address,
        naver_deeplink=naver_map_deeplink  # 이 값이 최종적으로 단축어까지 전달되어 자동 실행([기능 7])됩니다.
    )