"""이벤트 정규화 (결정론). 메시지에서 바뀌는 값(숫자·16진수·따옴표 안 문자열)을 자리표시로 바꿔
같은 종류의 이벤트를 같은 event_type으로 묶는다. 뜻을 붙이지 않는다(§13-11).
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from app.models.records import EventRule

MAX_EVENT_TYPE_LEN = 160

_QUOTED = re.compile(r"(\"[^\"]*\"|'[^']*')")
_HEX = re.compile(r"\b0[xX][0-9a-fA-F]+\b")
_UUIDISH = re.compile(r"\b[0-9a-fA-F]{8,}\b")
_NUMBER = re.compile(r"(?<![A-Za-z_])[-+]?\d+(?:[.,:]\d+)*")
_SPACES = re.compile(r"\s+")


def message_template(message: str) -> str:
    first_line = message.split("\n", 1)[0]
    text = _QUOTED.sub("<S>", first_line)
    text = _HEX.sub("<HEX>", text)
    text = _UUIDISH.sub("<HEX>", text)
    text = _NUMBER.sub("<N>", text)
    return _SPACES.sub(" ", text).strip()


class EventTyper:
    """사용자 규칙(이름 붙인 이벤트)이 먼저, 없으면 '구성요소|메시지 틀'."""

    def __init__(self, rules: Sequence[EventRule] = ()) -> None:
        self._rules = [(rule.name, re.compile(rule.regex)) for rule in rules]

    def event_type(self, message: str, component: str | None) -> str:
        for name, pattern in self._rules:
            if pattern.search(message):
                return name[:MAX_EVENT_TYPE_LEN]
        template = message_template(message) or "<빈 메시지>"
        key = f"{component}|{template}" if component else template
        return key[:MAX_EVENT_TYPE_LEN]
