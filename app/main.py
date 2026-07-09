"""SNS 정보 아카이빙 에이전트 — FastAPI 서버 오케스트레이션.

단축어(Shortcut) → URL 수신 → 크롤링 → LLM 분석 → 딥링크 생성 → 응답 반환
전 구간을 비동기로 처리한다.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from dotenv import load_dotenv
load_dotenv()  # .env 파일에서 환경변수 로드

import base64
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.models import ArchiveRequest, ArchiveResponse
from app.crawler import crawl_meta, normalize_url
from app.llm_analyzer import analyze
from app.deeplink import generate_deeplinks, DeeplinkResult
from app.database import NotionDatabaseSaver

NOTION_CLIENT_ID = os.environ.get("NOTION_CLIENT_ID")
NOTION_CLIENT_SECRET = os.environ.get("NOTION_CLIENT_SECRET")
NOTION_REDIRECT_URI = os.environ.get("NOTION_REDIRECT_URI")


# ── 로깅 ─────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(name)s │ %(message)s",
)
logger = logging.getLogger(__name__)


# ── Lifespan: httpx 클라이언트 풀 관리 ───────────────────
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """앱 시작 시 httpx AsyncClient를 생성하고 종료 시 정리.

    crawler 가 매 요청마다 커넥션을 새로 여는 오버헤드를 없앤다.
    """
    app.state.http_client = httpx.AsyncClient(
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        },
        timeout=httpx.Timeout(10.0),
        follow_redirects=True,
        max_redirects=5,
    )
    logger.info("httpx 커넥션 풀 초기화 완료")

    # 개인용(단일 사용자) 구성: .env 의 NOTION_TOKEN / NOTION_DATABASE_ID 를 그대로 사용.
    # 여러 사용자로 확장할 때는 이 부분을 사용자별 토큰 저장소 조회로 교체할 것.
    notion_token = os.environ.get("NOTION_TOKEN")
    notion_database_id = os.environ.get("NOTION_DATABASE_ID")

    if not notion_token or not notion_database_id:
        logger.warning(
            "NOTION_TOKEN 또는 NOTION_DATABASE_ID가 설정되지 않았습니다. "
            "Notion 저장 기능이 비활성화된 채로 서버가 시작됩니다."
        )
        app.state.notion_saver = None
    else:
        app.state.notion_saver = NotionDatabaseSaver(
            notion_token=notion_token,
            database_id=notion_database_id,
        )
        logger.info("Notion 연동 초기화 완료 (database_id=%s)", notion_database_id)

    yield
    await app.state.http_client.aclose()
    logger.info("httpx 커넥션 풀 종료")


# ── FastAPI 인스턴스 ─────────────────────────────────────
app = FastAPI(
    title="SNS 정보 아카이빙 에이전트",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/notion/authorize")
async def notion_authorize():
    from fastapi.responses import RedirectResponse
    auth_url = (
        "https://api.notion.com/v1/oauth/authorize"
        f"?client_id={NOTION_CLIENT_ID}"
        f"&redirect_uri={NOTION_REDIRECT_URI}"
        "&response_type=code&owner=user"
    )
    return RedirectResponse(auth_url)


@app.get("/notion/callback")
async def notion_callback(code: str):
    basic = base64.b64encode(f"{NOTION_CLIENT_ID}:{NOTION_CLIENT_SECRET}".encode()).decode()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.notion.com/v1/oauth/token",
            headers={"Authorization": f"Basic {basic}", "Content-Type": "application/json"},
            json={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": NOTION_REDIRECT_URI,
            },
        )
    resp.raise_for_status()
    data = resp.json()

    # 서버는 저장하지 않고 화면에 표시만 함 (사용자가 복사해서 단축어에 저장)
    access_token = data["access_token"]
    refresh_token = data.get("refresh_token", "")
    duplicated_db_id = data.get("duplicated_template_id", "")

    from fastapi.responses import HTMLResponse
    return HTMLResponse(f"""
        <h3>연결 완료 — 아래 값을 단축어에 저장하세요</h3>
        <p><b>access_token</b><br><textarea rows="2" cols="60">{access_token}</textarea></p>
        <p><b>refresh_token</b><br><textarea rows="2" cols="60">{refresh_token}</textarea></p>
        <p><b>database_id</b><br><textarea rows="2" cols="60">{duplicated_db_id}</textarea></p>
    """)


@app.post("/notion/refresh")
async def notion_refresh(refresh_token: str):
    basic = base64.b64encode(f"{NOTION_CLIENT_ID}:{NOTION_CLIENT_SECRET}".encode()).decode()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.notion.com/v1/oauth/token",
            headers={"Authorization": f"Basic {basic}", "Content-Type": "application/json"},
            json={"grant_type": "refresh_token", "refresh_token": refresh_token},
        )
    resp.raise_for_status()
    return resp.json()  # {access_token, refresh_token, ...} 다시 클라이언트가 저장


# ── 전역 예외 처리 ───────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    logger.exception("처리되지 않은 예외 발생")
    return JSONResponse(
        status_code=500,
        content=ArchiveResponse(
            success=False,
            error=f"서버 내부 오류: {type(exc).__name__}",
        ).model_dump(),
    )


# ── 헬스 체크 ────────────────────────────────────────────
@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ── 핵심 엔드포인트 ──────────────────────────────────────
@app.post("/archive", response_model=ArchiveResponse)
async def archive(req: ArchiveRequest, request: Request) -> ArchiveResponse:
    """단축어로부터 URL을 받아 전체 아카이빙 파이프라인을 실행.

    0) 캐시 확인 — 이전에 저장한 URL이면 크롤링/분석/딥링크 생성을 건너뛴다.
    1) 크롤링  — og 메타 태그 추출
    2) LLM 분석 — 카테고리·요약·좌표 등 구조화
    3) 딥링크 생성 — 지도/캘린더/메모 앱 연동 URL
    4) Notion 저장 — 결과(성공/실패 모두)를 Notion 데이터베이스에 기록
    """
    t0 = time.perf_counter()
    url = normalize_url(str(req.url))
    logger.info("▶ 아카이빙 요청: %s", url)

    if req.notion_access_token and req.notion_database_id:
        saver = NotionDatabaseSaver(
            notion_token=req.notion_access_token,
            database_id=req.notion_database_id,
        )
    else:
        saver = request.app.state.notion_saver 

    # ⓪ 캐시 확인 — 이미 저장된 URL이면 새로 토큰을 쓰지 않고 재사용.
    if saver is not None:
        normalized_for_cache = normalize_url(url)
        cached = await asyncio.to_thread(saver.find_by_url, normalized_for_cache)
        if cached and cached.get("map_deeplink"):
            logger.info("✔ 캐시 적중: %s", url)
            return ArchiveResponse(
                success=True,
                cached=True,
                deeplinks=DeeplinkResult(
                    map_deeplink=cached["map_deeplink"],
                    calendar_deeplink=None,
                    memo_deeplink=None,
                ),
            )

    crawl_result = None
    analysis_result = None
    deeplink_result = None

    try:
        # ① 크롤링
        client: httpx.AsyncClient = request.app.state.http_client
        crawl_result = await crawl_meta(url, client=client)

        if not crawl_result.og_description:
            logger.warning("og:description 비어있음 → 파이프라인 중단")
            if saver is not None:
                await asyncio.to_thread(
                saver.save_archive,
                crawl_data={"url": crawl_result.url},
                analysis_data={"category": "other", "summary": ""},
                status="failed",
                error_message="og:description 추출 실패 (비공개 게시물이거나 지원하지 않는 형식)",
            )
            raise HTTPException(
                status_code=422,
                detail="해당 URL에서 og:description을 추출할 수 없습니다. "
                "비공개 게시물이거나 지원하지 않는 형식일 수 있습니다.",
            )

        # ② LLM 분석
        analysis_result = await analyze(crawl_result)

        # ③ 딥링크 생성
        deeplink_result = await generate_deeplinks(analysis_result)

    except HTTPException:
        raise

    except Exception as e:
        logger.exception("파이프라인 실행 중 오류")
        if saver is not None:
            await asyncio.to_thread(
                crawl_data={"url": url},
                analysis_data=(
                    analysis_result.model_dump() if analysis_result else {"category": "other", "summary": ""}
                ),
                status="failed",
                error_message=str(e),
            )
        raise HTTPException(status_code=500, detail=f"파이프라인 오류: {e}") from e

    
    # ④ Notion 저장 (성공)
    if saver is not None:
        deeplink_data = {"map_deeplink": getattr(deeplink_result, "map_deeplink", None)}

        saved = await asyncio.to_thread(
            saver.save_archive,
            crawl_data={"url": crawl_result.url},   # ← 정규화된 url로 저장 (3번과 연결)
            analysis_data=analysis_result.model_dump(),
            deeplink_data=deeplink_data,
            status="success",
        )
        if not saved:
            logger.warning("파이프라인은 성공했지만 Notion 저장에 실패했습니다: %s", url)

    elapsed = time.perf_counter() - t0
    logger.info("✔ 파이프라인 완료 (%.2fs): category=%s", elapsed, analysis_result.category)

    return ArchiveResponse(
        success=True,
        crawl=crawl_result,
        analysis=analysis_result,
        deeplinks=deeplink_result,
    )