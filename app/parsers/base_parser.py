"""파서 프로토콜과 원문 파서.

원문 파서는 어떤 프로파일도 맞지 않을 때 쓴다: 빈 줄이 아닌 한 줄 = 이벤트 하나, 타임스탬프 없음.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from typing import Protocol, runtime_checkable

from app.core.cancel import NEVER_CANCEL, CancelToken
from app.core.event_normalizer import EventTyper
from app.models.enums import EventTimeKind, TimestampBasis
from app.models.records import ParsedEvent, RawLine

CANCEL_CHECK_EVERY = 5000


@runtime_checkable
class LogParser(Protocol):
    name: str

    @property
    def timestamp_basis(self) -> TimestampBasis: ...

    def sniff(self, head_lines: Sequence[str]) -> float:
        """0~1. 이 파서가 맞을 가능성. 결정론 규칙으로만 판단한다."""
        ...

    def parse(self, lines: Iterable[RawLine], cancel: CancelToken = NEVER_CANCEL) -> Iterator[ParsedEvent]: ...


class RawTextParser:
    name = "raw"

    def __init__(self) -> None:
        self._typer = EventTyper()

    @property
    def timestamp_basis(self) -> TimestampBasis:
        # 타임스탬프가 있는데 형식을 몰라서 못 읽었을 수도 있으므로 NONE이 아니라 UNKNOWN이다.
        return TimestampBasis.UNKNOWN

    def sniff(self, head_lines: Sequence[str]) -> float:
        return 0.01

    def parse(self, lines: Iterable[RawLine], cancel: CancelToken = NEVER_CANCEL) -> Iterator[ParsedEvent]:
        seq = 0
        for index, line in enumerate(lines):
            if index % CANCEL_CHECK_EVERY == 0:
                cancel.raise_if_cancelled()
            if not line.text.strip():
                continue
            yield ParsedEvent(
                seq=seq,
                line_no=line.line_no,
                byte_offset=line.byte_offset,
                ts_kind=EventTimeKind.NONE,
                event_type=self._typer.event_type(line.text, None),
                message=line.text,
                raw_text=line.text,
            )
            seq += 1
