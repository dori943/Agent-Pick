"""로그인 프리 메타 태그 크롤러.

인스타그램 등 SNS 공개 게시물의 og:description 메타 태그를
별도 로그인 없이 추출한다.

전략 (Instagram) — 3단계 fallback:
  1차) HTML GET → og:description 메타 태그 파싱
  2차) /embed/ 페이지 → 캡션 텍스트 직접 추출
  3차) oEmbed API (토큰 필요할 수 있어 최후 수단)
"""

from __future__ import annotations

import json
import logging
import re
from typing import Final
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup

from app.models import CrawlResult

logger = logging.getLogger(__name__)

# ── 상수 ─────────────────────────────────────────────────
_TIMEOUT: Final[float] = 10.0
_MAX_REDIRECTS: Final[int] = 5

_HEADERS: Final[dict[str, str]] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Dest": "document",
}

_INSTA_PATTERN = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)"
)

_OEMBED_URL: Final[str] = "https://api.instagram.com/oembed/?url={url}"


# ── 헬퍼 ─────────────────────────────────────────────────
def _is_instagram(url: str) -> bool:
    return bool(_INSTA_PATTERN.search(url))


def _get_shortcode(url: str) -> str | None:
    m = _INSTA_PATTERN.search(url)
    return m.group(1) if m else None


def _normalize_instagram_url(url: str) -> str:
    """인스타그램 URL의 쿼리 파라미터를 제거하고 표준화."""
    m = _INSTA_PATTERN.search(url)
    if m:
        return f"https://www.instagram.com/p/{m.group(1)}/"
    return url


def _extract_og_tags(html: str) -> dict[str, str]:
    """HTML에서 주요 Open Graph 메타 태그를 딕셔너리로 추출."""
    soup = BeautifulSoup(html, "html.parser")
    og: dict[str, str] = {}

    for tag in soup.find_all("meta", attrs={"property": True}):
        prop: str = tag.get("property", "")
        content: str = tag.get("content", "")
        if prop.startswith("og:") and content:
            key = prop.replace(":", "_")
            og[key] = content.strip()

    title_tag = soup.find("title")
    if title_tag and title_tag.string:
        og["raw_title"] = title_tag.string.strip()

    return og


# ── 전략 1: HTML OG 태그 ─────────────────────────────────
async def _try_html_og(url: str, client: httpx.AsyncClient) -> dict[str, str]:
    """직접 HTML GET → OG 태그 파싱."""
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        og = _extract_og_tags(resp.text)
        if og.get("og_description"):
            logger.info("[전략1] HTML OG 추출 성공")
            return og
        logger.info("[전략1] og:description 비어있음 → 다음 전략으로")
    except httpx.HTTPStatusError as exc:
        logger.info("[전략1] HTTP %s → 다음 전략으로", exc.response.status_code)
    except httpx.RequestError as exc:
        logger.info("[전략1] 네트워크 오류: %s", exc)
    return {}


# ── 전략 2: /embed/ 페이지 파싱 ──────────────────────────
async def _try_embed_page(url: str, client: httpx.AsyncClient) -> dict[str, str]:
    """Instagram /embed/ 페이지는 서버사이드 렌더링이라
    캡션이 HTML에 직접 포함되어 있다.
    """
    shortcode = _get_shortcode(url)
    if not shortcode:
        return {}

    embed_url = f"https://www.instagram.com/p/{shortcode}/embed/"
    try:
        resp = await client.get(embed_url)
        resp.raise_for_status()
        html = resp.text
        soup = BeautifulSoup(html, "html.parser")

        caption = ""

        # 방법 A: embed 페이지의 캡션 div
        # Instagram embed 페이지는 캡션을 다양한 클래스명으로 감쌈
        for selector in [
            "div.Caption",
            "div.CaptionContent",
            'div[class*="Caption"]',
            'span[class*="caption"]',
        ]:
            el = soup.select_one(selector)
            if el and el.get_text(strip=True):
                caption = el.get_text(separator=" ", strip=True)
                break

        # 방법 B: embed HTML 내 JSON-LD 또는 인라인 스크립트에서 추출
        if not caption:
            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    ld = json.loads(script.string or "")
                    if isinstance(ld, dict):
                        caption = ld.get("caption", "") or ld.get("articleBody", "")
                        if caption:
                            break
                except json.JSONDecodeError:
                    continue

        # 방법 C: og 태그가 embed 페이지에 있을 수도 있음
        if not caption:
            og_embed = _extract_og_tags(html)
            caption = og_embed.get("og_description", "")

        if caption:
            logger.info("[전략2] embed 페이지에서 캡션 추출 성공 (%d자)", len(caption))
            return {
                "og_description": caption,
                "og_site_name": "Instagram",
            }

        logger.info("[전략2] embed 페이지에서도 캡션 없음 → 다음 전략으로")
    except httpx.HTTPStatusError as exc:
        logger.info("[전략2] embed HTTP %s", exc.response.status_code)
    except httpx.RequestError as exc:
        logger.info("[전략2] embed 네트워크 오류: %s", exc)
    return {}


# ── 전략 3: oEmbed API ───────────────────────────────────
async def _try_oembed(url: str, client: httpx.AsyncClient) -> dict[str, str]:
    """Instagram oEmbed API (토큰 필요할 수 있어 최후 수단)."""
    oembed_endpoint = _OEMBED_URL.format(url=quote(url, safe=""))
    try:
        resp = await client.get(oembed_endpoint)
        resp.raise_for_status()
        data = resp.json()
        logger.info("[전략3] oEmbed 응답 수신")

        result: dict[str, str] = {}
        if data.get("title"):
            result["og_description"] = data["title"]
        if data.get("author_name"):
            result["og_title"] = f"{data['author_name']}의 Instagram 게시물"
        if data.get("thumbnail_url"):
            result["og_image"] = data["thumbnail_url"]
        result["og_site_name"] = "Instagram"
        return result
    except (httpx.HTTPStatusError, httpx.RequestError, json.JSONDecodeError) as exc:
        logger.warning("[전략3] oEmbed 실패: %s", exc)
    return {}


# ── 공개 API ─────────────────────────────────────────────
async def crawl_meta(url: str, *, client: httpx.AsyncClient | None = None) -> CrawlResult:
    """주어진 URL의 OG 메타 태그를 크롤링하여 CrawlResult로 반환.

    Instagram의 경우 3단계 fallback 전략을 순차 시도한다:
      1) HTML OG → 2) embed 페이지 → 3) oEmbed API
    """
    normalized = _normalize_instagram_url(url)
    logger.info("crawl_meta 시작: %s", normalized)

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(
            headers=_HEADERS,
            timeout=httpx.Timeout(_TIMEOUT),
            follow_redirects=True,
            max_redirects=_MAX_REDIRECTS,
        )

    try:
        # 1차: HTML OG 태그
        og = await _try_html_og(normalized, client)

        # Instagram 전용 fallback 체인
        if not og.get("og_description") and _is_instagram(normalized):
            # 2차: embed 페이지
            og = await _try_embed_page(normalized, client)

        if not og.get("og_description") and _is_instagram(normalized):
            # 3차: oEmbed API
            og = await _try_oembed(normalized, client)

        if not og.get("og_description"):
            logger.warning("모든 추출 전략 실패: %s", normalized)

        return CrawlResult(
            url=normalized,
            og_description=og.get("og_description", ""),
            og_title=og.get("og_title", ""),
            og_image=og.get("og_image", ""),
            og_site_name=og.get("og_site_name", ""),
            raw_title=og.get("raw_title", ""),
        )
    finally:
        if owns_client:
            await client.aclose()