"""공용 Pydantic 스키마 — 모듈 간 데이터 계약."""

from __future__ import annotations

from pydantic import BaseModel, Field, HttpUrl


# ── Request ──────────────────────────────────────────────
class ArchiveRequest(BaseModel):
    """단축어(Shortcut)로부터 전달받는 요청 바디."""

    url: HttpUrl = Field(..., description="아카이빙할 SNS 게시물 URL")


# ── Crawler → LLM ────────────────────────────────────────
class CrawlResult(BaseModel):
    """크롤러가 반환하는 메타 태그 추출 결과."""

    url: str
    og_description: str = Field("", description="og:description 메타 태그 본문")
    og_title: str = Field("", description="og:title (있을 경우)")
    og_image: str = Field("", description="og:image URL (있을 경우)")
    og_site_name: str = Field("", description="og:site_name (있을 경우)")
    raw_title: str = Field("", description="HTML <title> 태그 (있을 경우)")


# ── LLM 1차 분석 스키마 (Gemini response_schema로 강제) ───
class LLMExtraction(BaseModel):
    """Gemini 1차 호출(본문 분석)에 강제하는 응답 스키마.

    response_schema로 이 모델을 직접 넘기면 Gemini API가 이 구조를
    스스로 지키도록 강제하므로, 수동 json.loads() + 코드블록 제거 같은
    방어 로직 없이도 파싱 실패 가능성이 크게 줄어든다."""

    category: str = Field(..., description="place | event | recipe | tip | other")
    summary: str = Field(..., description="한 줄 요약")
    memo_body: str = Field(
        "",
        description=(
            "메모 앱에 그대로 저장할 본문. 첫 줄=제목, 둘째 줄부터=내용. "
            "SNS 메타데이터·홍보문구를 걷어내고 정리한 결과. 저장할 게 없으면 빈 문자열"
        ),
    )
    place_name: str | None = Field(None, description="상호명 (본문에 없으면 null)")
    region: str | None = Field(None, description="동/구 단위 지역명 (본문에 없으면 null)")
    address: str | None = Field(None, description="정확한 주소 (본문에 없으면 null)")
    tags: list[str] = Field(default_factory=list, description="해시태그 / 키워드")
    event_title: str | None = Field(None, description="일정 제목 (event일 때만)")
    event_date: str | None = Field(None, description="YYYY-MM-DD (event일 때만, 없으면 null)")


# ── LLM 액션 선택 (Function Calling) ─────────────────────
class ActionCall(BaseModel):
    """LLM이 Function Calling으로 직접 선택한 액션과 인자.

    action: Gemini에 등록된 함수 선언 이름과 동일해야 한다
            (예: "create_map_deeplink").
    args:   해당 함수의 파라미터 dict (예: {"query": "..."})."""

    action: str
    args: dict = Field(default_factory=dict)


# ── LLM 분석 결과 ────────────────────────────────────────
class AnalysisResult(BaseModel):
    """LLM이 반환하는 구조화된 분석 결과."""

    category: str = Field(
        ..., description="콘텐츠 카테고리 (place | event | recipe | tip | other)"
    )
    summary: str = Field("", description="한 줄 요약 (LLM 실패 시에도 빈 문자열로 안전하게 처리)")
    memo_body: str = Field("", description="LLM이 정제한 메모 본문 (첫 줄=제목). 없으면 빈 문자열")
    place_name: str | None = Field(None, description="장소명 (place일 때)")
    address: str | None = Field(None, description="주소 (place일 때)")
    event_title: str | None = Field(None, description="일정 제목 (event일 때)")
    event_date: str | None = Field(None, description="ISO-8601 날짜 (event일 때)")
    tags: list[str] = Field(default_factory=list, description="해시태그 / 키워드")
    region: str | None = Field(None, description="지역명 (예: 연남동) — 네이버 매칭 대조용")
    actions: list[ActionCall] = Field(
        default_factory=list,
        description="LLM이 Function Calling으로 직접 선택한 실행할 액션 목록",
    )


# ── 딥링크 ───────────────────────────────────────────────
class DeeplinkResult(BaseModel):
    """딥링크/액션 페이로드 생성 모듈의 반환값.

    메모만 URL이 아니라 텍스트인 이유: Apple 메모에는 본문을 채우는 공개
    URL 스킴이 없다(mobilenotes://는 비공개 스킴이라 노트를 저장하지 않음).
    대신 클라이언트인 단축어에 네이티브 '메모 생성' 액션이 있고 온디바이스라
    URL 길이·인코딩·개행 제약이 전혀 없다. 그래서 서버는 텍스트를 그대로
    내려주고 단축어가 그걸 본문 칸에 꽂는다.
    """

    map_deeplink: str | None = Field(None, description="지도 앱 딥링크")
    calendar_deeplink: str | None = Field(None, description="캘린더 딥링크")
    memo_text: str | None = Field(
        None, description="메모 앱에 그대로 넣을 본문 (단축어의 '메모 생성' 액션이 사용)"
    )


# ── Final Response ───────────────────────────────────────
class ArchiveResponse(BaseModel):
    """클라이언트(단축어)에 최종 반환하는 응답."""

    success: bool
    crawl: CrawlResult | None = None
    analysis: AnalysisResult | None = None
    deeplinks: DeeplinkResult | None = None
    error: str | None = None