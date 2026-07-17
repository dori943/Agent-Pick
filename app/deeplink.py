"""LLM이 Function Calling으로 선택한 actions를 실제 딥링크 문자열로 실행.

기존에는 category별 if/elif로 딥링크 종류를 코드가 미리 정해뒀지만,
지금은 llm_analyzer.py에서 LLM이 직접 어떤 함수를 어떤 인자로 호출할지
고른 AnalysisResult.actions를 그대로 실행만 한다.

새 액션을 추가하려면:
  1) llm_analyzer.py에 함수 선언(FunctionDeclaration) 추가
  2) 아래 _ACTION_BUILDERS에 "함수명" -> (DeeplinkResult 필드명, 빌더) 매핑 추가
이 두 곳 외에는 코드를 건드릴 필요가 없다.

주의: 모든 액션이 URL을 만드는 건 아니다. 메모는 URL이 아니라 본문
텍스트를 그대로 내려준다 (_build_memo_payload의 주석 참고).
"""
from __future__ import annotations

import logging
from typing import Callable
from urllib.parse import quote

from app.models import ActionCall, AnalysisResult, DeeplinkResult
from app.naver_map import build_nmap_search_deeplink

logger = logging.getLogger(__name__)


def _build_map_link(args: dict) -> str | None:
    query = args.get("query")
    if not query:
        return None
    return build_nmap_search_deeplink(query)


def _build_calendar_link(args: dict) -> str | None:
    event_title = args.get("event_title")
    if not event_title:
        return None
    return f"calshow://?title={quote(event_title)}"


def _build_memo_payload(args: dict) -> str | None:
    """메모는 딥링크를 만들지 않고 본문 텍스트를 그대로 반환한다.

    Apple 메모에는 노트를 만들고 본문까지 채우는 공개 URL 스킴이 없다.
    문서화된 액션은 mobilenotes://showNote?identifier= (기존 노트 열기)뿐이고,
    mobilenotes://new?text= 는 비공개 스킴이라 노트를 저장하지 않는다
    (실기기 확인: 메모 목록에 아무것도 남지 않음).

    대신 클라이언트인 단축어에 네이티브 '메모 생성' 액션이 있고, 이건
    온디바이스에서 동작하므로 URL 길이 제한도 퍼센트 인코딩도 개행 제한도
    없다. 그래서 서버는 URL 대신 llm_analyzer._build_memo_text가 조립한
    '제목\\n\\n본문' 문자열을 그대로 내려주고, 단축어가 그걸 본문 칸에 꽂는다.
    """
    text = (args.get("text") or "").strip()
    if not text:
        return None
    logger.info("메모 본문 페이로드: %d자 / %d줄", len(text), text.count("\n") + 1)
    return text


# 함수명(LLM이 호출한 이름) -> (DeeplinkResult 필드명, 빌더 함수)
_ACTION_BUILDERS: dict[str, tuple[str, Callable[[dict], str | None]]] = {
    "create_map_deeplink": ("map_deeplink", _build_map_link),
    "create_calendar_deeplink": ("calendar_deeplink", _build_calendar_link),
    "create_memo_deeplink": ("memo_text", _build_memo_payload),
}


def _execute_action(action: ActionCall) -> tuple[str, str] | None:
    """ActionCall 하나를 실제로 실행해 (필드명, 값) 튜플로 반환.

    등록되지 않은 함수명이거나 필수 인자가 없으면 None."""
    entry = _ACTION_BUILDERS.get(action.action)
    if entry is None:
        logger.warning("알 수 없는 액션 무시: %s", action.action)
        return None

    field_name, builder = entry
    try:
        value = builder(action.args)
    except Exception as e:
        logger.warning("액션 실행 실패 (%s, args=%s): %s", action.action, action.args, e)
        return None

    if not value:
        logger.warning("액션 %s 실행 결과 없음 (필수 인자 누락): %s", action.action, action.args)
        return None

    return field_name, value


async def generate_deeplinks(analysis_result: AnalysisResult) -> DeeplinkResult:
    """AnalysisResult.actions(LLM이 선택한 함수 호출들)를 실제 값으로 실행.

    category를 다시 들여다보지 않는다 — LLM이 이미 필요한 액션을
    골라뒀으므로, 여기서는 그 결과를 그대로 실행하기만 한다.
    """
    result: dict[str, str | None] = {
        "map_deeplink": None,
        "calendar_deeplink": None,
        "memo_text": None,
    }

    for action in analysis_result.actions:
        executed = _execute_action(action)
        if executed is None:
            continue
        field_name, value = executed
        result[field_name] = value

    return DeeplinkResult(**result)