from pydantic import BaseModel, Field, HttpUrl
from typing import Optional, Any

# ── 1. 단축어 요청 바디 ──
class ArchiveRequest(BaseModel):
    url: HttpUrl = Field(..., description="아카이빙할 SNS 게시물 URL")

# ── 2. 크롤러 리턴 스펙 ──
class CrawlResult(BaseModel):
    url: str
    og_description: str = Field("", description="og:description 메타 태그 본문")
    og_title: str = Field("", description="og:title")
    og_image: str = Field("", description="og:image URL")
    og_site_name: str = Field("", description="og:site_name")
    raw_title: str = Field("", description="HTML <title> 태그")

# ── 3. 세희님 담당: Claude 분석 리턴 스펙 ──
class AnalysisResult(BaseModel):
    category: str = Field("맛집", description="콘텐츠 카테고리")
    place_name: Optional[str] = Field(None, description="장소명")
    address: Optional[str] = Field(None, description="주소")
    summary: Optional[str] = Field(None, description="한 줄 요약")

# ── 4. 딥링크 리턴 스펙 ──
class DeeplinkResult(BaseModel):
    map_deeplink: Optional[str] = Field(None, description="지도 앱 딥링크")
    calendar_deeplink: Optional[str] = Field(None, description="캘린더 딥링크")
    memo_deeplink: Optional[str] = Field(None, description="메모 앱 딥링크")

# ── 5. 최종 리턴 전체 바디 ──
class ArchiveResponse(BaseModel):
    success: bool
    crawl: Optional[CrawlResult] = None
    analysis: Optional[AnalysisResult] = None
    deeplinks: Optional[DeeplinkResult] = None
    error: Optional[str] = None