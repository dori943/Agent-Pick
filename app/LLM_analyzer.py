"""Gemini 기반 콘텐츠 분석 모듈.

크롤링된 og:description 원문을 팀 공용 스펙(models.AnalysisResult)에 맞춰
구조화한다. category 값은 반드시 models.py / deeplink.py / database.py가
공유하는 영문 5종("place" | "event" | "recipe" | "tip" | "other")과
일치해야 하며, 여기서 어긋나면 지도 딥링크 생성과 Notion 조건부 속성
저장이 전부 스킵된다.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any, Final

import httpx
from google import genai
from google.genai import types
from pydantic import ValidationError

from app.models import ActionCall, AnalysisResult, LLMExtraction
from app.naver_map import search_place

logger = logging.getLogger(__name__)

# 환경 변수에서 Gemini API 키 로드
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    raise ValueError("환경 변수 'GEMINI_API_KEY'가 설정되지 않았습니다.")

# Gemini 클라이언트 생성 (비동기 호출은 client.aio 사용)
genai_client = genai.Client(api_key=GEMINI_API_KEY)

# ── 429(Rate Limit) 대응 설정 ────────────────────────────
_MAX_INPUT_CHARS: Final = 4000  # 제미나이에 보내는 본문 최대 길이 (토큰 절약)
_MIN_REQUEST_INTERVAL: Final = 3.0  # 연속 호출 사이 최소 간격(초) — 무료 플랜 RPM 방어
_MAX_RETRIES: Final = 3
_RETRY_BACKOFF_BASE: Final = 3.0  # 초. 429 발생 시 시도 횟수만큼 지수적으로 대기

# /archive 요청이 동시에 여러 건 들어와도 Gemini 호출은 이 락을 거쳐
# 전역적으로 최소 간격을 지키도록 한다 (단순 for-loop sleep보다 견고함).
_rate_limit_lock = asyncio.Lock()
_last_call_ts: float = 0.0

# 주 모델이 429/503 등으로 재시도까지 전부 실패하면 이 모델로 한 번 더
# 시도한다. 에이전트 신뢰성(안 죽고 응답 주기)이 속도/최신성보다 우선.
_FALLBACK_MODEL: Final = "gemini-3.5-flash"

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
아래 SNS 게시물 본문을 분석해서 정보를 추출해줘. (응답 형식은 별도로
스키마로 강제되니, 여기서는 각 필드에 뭘 채울지만 신경 써줘.)

- category는 다음 중 하나:
  "place": 맛집/카페/술집/여행지 등 특정 장소·업체 소개
  "event": 전시/공연/팝업스토어 등 날짜·기간이 있는 행사
  "recipe": 요리 레시피
  "tip": 장소 특정 없는 정보성 꿀팁
  "other": 위 어디에도 해당 안 됨
- summary는 한 줄 요약, 항상 채울 것
- memo_body는 메모 앱에 그대로 저장할 본문. 저장할 가치가 있는 정보성
  콘텐츠일 때만 채우고(주로 recipe/tip), 아니면 빈 문자열로 둬.
  · 첫 줄은 제목 한 줄 — iOS 메모는 첫 줄이 곧 제목이다
  · 둘째 줄부터 본문. category에 맞게 정리:
      recipe -> 재료와 분량, 조리 순서를 단계별로 빠짐없이
      tip    -> 핵심 정보를 항목별로
      place/event -> 영업시간·가격·예약 등 방문에 필요한 정보만
  · 버릴 것: "28K likes, 213 comments - 계정명 - June 12, 2026:" 같은 SNS
    메타데이터, 공구/할인/프로필 링크/배송 안내 등 홍보 문구, 계정
    태그(@xxx), "꼭 드셔보세요 ❤️" 같은 인사말·감탄사
  · 지키지 말고 그대로 둘 것: 재료명과 분량("감자 3개 500g", "간장 1T"),
    온도·시간("7~10분") 등 숫자와 단위는 원문 그대로. 압축하거나 바꾸지 마
  · 본문에 없는 내용을 지어내지 마 — 정리만 하고 창작하지 말 것
- place_name / address / event_title / event_date는 본문에 실제로 명시된
  경우만 채우고, 없으면 비워둬 (본문에 없는 값을 지어내지 마)
- region은 동/구 단위 지역명 (예: 연남동, 성수동) — place일 때 지도 검색
  정확도를 위해 본문/해시태그에서 최대한 추출
- tags는 해시태그/키워드 목록

[본문]
{raw_text}
"""

