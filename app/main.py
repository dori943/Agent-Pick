"""SNS 정보 아카이빙 에이전트 — FastAPI 서버 오케스트레이션.

단축어(Shortcut) → URL 수신 → 크롤링 → LLM 분석 → 딥링크 생성 → 응답 반환
전 구간을 비동기로 처리한다.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

import os
from dotenv import load_dotenv
load_dotenv()  # .env 파일에서 환경변수 로드

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse # OK.로그인창 화면 전환용

from app.models import ArchiveRequest, ArchiveResponse
from app.crawler import crawl_meta
from app.llm_analyzer import LLMAnalyzer
from app.deeplink import generate_deeplinks
from app.database import NotionDatabaseSaver # OK.노션적재클래스

# ── 로깅 ─────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(name)s │ %(message)s",
)
logger = logging.getLogger(__name__)

CLIENT_ID = os.getenv("NOTION_CLIENT_ID")
CLIENT_SECRET = os.getenv("NOTION_CLIENT_SECRET")
REDIRECT_URI = os.getenv("NOTION_REDIRECT_URI")


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
    yield
    await app.state.http_client.aclose()
    logger.info("httpx 커넥션 풀 종료")


# ── FastAPI 인스턴스 ─────────────────────────────────────
app = FastAPI(
    title="SNS 정보 아카이빙 에이전트",
    version="0.1.0",
    lifespan=lifespan,
)


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


@app.get("/login")
async def login_notion() -> RedirectResponse:
    """사용자가 접속하면 노션 OAuth 인증 페이지로 자동 리다이렉트합니다."""
    notion_auth_url = (
        f"https://api.notion.com/v1/oauth/authorize?"
        f"client_id={CLIENT_ID}&"
        f"redirect_uri={REDIRECT_URI}&"
        f"response_type=code&"
        f"owner=user"
    )
    logger.info("사용자를 노션 OAuth 로그인 화면으로 이동시킵니다.")
    return RedirectResponse(url=notion_auth_url)



@app.get("/callback")
async def notion_callback(code: str | None = None, error: str | None = None) -> dict[str, str]:
    """노션이 던져준 임시 code를 낚아채서 사용자의 진짜 Access Token과 교환합니다."""
    
    if error:
        logger.error("사용자가 노션 연동을 거부했습니다: %s", error)
        raise HTTPException(status_code=400, detail=f"Notion login denied: {error}")
    
    if not code:
        logger.error("주소창에 authorization code가 존재하지 않습니다.")
        raise HTTPException(status_code=400, detail="Missing authorization code.")

    logger.info("임시 code를 사용자의 진짜 영구 액세스 토큰으로 교환 요청 중...")
    
    async with httpx.AsyncClient() as client:
        
        response = await client.post(
            "https://api.notion.com/v1/oauth/token",
            auth=(CLIENT_ID, CLIENT_SECRET),
            json={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI
            },
            headers={"Content-Type": "application/json"}
        )
        
    
    token_data = response.json()
    
    if response.status_code != 200:
        logger.error("노션 OAuth 토큰 교환 실패")
        return {"error": "Failed to exchange token", "details": str(token_data)}

    
    user_access_token = token_data.get("access_token")
    
    user_database_id = token_data.get("duplicated_template_id")

    logger.info("노션 로그인 및 데이터베이스 연동이 성공적으로 완료되었습니다!")
    
    return {
        "status": "Authentication Successful",
        "message": "이제 스마트폰 단축어를 사용해 서비스를 이용하실 수 있습니다!",
        "database_id": str(user_database_id),
        "access_token":str(user_access_token)
    }


# ── 핵심 엔드포인트 ──────────────────────────────────────
@app.post("/archive", response_model=ArchiveResponse)
async def archive(req: ArchiveRequest, request: Request) -> ArchiveResponse:
    """단축어로부터 URL을 받아 전체 아카이빙 파이프라인을 실행.

    1) 크롤링  — og 메타 태그 추출
    2) LLM 분석 — 카테고리·요약·좌표 등 구조화
    3) 딥링크 생성 — 지도/캘린더/메모 앱 연동 URL
    """
    t0 = time.perf_counter()
    url = str(req.url)
    logger.info("▶ 아카이빙 요청: %s", url)

    # ① 크롤링
    client: httpx.AsyncClient = request.app.state.http_client
    crawl_result = await crawl_meta(url, client=client)

    if not crawl_result.og_description:
        logger.warning("og:description 비어있음 → 파이프라인 중단")
        raise HTTPException(
            status_code=422,
            detail="해당 URL에서 og:description을 추출할 수 없습니다. "
            "비공개 게시물이거나 지원하지 않는 형식일 수 있습니다.",
        )

    # ② LLM 분석
    llm_analyzer = LLMAnalyzer()
    analysis_result = await llm_analyzer.analyze(crawl_result)

    # ③ 딥링크 생성
    deeplink_result = await generate_deeplinks(analysis_result)

    user_token = os.getenv("NOTION_TOKEN")
    user_database_id = os.getenv("NOTION_DATABASE_ID")

    if not user_token or not user_database_id:
        logger.error("노션 토큰/데이터베이스 ID 미설정 → 적재 불가")
        raise HTTPException(
            status_code=401,
            detail="노션 연동 정보가 없습니다. /login으로 먼저 노션 계정을 연동해주세요.",
        )

    logger.info("▶ 노션 데이터베이스 최종 적재 시작 (Target DB ID: %s)", user_database_id)

    try:
        saver = NotionDatabaseSaver(notion_token=user_token, database_id=user_database_id)
    except ValueError as e:
        logger.error("노션 클라이언트 초기화 실패: %s", e)
        raise HTTPException(status_code=400, detail=f"노션 연동 정보가 올바르지 않습니다: {e}")

    notion_success = saver.save_archive(
        crawl_data=crawl_result.model_dump(),
        analysis_data=analysis_result.model_dump(),
        deeplink_data=deeplink_result.model_dump() if deeplink_result else {}
    )

    if not notion_success:
        logger.error("❌ 노션 데이터베이스 데이터 보존 작업 중 크리티컬 실패 발생")

    elapsed = time.perf_counter() - t0
    logger.info("✔ 파이프라인 완료 (%.2fs): category=%s", elapsed, analysis_result.category)

    return ArchiveResponse(
        success=True,
        crawl=crawl_result,
        analysis=analysis_result,
        deeplinks=deeplink_result,
    )