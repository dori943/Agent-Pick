"""SNS 정보 아카이빙 에이전트 — FastAPI 서버 오케스트레이션.

단축어(Shortcut) → URL 수신 → 크롤링 → LLM 분석 → 딥링크 생성 → 응답 반환
각 기능은 독립적인 클래스로 모듈화되어 관리된다.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from dotenv import load_dotenv
load_dotenv() 

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.models import ArchiveRequest, ArchiveResponse
from app.crawler import crawl_meta
# 클래스 구조로 바뀐 분석기 및 딥링크 서비스 임포트
from app.LLM_analyzer import LLMAnalyzer
from app.deeplink import DeeplinkService

# ── 로깅 및 상태 관리 ─────────────────────────────────────
import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s │ %(levelname)-7s │ %(name)s │ %(message)s")
logger = logging.getLogger(__name__)

# ── Lifespan: 클라이언트 및 서비스 인스턴스 관리 ──────────
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.http_client = httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0..."}, # 기존 헤더 유지
        timeout=httpx.Timeout(10.0),
        follow_redirects=True,
    )
    # 분석기 및 딥링크 서비스 인스턴스 생성
    app.state.analyzer = LLMAnalyzer()
    app.state.deeplink_service = DeeplinkService()
    
    logger.info("서버 초기화 완료: 클라이언트 풀 및 분석 서비스 준비됨")
    yield
    await app.state.http_client.aclose()

# ── FastAPI 인스턴스 ─────────────────────────────────────
app = FastAPI(title="SNS 정보 아카이빙 에이전트", lifespan=lifespan)

# ── 엔드포인트 ──────────────────────────────────────────
@app.post("/archive", response_model=ArchiveResponse)
async def archive(req: ArchiveRequest, request: Request) -> ArchiveResponse:
    t0 = time.perf_counter()
    url = str(req.url)
    logger.info("▶ 아카이빙 요청: %s", url)

    # 1. 크롤링
    client: httpx.AsyncClient = request.app.state.http_client
    crawl_result = await crawl_meta(url, client=client)

    if not crawl_result.og_description:
        raise HTTPException(status_code=422, detail="og:description 추출 실패")

    # 2. LLM 분석 (클래스 인스턴스 사용)
    analyzer: LLMAnalyzer = request.app.state.analyzer
    analysis_result = await analyzer.analyze(crawl_result)

    # 3. 딥링크 생성 (클래스 인스턴스 사용)
    deeplink_service: DeeplinkService = request.app.state.deeplink_service
    deeplink_result = await deeplink_service.generate_deeplinks(analysis_result)

    elapsed = time.perf_counter() - t0
    logger.info("✔ 파이프라인 완료 (%.2fs)", elapsed)

    return ArchiveResponse(
        success=True,
        crawl=crawl_result,
        analysis=analysis_result,
        deeplinks=deeplink_result,
    )