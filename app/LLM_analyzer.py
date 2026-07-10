"""Gemini 기반 콘텐츠 분석 모듈.

크롤링된 og:description 원문을 팀 공용 스펙(models.AnalysisResult)에 맞춰
구조화한다. category 값은 반드시 models.py / deeplink.py / database.py가
공유하는 영문 5종("place" | "event" | "recipe" | "tip" | "other")과
일치해야 하며, 여기서 어긋나면 지도 딥링크 생성과 Notion 조건부 속성
저장이 전부 스킵된다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Final

from google import genai
from google.genai import types
from pydantic import ValidationError

from app.models import ActionCall, AnalysisResult

logger = logging.getLogger(__name__)

# 환경 변수에서 Gemini API 키 로드
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    raise ValueError("환경 변수 'GEMINI_API_KEY'가 설정되지 않았습니다.")

# Gemini 클라이언트 생성 (비동기 호출은 client.aio 사용)
genai_client = genai.Client(api_key=GEMINI_API_KEY)

# ── 429(Rate Limit) 대응 설정 ────────────────────────────
_MAX_INPUT_CHARS: Final = 4000  # 제미나이에 보내는 본문 최대 길이 (토큰 절약)
_MIN_REQUEST_INTERVAL: Final = 2.0  # 연속 호출 사이 최소 간격(초) — 무료 플랜 RPM 방어
_MAX_RETRIES: Final = 3
_RETRY_BACKOFF_BASE: Final = 4.0  # 초. 429 발생 시 시도 횟수만큼 지수적으로 대기

# /archive 요청이 동시에 여러 건 들어와도 Gemini 호출은 이 락을 거쳐
# 전역적으로 최소 간격을 지키도록 한다 (단순 for-loop sleep보다 견고함).
_rate_limit_lock = asyncio.Lock()
_last_call_ts: float = 0.0

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

# ── 액션 선택 (Function Calling) ─────────────────────────
# 새 액션을 추가하고 싶으면 여기 함수 선언 하나 + deeplink.py의
# _ACTION_BUILDERS에 매핑 하나만 추가하면 된다. category별 if/elif는 없다.
_MAP_ACTION_DECL = types.FunctionDeclaration(
    name="create_map_deeplink",
    description=(
        "장소(맛집/카페/술집/여행지 등) 정보가 있어서 지도 앱에서 바로 "
        "검색해볼 수 있는 딥링크가 필요할 때 호출."
    ),
    parameters={
        "type": "OBJECT",
        "properties": {
            "query": {
                "type": "STRING",
                "description": (
                    "지도 검색에 쓸 쿼리. 주소와 상호명을 조합해서 만들어줘 "
                    "(예: '마포구 희우정로20길 70 온리디스베이커리'). "
                    "주소가 없으면 상호명+지역명만이라도 조합."
                ),
            }
        },
        "required": ["query"],
    },
)

_CALENDAR_ACTION_DECL = types.FunctionDeclaration(
    name="create_calendar_deeplink",
    description="날짜/기간이 명시된 행사(전시/공연/팝업 등)라서 캘린더에 등록하면 좋을 때 호출.",
    parameters={
        "type": "OBJECT",
        "properties": {
            "event_title": {"type": "STRING", "description": "캘린더에 등록할 일정 제목"},
            "event_date": {
                "type": "STRING",
                "description": "YYYY-MM-DD 형식 날짜. 본문에 명시 안 됐으면 생략.",
            },
        },
        "required": ["event_title"],
    },
)

_MEMO_ACTION_DECL = types.FunctionDeclaration(
    name="create_memo_deeplink",
    description="장소/일정과 무관한 정보성 콘텐츠(레시피, 꿀팁 등)라서 메모 앱에 저장해두면 좋을 때 호출.",
    parameters={
        "type": "OBJECT",
        "properties": {
            "text": {"type": "STRING", "description": "메모에 저장할 핵심 요약 텍스트"},
        },
        "required": ["text"],
    },
)

_ACTION_TOOLS: Final = [
    types.Tool(
        function_declarations=[
            _MAP_ACTION_DECL,
            _CALENDAR_ACTION_DECL,
            _MEMO_ACTION_DECL,
        ]
    )
]

_ACTION_PROMPT_TEMPLATE = """\
아래는 SNS 게시물을 분석한 결과다. 이 정보를 참고해서 사용자에게 실제로
도움이 될 액션을 0개 이상 선택해 함수 호출로 실행해줘.

