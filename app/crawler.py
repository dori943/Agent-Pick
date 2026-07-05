"""로그인 프리 메타 태그 크롤러.

인스타그램 등 SNS 공개 게시물의 og:description 메타 태그를
별도 로그인 없이 추출한다.

전략 (Instagram) — 3단계 fallback:
  1차) 쿠키 기반 HTML OG — 인스타 홈페이지에서 세션 쿠키를 받은 뒤 게시물 요청
  2차) /embed/captioned/ 페이지 파싱
  3차) Graph API oEmbed (META_ACCESS_TOKEN 필요)
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

# Graph API oEmbed — access_token은 "앱ID|클라이언트토큰" 형식
_GRAPH_OEMBED_URL: Final[str] = (
    "https://graph.facebook.com/v22.0/instagram_oembed"
    "?url={url}&access_token={token}"
)


# ── 환경변수 ─────────────────────────────────────────────
def _get_meta_token() -> str | None:
    """META_ACCESS_TOKEN 환경변수를 가져온다.

    Graph API oEmbed는 앱 액세스 토큰 형식 "앱ID|클라이언트토큰"을 요구.
    .env에 META_APP_ID가 있으면 자동으로 조합한다.
    """
    token = os.getenv("META_ACCESS_TOKEN", "")
    app_id = os.getenv("META_APP_ID", "")

    if not token:
        return None

    # 이미 "앱ID|토큰" 형식이면 그대로 사용
    if "|" in token:
        logger.info("Meta 앱 액세스 토큰 감지됨 (파이프 형식)")
        return token

    # META_APP_ID가 있으면 "앱ID|클라이언트토큰" 조합
    if app_id:
        combined = f"{app_id}|{token}"
        logger.info("Meta 토큰 조합: APP_ID|CLIENT_TOKEN")
        return combined

    # 클라이언트 토큰 단독 — 그래도 시도는 해봄
    logger.info("Meta 클라이언트 토큰 단독 사용 (400 에러 시 META_APP_ID 설정 필요)")
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


# ── 전략 1: 쿠키 기반 HTML OG ────────────────────────────
async def _try_html_og_with_cookies(
    url: str, client: httpx.AsyncClient
) -> dict[str, str]:
    """인스타그램 홈에 먼저 접속해서 세션 쿠키를 받고,
    그 쿠키를 포함해 게시물 페이지를 요청한다.
    브라우저가 OG 태그를 받을 수 있는 이유가 바로 이 쿠키 때문.
    """
    # 쿠키를 자동으로 저장/전송할 전용 클라이언트
    cookie_client = httpx.AsyncClient(
        headers=_HEADERS,
        timeout=httpx.Timeout(_TIMEOUT),
        follow_redirects=True,
        max_redirects=_MAX_REDIRECTS,
        cookies=httpx.Cookies(),
    )
    try:
        # Step 1: 인스타 홈에 접속 → 세션 쿠키 수신
        logger.info("[전략1] 인스타그램 홈 접속 → 쿠키 수집")
        await cookie_client.get("https://www.instagram.com/")

        # Step 2: 쿠키가 포함된 상태로 게시물 페이지 요청
        resp = await cookie_client.get(url)
        resp.raise_for_status()

        og = _extract_og_tags(resp.text)
        if og.get("og_description"):
            logger.info("[전략1] 쿠키 기반 HTML OG 추출 성공")
            return og

        logger.info("[전략1] 쿠키 있어도 og:description 비어있음")
    except httpx.HTTPStatusError as exc:
        logger.info("[전략1] HTTP %s", exc.response.status_code)
    except httpx.RequestError as exc:
        logger.info("[전략1] 네트워크 오류: %s", exc)
    finally:
        await cookie_client.aclose()
    return {}


# ── 전략 2: embed 페이지 ─────────────────────────────────
async def _try_embed_page(url: str, client: httpx.AsyncClient) -> dict[str, str]:
    """Instagram /embed/captioned/ 페이지는 서버사이드 렌더링이라
    캡션이 HTML에 직접 포함되어 있을 수 있다.
    """
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

        # A: 캡션 div (클래스명은 인스타가 자주 바꿈)
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

        # C: embed 페이지에도 og 태그가 있을 수 있음
        if not caption:
            caption = _extract_og_tags(html).get("og_description", "")

        if caption:
            logger.info("[전략2] embed 캡션 추출 성공 (%d자)", len(caption))
            return {"og_description": caption, "og_site_name": "Instagram"}

        logger.info("[전략2] embed 캡션 없음")
    except httpx.HTTPStatusError as exc:
        logger.info("[전략2] HTTP %s", exc.response.status_code)
    except httpx.RequestError as exc:
        logger.info("[전략2] 네트워크 오류: %s", exc)
    return {}


# ── 전략 3: oEmbed API (토큰) ────────────────────────────
async def _try_oembed_with_token(
    url: str, token: str, client: httpx.AsyncClient
) -> dict[str, str]:
    """Graph API oEmbed — Meta 앱 토큰으로 호출."""
    endpoint = _GRAPH_OEMBED_URL.format(
        url=quote(url, safe=""), token=token
    )
    try:
        resp = await client.get(endpoint)
        resp.raise_for_status()
        data = resp.json()
        logger.info("[전략3] oEmbed 성공 (키: %s)", list(data.keys()))

        result: dict[str, str] = {"og_site_name": "Instagram"}
        if data.get("title"):
            result["og_description"] = data["title"]
        if data.get("author_name"):
            result["og_title"] = f"{data['author_name']}의 Instagram 게시물"
        if data.get("thumbnail_url"):
            result["og_image"] = data["thumbnail_url"]
        return result
    except httpx.HTTPStatusError as exc:
        # 에러 응답 본문을 로깅해서 원인 파악
        body = exc.response.text[:300]
        logger.warning("[전략3] HTTP %s — %s", exc.response.status_code, body)
    except (httpx.RequestError, json.JSONDecodeError) as exc:
        logger.warning("[전략3] 실패: %s", exc)
    return {}


# ── 공개 API ─────────────────────────────────────────────
async def crawl_meta(
    url: str, *, client: httpx.AsyncClient | None = None
) -> CrawlResult:
    """주어진 URL의 OG 메타 태그를 크롤링하여 CrawlResult로 반환.

    Instagram 전략: 쿠키 HTML OG → embed 페이지 → oEmbed(토큰)
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

        # 전략 1: 쿠키 기반 HTML OG
        og = await _try_html_og_with_cookies(normalized, client)

        # 전략 2: embed 페이지
        if not og.get("og_description") and is_insta:
            og = await _try_embed_page(normalized, client)

        # 전략 3: oEmbed API (토큰)
        if not og.get("og_description") and is_insta:
            token = _get_meta_token()
            if token:
                og = await _try_oembed_with_token(normalized, token, client)
            else:
                logger.warning(
                    "META_ACCESS_TOKEN 환경변수 미설정 → oEmbed 전략 건너뜀"
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