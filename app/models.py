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


# ── LLM 분석 결과 ────────────────────────────────────────
class AnalysisResult(BaseModel):
    """LLM이 반환하는 구조화된 분석 결과."""

    category: str = Field(
        ..., description="콘텐츠 카테고리 (place | event | recipe | tip | other)"
    )
    summary: str = Field(..., description="한 줄 요약")
    place_name: str | None = Field(None, description="장소명 (place일 때)")
    address: str | None = Field(None, description="주소 (place일 때)")
    latitude: float | None = Field(None, description="위도")
    longitude: float | None = Field(None, description="경도")
    event_title: str | None = Field(None, description="일정 제목 (event일 때)")
    event_date: str | None = Field(None, description="ISO-8601 날짜 (event일 때)")
    tags: list[str] = Field(default_factory=list, description="해시태그 / 키워드")
    region: str | None = Field(None, description="지역명 (예: 연남동) — 네이버 매칭 대조용")


# ── 딥링크 ───────────────────────────────────────────────
class DeeplinkResult(BaseModel):
    """딥링크 생성 모듈의 반환값."""

    map_deeplink: str | None = Field(None, description="지도 앱 딥링크")
    calendar_deeplink: str | None = Field(None, description="캘린더 딥링크")
    memo_deeplink: str | None = Field(None, description="메모/노트 딥링크")


# ── Final Response ───────────────────────────────────────
class ArchiveResponse(BaseModel):
    """클라이언트(단축어)에 최종 반환하는 응답."""

    success: bool
    crawl: CrawlResult | None = None
    analysis: AnalysisResult | None = None
    deeplinks: DeeplinkResult | None = None
    error: str | None = None