- 관련 없는 함수는 절대 호출하지 마 (예: 장소 정보 없는데 지도 함수 호출 금지)
- 해당하는 함수가 여러 개면 전부 호출해도 됨
- 애매하면 호출하지 말 것

[분석 결과]
{analysis_json}
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


def _clean_and_truncate(
    text: str, max_chars: int = _MAX_INPUT_CHARS, tail_ratio: float = 0.4
) -> str:
    """제미나이에 보낼 본문에서 불필요한 공백/개행을 줄이고 길이를 제한한다.

    토큰(=요금/RPM 제한) 절약이 목적. 단, 인스타 캡션은 보통

        [도입부 스토리텔링]
        -
        📍 상호명 / 주소
        ⏰ 영업시간

    처럼 상호명·주소 같은 핵심 정보가 맨 뒤에 몰리는 경우가 많다.
    그래서 뒤를 그냥 잘라내지 않고, 앞부분(맥락)과 뒷부분(핵심 정보)을
    둘 다 남기고 중간만 생략한다. tail_ratio는 뒤쪽에 배분하는 비율
    (기본 40%) — 상호명/주소가 잘릴 위험을 줄이기 위해 앞보다 넉넉히 준다.
    """
    if not text:
        return text
    cleaned = re.sub(r"\n{3,}", "\n\n", text.strip())  # 과도한 빈 줄 압축
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)  # 연속 공백 압축

    if len(cleaned) <= max_chars:
        return cleaned

    marker = "\n...(중략)...\n"
    budget = max(max_chars - len(marker), 0)
    tail_len = int(budget * tail_ratio)
    head_len = budget - tail_len

    head = cleaned[:head_len].rstrip()
    tail = cleaned[-tail_len:].lstrip() if tail_len > 0 else ""
    return f"{head}{marker}{tail}" if tail else head


def _is_retryable_error(exc: Exception) -> bool:
    """일시적으로 재시도하면 나아질 가능성이 있는 에러인지 판별.

    - 429 / RESOURCE_EXHAUSTED / quota: rate limit
    - 503 / UNAVAILABLE: 모델 과부하 (일시적 현상, 재시도하면 대부분 해결됨)
    """
    text = str(exc)
    return (
        "429" in text
        or "RESOURCE_EXHAUSTED" in text
        or "rate limit" in text.lower()
        or "quota" in text.lower()
        or "503" in text
        or "UNAVAILABLE" in text
    )


