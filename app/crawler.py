"""로그인 프리 메타 태그 크롤러.

인스타그램 등 SNS 공개 게시물의 og:description 메타 태그를
별도 로그인 없이 추출한다.
httpx(비동기 HTTP) + BeautifulSoup(HTML 파싱) 조합.
"""

from __future__ import annotations

import logging
import re
from typing import Final

import httpx
from bs4 import BeautifulSoup

from app.models import CrawlResult

logger = logging.getLogger(__name__)

# ── 상수 ─────────────────────────────────────────────────
_TIMEOUT: Final[float] = 10.0  # 초
_MAX_REDIRECTS: Final[int] = 5


_HEADERS: Final[dict[str, str]] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    # 쿠키 배너 / 리다이렉트 우회용
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Dest": "document",
}

# 인스타그램 URL 정규화: /reel/ , /p/ 등 → 표준 형태
_INSTA_PATTERN = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)"
)


# ── 헬퍼 ─────────────────────────────────────────────────
def _normalize_instagram_url(url: str) -> str:
    """인스타그램 URL의 쿼리 파라미터를 제거하고 표준화."""
    m = _INSTA_PATTERN.search(url)
    if m:
        shortcode = m.group(1)
        return f"https://www.instagram.com/p/{shortcode}/"
    return url


def _extract_og_tags(html: str) -> dict[str, str]:
    """HTML에서 주요 Open Graph 메타 태그를 딕셔너리로 추출."""
    soup = BeautifulSoup(html, "html.parser")
    og: dict[str, str] = {}

    for tag in soup.find_all("meta", attrs={"property": True}):
        prop: str = tag.get("property", "")
        content: str = tag.get("content", "")
        if prop.startswith("og:") and content:
            # "og:description" → "og_description"
            key = prop.replace(":", "_")
            og[key] = content.strip()

    # fallback: <title> 태그
    title_tag = soup.find("title")
    if title_tag and title_tag.string:
        og["raw_title"] = title_tag.string.strip()

    return og


# ── 공개 API ─────────────────────────────────────────────
async def crawl_meta(url: str, *, client: httpx.AsyncClient | None = None) -> CrawlResult:
    """주어진 URL에서 og 메타 태그를 추출."""
    normalized = _normalize_instagram_url(url)
    logger.info("crawl_meta 시작: %s", normalized)

    # 클라이언트 주입 여부에 따라 분기
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(
            headers=_HEADERS,
            timeout=httpx.Timeout(_TIMEOUT),
            follow_redirects=True,
            max_redirects=_MAX_REDIRECTS,
        )

    try:
        resp = await client.get(normalized)
        resp.raise_for_status()
        html = resp.text
    except httpx.HTTPStatusError as exc:
        logger.warning("HTTP %s for %s", exc.response.status_code, normalized)
        return CrawlResult(url=normalized)
    except httpx.RequestError as exc:
        logger.error("Request failed for %s: %s", normalized, exc)
        return CrawlResult(url=normalized)
    finally:
        if owns_client:
            await client.aclose()

    og = _extract_og_tags(html)
    logger.info("추출된 OG 키: %s", list(og.keys()))

    return CrawlResult(
        url=normalized,
        og_description=og.get("og_description", ""),
        og_title=og.get("og_title", ""),
        og_image=og.get("og_image", ""),
        og_site_name=og.get("og_site_name", ""),
        raw_title=og.get("raw_title", ""),
    )