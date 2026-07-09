import os
import json
import logging
from typing import Any
from anthropic import AsyncAnthropic
# 도희님이 정의한 models.py의 스펙을 불러옵니다.
from app.models import AnalysisResult
# 분리된 딥링크 생성 로직을 불러옵니다.
from app.deeplink import DeeplinkService

logger = logging.getLogger(__name__)

# 환경 변수에서 Claude API 키 로드
CLAUDE_API_KEY = os.getenv("ANTHROPIC_API_KEY")

if not CLAUDE_API_KEY:
    raise ValueError("환경 변수 'ANTHROPIC_API_KEY'가 설정되지 않았습니다.")

anthropic_client = AsyncAnthropic(api_key=CLAUDE_API_KEY)

class LLMAnalyzer:
    def __init__(self):
        self.client = anthropic_client

    async def analyze(self, crawl_result: Any) -> AnalysisResult:
        """
        크롤링 결과를 받아 Claude API로 분석하고, 
        팀 공용 스펙인 AnalysisResult 구조로 반환합니다.
        """
        # 본문 텍스트 추출 (기존 로직 유지)
        raw_text = getattr(crawl_result, "og_description", "")
        if not raw_text and isinstance(crawl_result, dict):
            raw_text = crawl_result.get("og_description", "")

        # Claude 프롬프트 설정 (models.py 스펙 반영)
        prompt = f"""
        아래 본문에서 '상호명', '주소', '카테고리', '한 줄 요약'을 추출해줘.
        응답은 오직 JSON 형식으로만 해.

        [응답 형식]
        {{
            "category": "맛집",
            "place_name": "상호명",
            "address": "정확한 주소",
            "summary": "한 줄 요약"
        }}

        [본문]
        {raw_text}
        """

        response = await self.client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=1000,
            temperature=0.0,
            messages=[{"role": "user", "content": prompt}]
        )

        response_text = response.content[0].text.strip()
        try:
            data = json.loads(response_text)
        except json.JSONDecodeError:
            data = {}

        # 팀 스펙 모델에 맞게 객체 생성
        analysis_result = AnalysisResult(
            category=data.get("category", "맛집"),
            place_name=data.get("place_name"),
            address=data.get("address"),
            summary=data.get("summary")
        )

        return analysis_result