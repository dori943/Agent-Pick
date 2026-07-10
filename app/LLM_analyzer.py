"""Gemini 기반 콘텐츠 분석 모듈.

크롤링된 og:description 원문을 팀 공용 스펙(models.AnalysisResult)에 맞춰
구조화한다. category 값은 반드시 models.py / deeplink.py / database.py가
공유하는 영문 5종("place" | "event" | "recipe" | "tip" | "other")과
일치해야 하며, 여기서 어긋나면 지도 딥링크 생성과 Notion 조건부 속성
저장이 전부 스킵된다.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Final

from google import genai
from google.genai import types
from pydantic import ValidationError

from app.models import AnalysisResult

logger = logging.getLogger(__name__)

# 환경 변수에서 Gemini API 키 로드
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    raise ValueError("환경 변수 'GEMINI_API_KEY'가 설정되지 않았습니다.")

# Gemini 클라이언트 생성 (비동기 호출은 client.aio 사용)
genai_client = genai.Client(api_key=GEMINI_API_KEY)

# ── 팀 공용 category 스펙 (models.py / deeplink.py / database.py와 동일해야 함) ──
_VALID_CATEGORIES: Final = {"place", "event", "recipe", "tip", "other"}

# Gemini가 지시를 무시하고 한글/유사어로 응답하는 경우를 대비한 방어적 매핑
_CATEGORY_ALIASES: Final = {
    "맛집": "place",
    "카페": "place",
    "장소": "place",
    "가게": "place",
    "숙소": "place",
    "여행지": "place",
    "이벤트": "event",
    "전시": "event",
    "공연": "event",
    "행사": "event",
    "팝업": "event",
    "레시피": "recipe",
    "요리": "recipe",
    "꿀팁": "tip",
    "정보": "tip",
    "팁": "tip",
}

_PROMPT_TEMPLATE = """\
아래 SNS 게시물 본문을 분석해서 정보를 추출해줘.

category는 반드시 아래 5개 영어 값 중 하나로만 응답해 (한글 금지):
- "place": 맛집/카페/술집/여행지 등 특정 장소·업체 소개
- "event": 전시/공연/팝업스토어 등 날짜·기간이 있는 행사
- "recipe": 요리 레시피
- "tip": 장소 특정 없는 정보성 꿀팁
- "other": 위 어디에도 해당 안 됨

응답은 아래 JSON 형식으로만 해. 설명, 마크다운 코드블록 없이 순수 JSON만.
본문에 없는 정보는 null로 채워.

{{
    "category": "place",
    "summary": "한 줄 요약 (항상 필수로 채울 것)",
    "place_name": "상호명 또는 null",
    "region": "동/구 단위 지역명. 예: 연남동, 성수동. place일 때 지도 검색 정확도를 위해 본문/해시태그에서 최대한 추출. 없으면 null",
    "address": "본문에 명시된 정확한 주소 또는 null",
    "tags": ["해시태그", "키워드"],
    "event_title": "일정 제목 또는 null (event일 때만)",
    "event_date": "YYYY-MM-DD 형식 날짜 또는 null (event일 때, 본문에 날짜가 명시된 경우만)"
}}

[본문]
{raw_text}
"""


def _normalize_category(raw: Any) -> str:
    """Gemini 응답 category를 팀 스펙 5종으로 정규화."""
    if not isinstance(raw, str):
        return "other"
    value = raw.strip()
    if value in _VALID_CATEGORIES:
        return value
    return _CATEGORY_ALIASES.get(value, "other")


def _normalize_tags(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(t).strip() for t in raw if str(t).strip()]


def _clean_instagram_title(raw_title: str) -> str:
    """인스타그램 <title> 태그는 보통

        '계정명 (@handle) on Instagram: "캡션 내용"'
        '계정명님의 Instagram: "캡션 내용"'

    형태라 콜론+따옴표 뒤의 실제 캡션만 추출한다. 패턴이 없으면 원본 그대로.
    """
    m = re.search(r':\s*"', raw_title)
    if not m:
        return raw_title
    caption = raw_title[m.end():]
    return caption.rstrip('"').strip()


def _extract_source_text(crawl_result: Any) -> str:
    """크롤러가 og_description 추출에 실패해도 raw_title/og_title에 캡션이
    남아있는 경우가 많다 (특히 Instagram). 우선순위대로 폴백한다.
    """

    def _get(key: str) -> str:
        if isinstance(crawl_result, dict):
            return (crawl_result.get(key) or "").strip()
        return (getattr(crawl_result, key, "") or "").strip()

    og_description = _get("og_description")
    if og_description:
        return og_description

    raw_title = _get("raw_title")
    if raw_title:
        cleaned = _clean_instagram_title(raw_title)
        logger.info("og_description 비어있어 raw_title에서 폴백 추출 (%d자)", len(cleaned))
        return cleaned

    og_title = _get("og_title")
    if og_title:
        logger.info("og_description/raw_title 모두 비어있어 og_title로 폴백")
        return og_title

    return ""


def _fallback_summary(raw_text: str) -> str:
    """summary는 AnalysisResult 필수 필드이므로 절대 빈 값이 되면 안 된다."""
    if raw_text:
        snippet = raw_text.strip().replace("\n", " ")
        return snippet[:80] + ("…" if len(snippet) > 80 else "")
    return "요약 정보를 추출하지 못했습니다."


class LLMAnalyzer:
    def __init__(self):
        self.client = genai_client
        self.model = "gemini-2.0-flash"

    async def analyze(self, crawl_result: Any) -> AnalysisResult:
        """크롤링 결과를 Gemini로 분석해 AnalysisResult로 반환.

        Gemini 호출/파싱/검증 중 어느 단계가 실패하더라도 예외를 던지지
        않고 category="other" + 안전한 summary로 폴백한다 (파이프라인이
        /archive 단계에서 500으로 죽지 않도록).
        """
        raw_text = _extract_source_text(crawl_result)

        prompt = _PROMPT_TEMPLATE.format(raw_text=raw_text)

        response_text = ""
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

        # JSON 파싱 (실패 시 로깅 후 빈 dict 처리)
        data: dict[str, Any] = {}
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

        category = _normalize_category(data.get("category"))
        summary = data.get("summary") or _fallback_summary(raw_text)
        tags = _normalize_tags(data.get("tags"))

        # 안전장치: place_name/address는 뽑혔는데 category만 "other"로
        # 잘못 나온 경우 "place"로 보정 (Gemini의 카테고리 판단 실수 방어)
        if category == "other" and (data.get("place_name") or data.get("address")):
            logger.info("place_name/address 존재 → category를 'place'로 보정")
            category = "place"

        try:
            analysis_result = AnalysisResult(
                category=category,
                summary=summary,
                place_name=data.get("place_name") or None,
                address=data.get("address") or None,
                region=data.get("region") or None,
                event_title=data.get("event_title") or None,
                event_date=data.get("event_date") or None,
                tags=tags,
            )
        except ValidationError as e:
            # 스펙 불일치로 인한 크래시 방지: 최소 필드만으로 안전하게 폴백
            logger.error("AnalysisResult 검증 실패, 폴백 처리: %s", e)
            analysis_result = AnalysisResult(category="other", summary=_fallback_summary(raw_text))

        logger.info(
            "LLM 분석 완료: category=%s, place_name=%s, region=%s",
            analysis_result.category,
            analysis_result.place_name,
            analysis_result.region,
        )
        return analysis_result