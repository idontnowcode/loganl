"""긴 작업의 취소 신호 (계획서 §8.5 "취소 가능한 분석 작업"). Qt를 쓰지 않는다."""

from __future__ import annotations

import threading


class Cancelled(Exception):
    """사용자가 작업을 취소함."""


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise Cancelled("작업을 취소했습니다.")


NEVER_CANCEL = CancelToken()