# ── 액션 선택 (Function Calling) ─────────────────────────
# 새 액션을 추가하고 싶으면 여기 함수 선언 하나 + deeplink.py의
# _ACTION_BUILDERS에 매핑 하나만 추가하면 된다. category별 if/elif는 없다.
_MAP_ACTION_DECL = types.FunctionDeclaration(
    name="create_map_deeplink",
    description=(
        "장소명(place_name) 또는 주소(address) 중 하나라도 있으면 호출. "
        "category가 'event'여도 장소 정보가 함께 있으면 호출한다 "
        "(캘린더 액션과 동시에 호출될 수 있음 — 배타적이지 않음)."
    ),
    parameters={
        "type": "OBJECT",
        "properties": {
            "query": {
                "type": "STRING",
                "description": (
                    "장소명 또는 주소 중 실제로 값이 있는 걸 그대로 채워줘 "
                    "(둘 다 있으면 장소명을 우선 채운다). 이 값은 호출 여부 "
                    "판단용으로만 쓰이고, 실제 지도 검색 쿼리는 서버 코드가 "
                    "place_name/address 우선순위 규칙으로 별도 구성하니 "
                    "이름과 주소를 한 문장으로 합치려 하지 마."
                ),
            }
        },
        "required": ["query"],
    },
)

