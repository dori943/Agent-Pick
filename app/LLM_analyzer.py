import os
import json
from anthropic import AsyncAnthropic
from pydantic import BaseModel
from typing import Any

# .env 파일에 들어갈 ANTHROPIC_API_KEY를 읽어와 비동기 Claude 클라이언트 생성
anthropic_client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

class LLMAnalysisResult(BaseModel):
    category: str = "맛집"
    place_name: str
    address: str

async def analyze(crawl_result: Any) -> LLMAnalysisResult:
    """Claude API를 호출하여 크롤링된 데이터(og_description)에서 장소명과 주소를 정형 JSON으로 파싱합니다."""
    raw_text = crawl_result.og_description
    
    prompt = f"""
    아래 인스타그램 피드 본문에서 '상호명(맛집 이름)'과 '실제 도로명 또는 지번 주소'를 찾아내어 지정된 JSON 형식으로만 응답
    응답에는 다른 설명이나 마크다운 가이드(```json) 없이 오직 순수한 JSON 데이터만 반환

    [피드 본문]
    {raw_text}

    [응답 형식]
    {{
        "category": "맛집",
        "place_name": "상호명",
        "address": "정확한 주소"
    }}
    """

    response = await anthropic_client.messages.create(
        model="claude-3-5-sonnet-20240620",
        max_tokens=1000,
        temperature=0.0,
        messages=[
            {"role": "user", "content": prompt}
        ]
    )

    # Claude 응답을 딕셔너리로 변환
    response_text = response.content[0].text.strip()
    data = json.loads(response_text)

    return LLMAnalysisResult(
        category=data.get("category", "맛집"),
        place_name=data.get("place_name", "알 수 없음"),
        address=data.get("address", "주소 없음")
    )