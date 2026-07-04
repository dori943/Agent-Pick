# SNS 정보 아카이빙 에이전트

어떤 기능의 에이전트인지 설명.

## 폴더 구조
```
app/
  main.py          # 서버 오케스트레이션 (엔드포인트, lifespan, 예외처리)
  crawler.py        # 로그인 프리 메타 태그 크롤러
  llm_analyzer.py    # (타 담당) LLM 분석 모듈 - 파이프라인용 임시 스텁 포함
  deeplink.py        # (타 담당) 딥링크 생성 모듈 - 파이프라인용 임시 스텁 포함
  models.py          # 공용 Pydantic 스키마
```