_CALENDAR_ACTION_DECL = types.FunctionDeclaration(
    name="create_calendar_deeplink",
    description=(
        "event_title에 값이 있으면 호출. 장소 정보가 함께 있어서 "
        "create_map_deeplink도 같이 호출되는 경우가 흔하다 (배타적이지 않음)."
    ),
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
    description=(
        "memo_body에 저장할 내용이 있으면 호출. 인자는 없다 — 본문은 1차 분석에서 "
        "이미 만들어져 있고 서버가 주입하므로, 여기서 본문을 다시 쓰지 마."
    ),
    parameters={"type": "OBJECT", "properties": {}},
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
아래는 SNS 게시물을 분석한 결과다. category는 참고용일 뿐이니 그것만 보고
액션을 하나만 고르지 마. 실제로는 각 필드가 채워져 있는지(데이터 존재
여부)를 독립적으로 판단해서, 해당하는 액션을 전부 호출해줘.

판단 기준 (서로 배타적이지 않음 — 둘 다 해당하면 둘 다 호출):
- place_name 또는 address 중 하나라도 값이 있으면
  → create_map_deeplink 호출
  → category가 "event"여도 장소 정보가 있으면 반드시 호출한다
    (예: 주소가 있는 팝업스토어/전시는 캘린더 + 지도 둘 다 필요)
- event_title에 값이 있으면
  → create_calendar_deeplink 호출 (event_date는 있으면 같이 넘기고 없으면 생략)
- memo_body가 "있음"이면 -> create_memo_deeplink 호출 (인자 없음)
  -> 장소/일정 액션과 배타적이지 않다. 둘 다 해당하면 둘 다 호출

- 값이 없는 필드를 근거로 삼아 호출하지 마
- 조건에 안 맞으면 억지로 호출하지 말 것

[분석 결과]
{analysis_json}

memo_body: {has_memo_body}
"""


def _build_map_query(analysis: AnalysisResult) -> str:
    """지도 검색 쿼리를 결정론적으로 구성한다.

    상호명(place_name)이 있으면 그것만(+구분이 필요하면 region) 우선
    사용하고, 상호명이 없을 때만 주소(address)를 쓴다. 상호명과 전체
    도로명주소를 한 문자열로 합치면 지도 검색엔진이 오히려 매칭에
    실패하는 경우가 많아 LLM이 만든 조합 쿼리는 쓰지 않고 여기서 새로
    만든다.
    """
    if analysis.place_name:
        # region이 place_name에 이미 포함돼 있지 않으면 동명이인/동명업체
        # 구분을 위해 지역명을 붙여준다 (예: "성수동 어니언").
        if analysis.region and analysis.region not in analysis.place_name:
            return f"{analysis.region} {analysis.place_name}"
        return analysis.place_name
    if analysis.address:
        return analysis.address
    return ""


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
        self.model = "gemini-3.1-flash-lite"
        self.fallback_model = _FALLBACK_MODEL

    async def _call_gemini_raw(
        self,
        prompt: str,
        config: types.GenerateContentConfig,
        model: str | None = None,
    ) -> Any | None:
        """전역 최소 호출 간격 + 429/503 재시도를 공통 처리하는 저수준 호출.

        text 응답(JSON 분석)과 function-calling 응답(액션 선택) 양쪽에서
        재사용한다. 실패 시 None을 반환 (호출부가 각자 폴백 처리)."""
        global _last_call_ts
        target_model = model or self.model

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
                    model=target_model, contents=prompt, config=config,
                )
            except Exception as e:
                if _is_retryable_error(e) and attempt < _MAX_RETRIES:
                    backoff = _RETRY_BACKOFF_BASE * attempt
                    logger.warning(
                        "[%d/%d] Gemini(%s) 일시 오류(429/503) 감지 → %.1f초 대기 후 재시도: %s",
                        attempt,
                        _MAX_RETRIES,
                        target_model,
                        backoff,
                        e,
                    )
                    await asyncio.sleep(backoff)
                    continue
                logger.error("Gemini(%s) API 호출 실패: %s", target_model, e)
                return None
        return None

    async def _call_gemini_with_fallback(
        self, prompt: str, config: types.GenerateContentConfig
    ) -> Any | None:
        """주 모델(self.model)로 재시도까지 다 실패하면 폴백 모델로 한 번 더
        전체 재시도 사이클을 시도한다. 에이전트가 완전히 죽지 않고 응답을
        주는 것(신뢰성)이 최신 모델 사용보다 우선."""
        response = await self._call_gemini_raw(prompt, config, model=self.model)
        if response is not None:
            return response

        logger.warning(
            "주 모델(%s) 전부 실패 → 폴백 모델(%s)로 전환", self.model, self.fallback_model
        )
        return await self._call_gemini_raw(prompt, config, model=self.fallback_model)

    async def _extract_with_gemini(self, prompt: str) -> LLMExtraction | None:
        """1차 분석 호출. response_schema=LLMExtraction으로 응답 구조를
        Gemini API 레벨에서 강제한다.

        - SDK가 스키마에 맞춰 이미 파싱해준 response.parsed를 우선 사용
        - 혹시 parsed가 없으면 텍스트를 직접 스키마로 검증
        - 둘 다 실패하면 None (analyze()가 안전하게 폴백 처리)
        """
        config = types.GenerateContentConfig(
            temperature=0.0,
            # memo_body(정제된 원문)가 응답에 실리므로 예산을 넉넉히 잡는다.
            # 한국어는 대략 1자 ≈ 1토큰이라 원문 4000자면 출력도 그만큼 나온다.
            max_output_tokens=5000,
            response_mime_type="application/json",
            response_schema=LLMExtraction,
            # gemini-3.5-flash의 내부 reasoning(thinking) 토큰이
            # max_output_tokens 예산을 함께 소비해 응답이 잘리는 걸 방지
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )
        response = await self._call_gemini_with_fallback(prompt, config)
        if response is None:
            return None

        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, LLMExtraction):
            return parsed

        text = (response.text or "").strip()
        if not text:
            return None
        try:
            return LLMExtraction.model_validate_json(text)
        except ValidationError as e:
            logger.warning("Gemini 응답이 스키마와 불일치 (response_schema 우회됨): %s", e)
            return None

    async def _choose_actions(self, analysis: AnalysisResult) -> list[ActionCall]:
        """분석 결과를 바탕으로 LLM이 Function Calling으로 액션을 직접
        선택하게 한다. category별 if/elif 분기 없이, 모델이 호출한 함수
        이름 + 인자를 기본적으로 그대로 ActionCall로 담아 반환한다.

        단, 아래 두 인자만은 예외적으로 코드에서 채운다:
        - create_map_deeplink의 query (_build_map_query 참고) — 지도 검색
          쿼리는 LLM의 자유 조합보다 "상호명 우선, 없으면 주소" 규칙을
          결정론적으로 따르는 게 검색 정확도가 훨씬 높기 때문.
        - create_memo_deeplink의 text — 1차 분석에서 이미 만들어둔
          memo_body를 그대로 넣는다. 이 프롬프트에는 원문이 없어서 LLM이
          본문을 다시 쓸 수도 없고, 쓰게 하면 원문을 두 번 보내는 셈이라
          토큰이 두 배가 된다.

        실패하거나 아무 함수도 호출하지 않으면 빈 리스트를 반환한다.
        """
        # memo_body는 수백 자라 프롬프트에 통째로 넣으면 토큰만 낭비된다.
        # 액션 선택에 필요한 건 "있냐 없냐"뿐이므로 유무만 넘긴다.
        analysis_json = analysis.model_dump_json(exclude={"actions", "memo_body"})
        prompt = _ACTION_PROMPT_TEMPLATE.format(
            analysis_json=analysis_json,
            has_memo_body="있음" if analysis.memo_body else "없음",
        )

        config = types.GenerateContentConfig(
            temperature=0.0,
            max_output_tokens=500,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            tools=_ACTION_TOOLS,
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode="AUTO")
            ),
        )
        response = await self._call_gemini_with_fallback(prompt, config)
        if response is None or not response.candidates:
            return []

        actions: list[ActionCall] = []
        parts = response.candidates[0].content.parts or []
        for part in parts:
            fc = getattr(part, "function_call", None)
            if fc is None:
                continue
            args = dict(fc.args) if fc.args else {}
            if fc.name == "create_map_deeplink":
                # LLM이 만든 query는 호출 여부 판단용 신호일 뿐, 실제 검색
                # 쿼리는 place_name 우선 규칙으로 여기서 새로 구성한다.
                built_query = _build_map_query(analysis)
                if built_query:
                    args["query"] = built_query
            elif fc.name == "create_memo_deeplink":
                # 본문은 1차 분석의 memo_body를 그대로 주입. 비어있으면
                # 넣을 게 없으므로 액션 자체를 버린다.
                if not analysis.memo_body:
                    logger.info("memo_body 비어있음 → create_memo_deeplink 무시")
                    continue
                args["text"] = analysis.memo_body
            actions.append(ActionCall(action=fc.name, args=args))
            # 메모 본문이 통째로 로그에 찍히지 않도록 인자 키/길이만 남긴다
            logger.info(
                "LLM이 액션 선택: %s(keys=%s, sizes=%s)",
                fc.name,
                list(args),
                {k: len(str(v)) for k, v in args.items()},
            )

        return actions

    async def _enrich_place_address(
        self, analysis: AnalysisResult, client: httpx.AsyncClient | None
    ) -> None:
        """category=place인데 상호명은 있고 주소가 없는 경우, 네이버 지역
        검색으로 주소를 보완한다 (제자리에서 analysis를 직접 수정).

        - NAVER_CLIENT_ID/SECRET 미설정, 검색 실패, 매칭 실패 등 어떤
          이유로든 보완에 실패해도 예외를 던지지 않고 조용히 건너뛴다
          (보완은 "있으면 좋은" 단계지 필수 단계가 아님).
        - httpx client가 없으면(단독 호출 등) 아예 시도하지 않는다.
        """
        if analysis.category != "place" or not analysis.place_name or analysis.address:
            return
        if client is None:
            logger.info("httpx client 미제공 → 주소 보완 스킵")
            return

        try:
            item = await search_place(analysis.place_name, analysis.region, client)
        except RuntimeError:
            logger.info("NAVER_CLIENT_ID/SECRET 미설정 → 주소 보완 스킵")
            return
        except Exception as e:
            logger.warning("네이버 지역 검색 실패, 주소 보완 없이 진행: %s", e)
            return

        if not item:
            logger.info("네이버 검색에서 '%s' 매칭 실패 → 주소 보완 없음", analysis.place_name)
            return

        address = item.get("roadAddress") or item.get("address")
        if address:
            analysis.address = address
            logger.info("네이버 지역 검색으로 주소 보완 완료: %s", address)


    async def analyze(
        self, crawl_result: Any, client: httpx.AsyncClient | None = None
    ) -> AnalysisResult:
        """크롤링 결과를 Gemini로 분석해 AnalysisResult로 반환.

        Gemini 호출/파싱/검증 중 어느 단계가 실패하더라도 예외를 던지지
        않고 category="other" + 안전한 summary로 폴백한다 (파이프라인이
        /archive 단계에서 500으로 죽지 않도록).
        """
        raw_text = _extract_source_text(crawl_result)
        raw_text = _clean_and_truncate(raw_text)

        prompt = _PROMPT_TEMPLATE.format(raw_text=raw_text)
        extraction = await self._extract_with_gemini(prompt)

        if extraction is not None:
            category = _normalize_category(extraction.category)
            summary = extraction.summary or _fallback_summary(raw_text)
            tags = _normalize_tags(extraction.tags)
            place_name = extraction.place_name or None
            address = extraction.address or None
            region = extraction.region or None
            event_title = extraction.event_title or None
            event_date = extraction.event_date or None
            memo_body = (extraction.memo_body or "").strip()
        else:
            logger.warning("Gemini 구조화 응답 획득 실패 → 안전 폴백으로 진행")
            category = "other"
            summary = _fallback_summary(raw_text)
            tags = []
            memo_body = ""
            place_name = address = region = event_title = event_date = None

        # 안전장치: place_name/address는 뽑혔는데 category만 "other"로
        # 잘못 나온 경우 "place"로 보정 (Gemini의 카테고리 판단 실수 방어)
        if category == "other" and (place_name or address):
            logger.info("place_name/address 존재 → category를 'place'로 보정")
            category = "place"

        try:
            analysis_result = AnalysisResult(
                category=category,
                summary=summary,
                place_name=place_name,
                address=address,
                region=region,
                event_title=event_title,
                event_date=event_date,
                tags=tags,
                memo_body=memo_body,
            )
        except ValidationError as e:
            # 스펙 불일치로 인한 크래시 방지: 최소 필드만으로 안전하게 폴백
            logger.error("AnalysisResult 검증 실패, 폴백 처리: %s", e)
            analysis_result = AnalysisResult(category="other", summary=_fallback_summary(raw_text))

        # 주소 보완: place인데 상호명만 있고 주소가 없으면 네이버 지역
        # 검색으로 보완한 뒤 액션 선택에 반영 (지도 딥링크 정확도 개선)
        try:
            await self._enrich_place_address(analysis_result, client)
        except Exception as e:
            logger.warning("주소 보완 단계 실패, 보완 없이 진행: %s", e)

        # 액션 선택 (Function Calling) — 실패해도 파이프라인은 계속 진행
        try:
            analysis_result.actions = await self._choose_actions(analysis_result)
        except Exception as e:
            logger.warning("액션 선택 실패, 액션 없이 진행: %s", e)

        logger.info(
            "LLM 분석 완료: category=%s, place_name=%s, region=%s, memo_body=%d자, actions=%s",
            analysis_result.category,
            analysis_result.place_name,
            analysis_result.region,
            len(analysis_result.memo_body),
            [a.action for a in analysis_result.actions],
        )
        return analysis_result