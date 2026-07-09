import os
import json
import logging
from typing import Any

from google import genai
from google.genai import types
from app.models import AnalysisResult

logger = logging.getLogger(__name__)

# 환경 변수에서 Gemini API 키 로드
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    raise ValueError("환경 변수 'GEMINI_API_KEY'가 설정되지 않았습니다.")

# Gemini 클라이언트 생성 (비동기 호출은 client.aio 사용)
genai_client = genai.Client(api_key=GEMINI_API_KEY)


class LLMAnalyzer:
    def __init__(self):
        self.client = genai_client
        self.model = "gemini-2.0-flash"

    async def analyze(self, crawl_result: Any) -> AnalysisResult:
        """
        크롤링 결과를 받아 Gemini API로 분석하고,
        팀 공용 스펙인 AnalysisResult 구조로 반환합니다.
        """
        # 본문 텍스트 추출 (기존 로직 유지)
        raw_text = getattr(crawl_result, "og_description", "")
        if not raw_text and isinstance(crawl_result, dict):
            raw_text = crawl_result.get("og_description", "")

        # Gemini 프롬프트 설정 (models.py 스펙 반영)
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

        try:
            response = await self.client.aio.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.0,
                    max_output_tokens=1000,
                    # JSON 형식 강제 → 마크다운 코드블록 없이 순수 JSON 응답
                    response_mime_type="application/json",
                ),
            )
            response_text = (response.text or "").strip()
        except Exception as e:
            logger.error("Gemini API 호출 실패: %s", e)
            response_text = ""

        # JSON 파싱 (실패 시 로깅 후 빈 dict 처리)
        data = {}
        if response_text:
            try:
                data = json.loads(response_text)
            except json.JSONDecodeError:
                # 안전장치: 혹시 코드블록이 섞여 오면 제거 후 재시도
                cleaned = response_text.strip("`").removeprefix("json").strip()
                try:
                    data = json.loads(cleaned)
                except json.JSONDecodeError:
                    logger.warning("Gemini 응답 JSON 파싱 실패: %s", response_text)

        # 팀 스펙 모델에 맞게 객체 생성
        analysis_result = AnalysisResult(
            category=data.get("category", "맛집"),
            place_name=data.get("place_name"),
            address=data.get("address"),
            summary=data.get("summary"),
        )

        return analysis_result