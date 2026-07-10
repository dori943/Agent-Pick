"""LLM이 Function Calling으로 선택한 actions를 실제 딥링크 문자열로 실행.

기존에는 category별 if/elif로 딥링크 종류를 코드가 미리 정해뒀지만,
지금은 llm_analyzer.py에서 LLM이 직접 어떤 함수를 어떤 인자로 호출할지
고른 AnalysisResult.actions를 그대로 실행만 한다.

새 액션을 추가하려면:
  1) llm_analyzer.py에 함수 선언(FunctionDeclaration) 추가
  2) 아래 _ACTION_BUILDERS에 "함수명" -> (DeeplinkResult 필드명, 빌더) 매핑 추가
이 두 곳 외에는 코드를 건드릴 필요가 없다.
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


def _build_memo_link(args: dict) -> str | None:
    text = args.get("text")
    if not text:
        return None
    return f"mobilenotes://new?text={quote(text)}"


# 함수명(LLM이 호출한 이름) -> (DeeplinkResult 필드명, 빌더 함수)
_ACTION_BUILDERS: dict[str, tuple[str, Callable[[dict], str | None]]] = {
    "create_map_deeplink": ("map_deeplink", _build_map_link),
    "create_calendar_deeplink": ("calendar_deeplink", _build_calendar_link),
    "create_memo_deeplink": ("memo_deeplink", _build_memo_link),
}


def _execute_action(action: ActionCall) -> tuple[str, str] | None:
    """ActionCall 하나를 실제로 실행해 (필드명, 딥링크) 튜플로 반환.

    등록되지 않은 함수명이거나 필수 인자가 없으면 None."""
    entry = _ACTION_BUILDERS.get(action.action)
    if entry is None:
        logger.warning("알 수 없는 액션 무시: %s", action.action)
        return None

    field_name, builder = entry
    try:
        link = builder(action.args)
    except Exception as e:
        logger.warning("액션 실행 실패 (%s, args=%s): %s", action.action, action.args, e)
        return None

    if not link:
        logger.warning("액션 %s 실행 결과 없음 (필수 인자 누락): %s", action.action, action.args)
        return None

    return field_name, link


async def generate_deeplinks(analysis_result: AnalysisResult) -> DeeplinkResult:
    """AnalysisResult.actions(LLM이 선택한 함수 호출들)를 실제 딥링크로 실행.

    category를 다시 들여다보지 않는다 — LLM이 이미 필요한 액션을
    골라뒀으므로, 여기서는 그 결과를 그대로 실행하기만 한다.
    """
    result: dict[str, str | None] = {
        "map_deeplink": None,
        "calendar_deeplink": None,
        "memo_deeplink": None,
    }

    for action in analysis_result.actions:
        executed = _execute_action(action)
        if executed is None:
            continue
        field_name, link = executed
        result[field_name] = link

    return DeeplinkResult(**result)