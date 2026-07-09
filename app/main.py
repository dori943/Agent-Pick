"""SNS 정보 아카이빙 에이전트 — FastAPI 서버 오케스트레이션.

단축어(Shortcut) → URL 수신 → 크롤링 → LLM 분석 → 딥링크 생성 → 응답 반환
전 구간을 비동기로 처리한다.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from dotenv import load_dotenv
load_dotenv()  # .env 파일에서 환경변수 로드

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse # OK.로그인창 화면 전환용

from app.models import ArchiveRequest, ArchiveResponse
from app.crawler import crawl_meta
from app.LLM_analyzer import analyze
from app.deeplink import generate_deeplinks
from app.database import NotionDatabaseSaver # OK.노션적재클래스

# ── 로깅 ─────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(name)s │ %(message)s",
)
logger = logging.getLogger(__name__)

CLIENT_ID = "insert_client_id" #OK
CLIENT_SECRET = "insert_client_secret" #OK
REDIRECT_URI = "http://localhost:8000/callback" #OK.앱의 키값들


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

# OK. ── [노션 파트] 1. 사용자를 노션의 로그인 및 템플릿 선택 창으로 튕겨주는 주소입니다 ──
@app.get("/login")
async def login_notion() -> RedirectResponse:
    """사용자가 접속하면 노션 OAuth 인증 페이지로 자동 리다이렉트합니다."""
    # ◀ [노션 파트] 노션 표준 규격에 맞게 쿼리 매개변수들을 결합하여 로그인 링크를 생성합니다.
    notion_auth_url = (
        f"https://api.notion.com/v1/oauth/authorize?"
        f"client_id={CLIENT_ID}&"
        f"redirect_uri={REDIRECT_URI}&"
        f"response_type=code&"
        f"owner=user"
    )
    logger.info("사용자를 노션 OAuth 로그인 화면으로 이동시킵니다.")
    # ◀ [노션 파트] 생성된 링크 주소로 사용자의 브라우저 화면을 강제 전환시킵니다.
    return RedirectResponse(url=notion_auth_url)


# ── [노션 파트] 2. 사용자가 노션 승인 완료 후 임시 비밀번호(code)를 들고 돌아오는 주소입니다 ──
@app.get("/callback")
async def notion_callback(code: str = None, error: str = None) -> dict[str, str]:
    """노션이 던져준 임시 code를 낚아채서 사용자의 진짜 Access Token과 교환합니다."""
    # ◀ [노션 파트] 만약 사용자가 연동을 거부하거나 취소하여 에러 코드가 넘어온 경우의 처리입니다.
    if error:
        logger.error("사용자가 노션 연동을 거부했습니다: %s", error)
        raise HTTPException(status_code=400, detail=f"Notion login denied: {error}")
    # ◀ [노션 파特] 임시 code 값이 주소창에 아예 존재하지 않을 때 예외를 발생시킵니다.
    if not code:
        logger.error("주소창에 authorization code가 존재하지 않습니다.")
        raise HTTPException(status_code=400, detail="Missing authorization code.")

    logger.info("임시 code를 사용자의 진짜 영구 액세스 토큰으로 교환 요청 중...")
    # ◀ [노션 파트] 비동기 HTTP 클라이언트를 열어 노션 서버에 토큰 교환 API 전송을 준비합니다.
    async with httpx.AsyncClient() as client:
        # ◀ [노션 파트] 내 앱의 Client ID, Secret과 임시 code를 모아 진짜 열쇠를 요청합니다.
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
        
    # ◀ [노션 파트] 노션 서버가 반환해준 응답 바디를 json 딕셔너리로 변환합니다.
    token_data = response.json()
    # ◀ [노션 파트] 통신 결과가 200 성공이 아닐 경우, 에러 내용과 실패 페이로드를 반환합니다.
    if response.status_code != 200:
        logger.error("노션 OAuth 토큰 교환 실패")
        return {"error": "Failed to exchange token", "details": str(token_data)}

    # ◀ [노션 파트] 로그인한 개별 유저만의 고유한 진짜 액세스 토큰을 추출합니다.
    user_access_token = token_data.get("access_token")
    # ◀ [노션 파트] 유저가 내 워크스페이스로 복사해간 새 템플릿 데이터베이스의 ID를 추출합니다.
    user_database_id = token_data.get("duplicated_template_id")

    logger.info("노션 로그인 및 데이터베이스 연동이 성공적으로 완료되었습니다!")
    # ◀ [노션 파트] 토큰 정보를 화면에 띄워줍니다. (실제 서비스 시에는 이를 통합 서버 DB에 저장해야 합니다.)
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
    analysis_result = await analyze(crawl_result)

    # ③ 딥링크 생성
    deeplink_result = await generate_deeplinks(analysis_result)
    
    # ④ [노션 파트] 추출된 모든 데이터를 로그인 유저의 노션 DB에 동적으로 최종 적재
    # ◀ [노션 파트] 단축어 요청 바디(req)나 유저 세션 DB에서 해당 사용자의 토큰 값을 동적으로 가져옵니다.
    user_token = getattr(req, "notion_token", None) or "insert_user_token"
    # ◀ [노션 파트] 유저가 로그인할 때 복사해갔던 유저만의 고유 노션 데이터베이스 ID 값을 가져옵니다.
    user_database_id = getattr(req, "database_id", None) or "insert_database_id"

    logger.info("▶ 노션 데이터베이스 최종 적재 시작 (Target DB ID: %s)", user_database_id)
    
    # ◀ [노션 파트] 받아온 사용자의 동적 자격증명으로 노션 적재기 클래스를 초기화(인스턴스화)합니다.
    saver = NotionDatabaseSaver(notion_token=user_token, database_id=user_database_id)
    # ◀ [노션 파트] 크롤링, LLM 분석, 딥링크 모듈 객체들을 딕셔너리로 변환하여 적재 함수를 가동합니다.
    notion_success = saver.save_archive(
        crawl_data=crawl_result.model_dump(),
        analysis_data=analysis_result.model_dump(),
        deeplink_data=deeplink_result.model_dump() if deeplink_result else {}
    )

    # ◀ [노션 파트] 노션 적재 함수가 에러를 뱉어 False를 반환했을 경우, 에러 로그를 기록합니다.
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