class LLMAnalyzer:
    def __init__(self):
        self.client = genai_client
        self.model = "gemini-3.5-flash"

    async def _call_gemini_raw(
        self, prompt: str, config: types.GenerateContentConfig
    ) -> Any | None:
        """전역 최소 호출 간격 + 429 재시도를 공통 처리하는 저수준 호출.

        text 응답(JSON 분석)과 function-calling 응답(액션 선택) 양쪽에서
        재사용한다. 실패 시 None을 반환 (호출부가 각자 폴백 처리)."""
        global _last_call_ts

        for attempt in range(1, _MAX_RETRIES + 1):
            # 여러 요청이 동시에 들어와도 Gemini 호출은 순차적으로,
            # 최소 _MIN_REQUEST_INTERVAL초 간격을 두고 나가도록 보장
            async with _rate_limit_lock:
                elapsed = time.monotonic() - _last_call_ts
                if elapsed < _MIN_REQUEST_INTERVAL:
                    await asyncio.sleep(_MIN_REQUEST_INTERVAL - elapsed)
                _last_call_ts = time.monotonic()

            try:
                return await self.client.aio.models.generate_content(
                    model=self.model, contents=prompt, config=config,
                )
            except Exception as e:
                if _is_retryable_error(e) and attempt < _MAX_RETRIES:
                    backoff = _RETRY_BACKOFF_BASE * attempt
                    logger.warning(
                        "[%d/%d] Gemini 일시 오류(429/503) 감지 → %.1f초 대기 후 재시도: %s",
                        attempt,
                        _MAX_RETRIES,
                        backoff,
                        e,
                    )
                    await asyncio.sleep(backoff)
                    continue
                logger.error("Gemini API 호출 실패: %s", e)
                return None
        return None

    async def _call_gemini_with_retry(self, prompt: str) -> str:
        """JSON 구조화 분석용 호출. 실패 시 빈 문자열 반환
        (analyze()가 이후 폴백 로직으로 안전하게 처리하도록)."""
        config = types.GenerateContentConfig(
            temperature=0.0,
            max_output_tokens=2000,
            # JSON 형식 강제 → 마크다운 코드블록 없이 순수 JSON 응답
            response_mime_type="application/json",
            # gemini-3.5-flash의 내부 reasoning(thinking) 토큰이
            # max_output_tokens 예산을 함께 소비해 응답이 잘리는 걸 방지
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )
        response = await self._call_gemini_raw(prompt, config)
        if response is None:
            return ""
        return (response.text or "").strip()

    async def _choose_actions(self, analysis: AnalysisResult) -> list[ActionCall]:
        """분석 결과를 바탕으로 LLM이 Function Calling으로 액션을 직접
        선택하게 한다. category별 if/elif 분기 없이, 모델이 호출한 함수
        이름 + 인자를 그대로 ActionCall로 담아 반환한다.

        실패하거나 아무 함수도 호출하지 않으면 빈 리스트를 반환한다.
        """
        analysis_json = analysis.model_dump_json(exclude={"actions"})
        prompt = _ACTION_PROMPT_TEMPLATE.format(analysis_json=analysis_json)

        config = types.GenerateContentConfig(
            temperature=0.0,
            max_output_tokens=500,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            tools=_ACTION_TOOLS,
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode="AUTO")
            ),
        )
        response = await self._call_gemini_raw(prompt, config)
        if response is None or not response.candidates:
            return []

        actions: list[ActionCall] = []
        parts = response.candidates[0].content.parts or []
        for part in parts:
            fc = getattr(part, "function_call", None)
            if fc is None:
                continue
            args = dict(fc.args) if fc.args else {}
            actions.append(ActionCall(action=fc.name, args=args))
            logger.info("LLM이 액션 선택: %s(%s)", fc.name, args)

        return actions

    async def analyze(self, crawl_result: Any) -> AnalysisResult:
        """크롤링 결과를 Gemini로 분석해 AnalysisResult로 반환.

        Gemini 호출/파싱/검증 중 어느 단계가 실패하더라도 예외를 던지지
        않고 category="other" + 안전한 summary로 폴백한다 (파이프라인이
        /archive 단계에서 500으로 죽지 않도록).
        """
        raw_text = _extract_source_text(crawl_result)
        raw_text = _clean_and_truncate(raw_text)

        prompt = _PROMPT_TEMPLATE.format(raw_text=raw_text)
        response_text = await self._call_gemini_with_retry(prompt)

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

        # 액션 선택 (Function Calling) — 실패해도 파이프라인은 계속 진행
        try:
            analysis_result.actions = await self._choose_actions(analysis_result)
        except Exception as e:
            logger.warning("액션 선택 실패, 액션 없이 진행: %s", e)

        logger.info(
            "LLM 분석 완료: category=%s, place_name=%s, region=%s, actions=%s",
            analysis_result.category,
            analysis_result.place_name,
            analysis_result.region,
            [a.action for a in analysis_result.actions],
        )
        return analysis_result