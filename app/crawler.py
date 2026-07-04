"""로그인 프리 메타 태그 크롤러.

인스타그램 등 SNS 공개 게시물의 og:description 메타 태그를
별도 로그인 없이 추출한다.

전략 (Instagram):
  토큰 있을 때 → oEmbed API (공식, 안정적) → HTML OG fallback
  토큰 없을 때 → HTML OG → embed 페이지 → oEmbed(토큰 없이 시도)
"""

from __future__ import annotations

import json
import logging
import os
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

# Facebook Graph API oEmbed (토큰 필요)
_GRAPH_OEMBED_URL: Final[str] = (
    "https://graph.facebook.com/v22.0/instagram_oembed"
    "?url={url}&access_token={token}"
)
# 레거시 oEmbed (토큰 없이 되던 엔드포인트, 현재 불안정)
_LEGACY_OEMBED_URL: Final[str] = "https://api.instagram.com/oembed/?url={url}"


# ── 환경변수에서 토큰 로드 ───────────────────────────────
def _get_meta_token() -> str | None:
    """환경변수 INSTAGRAM_OEMBED_TOKEN 에서 Meta 앱 토큰을 가져온다."""
    token = os.getenv("INSTAGRAM_OEMBED_TOKEN")
    if token:
        logger.info("Meta oEmbed 토큰 감지됨")
    return token


# ── 헬퍼 ─────────────────────────────────────────────────
def _is_instagram(url: str) -> bool:
    return bool(_INSTA_PATTERN.search(url))


def _get_shortcode(url: str) -> str | None:
    m = _INSTA_PATTERN.search(url)
    return m.group(1) if m else None


def _normalize_instagram_url(url: str) -> str:
    m = _INSTA_PATTERN.search(url)
    if m:
        return f"https://www.instagram.com/p/{m.group(1)}/"
    return url


def _extract_og_tags(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    og: dict[str, str] = {}
    for tag in soup.find_all("meta", attrs={"property": True}):
        prop: str = tag.get("property", "")
        content: str = tag.get("content", "")
        if prop.startswith("og:") and content:
            og[prop.replace(":", "_")] = content.strip()
    title_tag = soup.find("title")
    if title_tag and title_tag.string:
        og["raw_title"] = title_tag.string.strip()
    return og


# ── 전략: oEmbed (토큰 O) ────────────────────────────────
async def _try_oembed_with_token(
    url: str, token: str, client: httpx.AsyncClient
) -> dict[str, str]:
    """Graph API oEmbed — Meta 앱 토큰으로 호출. 가장 안정적."""
    endpoint = _GRAPH_OEMBED_URL.format(
        url=quote(url, safe=""), token=token
    )
    try:
        resp = await client.get(endpoint)
        resp.raise_for_status()
        data = resp.json()
        logger.info("[oEmbed+토큰] 성공 (키: %s)", list(data.keys()))

        result: dict[str, str] = {"og_site_name": "Instagram"}
        if data.get("title"):
            result["og_description"] = data["title"]
        if data.get("author_name"):
            result["og_title"] = f"{data['author_name']}의 Instagram 게시물"
        if data.get("thumbnail_url"):
            result["og_image"] = data["thumbnail_url"]
        return result
    except httpx.HTTPStatusError as exc:
        logger.warning("[oEmbed+토큰] HTTP %s", exc.response.status_code)
    except (httpx.RequestError, json.JSONDecodeError) as exc:
        logger.warning("[oEmbed+토큰] 실패: %s", exc)
    return {}


# ── 전략: HTML OG 태그 ───────────────────────────────────
async def _try_html_og(url: str, client: httpx.AsyncClient) -> dict[str, str]:
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        og = _extract_og_tags(resp.text)
        if og.get("og_description"):
            logger.info("[HTML OG] 추출 성공")
            return og
        logger.info("[HTML OG] og:description 비어있음")
    except httpx.HTTPStatusError as exc:
        logger.info("[HTML OG] HTTP %s", exc.response.status_code)
    except httpx.RequestError as exc:
        logger.info("[HTML OG] 네트워크 오류: %s", exc)
    return {}


# ── 전략: embed 페이지 ───────────────────────────────────
async def _try_embed_page(url: str, client: httpx.AsyncClient) -> dict[str, str]:
    shortcode = _get_shortcode(url)
    if not shortcode:
        return {}

    embed_url = f"https://www.instagram.com/p/{shortcode}/embed/captioned/"
    try:
        resp = await client.get(embed_url)
        resp.raise_for_status()
        html = resp.text
        soup = BeautifulSoup(html, "html.parser")

        caption = ""

        # A: 캡션 div
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

        # B: JSON-LD
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

        # C: embed 페이지의 og 태그
        if not caption:
            caption = _extract_og_tags(html).get("og_description", "")

        if caption:
            logger.info("[embed] 캡션 추출 성공 (%d자)", len(caption))
            return {"og_description": caption, "og_site_name": "Instagram"}

        logger.info("[embed] 캡션 없음")
    except httpx.HTTPStatusError as exc:
        logger.info("[embed] HTTP %s", exc.response.status_code)
    except httpx.RequestError as exc:
        logger.info("[embed] 네트워크 오류: %s", exc)
    return {}


# ── 전략: oEmbed (토큰 X, 레거시) ────────────────────────
async def _try_oembed_legacy(url: str, client: httpx.AsyncClient) -> dict[str, str]:
    endpoint = _LEGACY_OEMBED_URL.format(url=quote(url, safe=""))
    try:
        resp = await client.get(endpoint)
        resp.raise_for_status()
        data = resp.json()
        logger.info("[oEmbed 레거시] 성공")
        result: dict[str, str] = {"og_site_name": "Instagram"}
        if data.get("title"):
            result["og_description"] = data["title"]
        if data.get("author_name"):
            result["og_title"] = f"{data['author_name']}의 Instagram 게시물"
        if data.get("thumbnail_url"):
            result["og_image"] = data["thumbnail_url"]
        return result
    except (httpx.HTTPStatusError, httpx.RequestError, json.JSONDecodeError) as exc:
        logger.warning("[oEmbed 레거시] 실패: %s", exc)
    return {}


# ── 공개 API ─────────────────────────────────────────────
async def crawl_meta(
    url: str, *, client: httpx.AsyncClient | None = None
) -> CrawlResult:
    """주어진 URL의 OG 메타 태그를 크롤링하여 CrawlResult로 반환.

    Instagram의 경우:
      - INSTAGRAM_OEMBED_TOKEN 환경변수가 있으면 oEmbed API 우선
      - 없으면 HTML OG → embed → 레거시 oEmbed 순으로 fallback
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
        og: dict[str, str] = {}
        is_insta = _is_instagram(normalized)

        # 전략 1: HTML OG 태그
        og = await _try_html_og(normalized, client)

        # 전략 2: Instagram embed 페이지
        if not og.get("og_description") and is_insta:
            og = await _try_embed_page(normalized, client)

        # 전략 3: oEmbed API (토큰 필요)
        if not og.get("og_description") and is_insta:
            token = _get_meta_token()
            if token:
                og = await _try_oembed_with_token(normalized, token, client)
            else:
                logger.warning(
                    "INSTAGRAM_OEMBED_TOKEN 환경변수 미설정 → oEmbed 전략 건너뜀. "
                    "Meta 앱 토큰을 발급받아 설정하세요."
                )

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