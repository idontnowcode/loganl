"""백그라운드 작업 (계획서 §8.5 "긴 분석 중 UI 응답 유지", "진행률", "취소").

일반 함수를 QThreadPool에서 돌리고 결과·진행률을 신호로 UI 스레드에 돌려준다.
작업 함수는 Qt를 몰라야 한다. 취소는 app.core.cancel.CancelToken으로 한다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from app.core.cancel import Cancelled

logger = logging.getLogger("app.ui.workers")


class WorkerSignals(QObject):
    finished = Signal(object)
    failed = Signal(str)
    cancelled = Signal()
    progress = Signal(int, int, str)


class FunctionWorker(QRunnable):
    """`fn(*args, **kwargs)`를 다른 스레드에서 실행한다.

    with_progress=True면 `progress=콜백(done, total, message)`을 키워드 인자로 넣어 준다.
    PySide6에서는 실행이 끝난 QRunnable이 지워질 수 있으므로, 호출하는 쪽이
    `worker.signals`의 참조를 신호가 도착할 때까지 들고 있어야 한다(TaskRunner가 대신 한다).
    """

    def __init__(self, fn: Callable[..., Any], *args: Any, with_progress: bool = False, **kwargs: Any) -> None:
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self.signals = WorkerSignals()
        if with_progress:
            self._kwargs["progress"] = self._emit_progress

    def _emit_progress(self, done: int, total: int, message: str) -> None:
        self.signals.progress.emit(done, total, message)

    @Slot()
    def run(self) -> None:
        try:
            result = self._fn(*self._args, **self._kwargs)
        except Cancelled:
            self.signals.cancelled.emit()
        except Exception as exc:  # 작업 실패는 화면에 알리고 앱은 계속 돈다
            logger.exception("백그라운드 작업 실패")
            self.signals.failed.emit(str(exc) or exc.__class__.__name__)
        else:
            self.signals.finished.emit(result)


class TaskRunner(QObject):
    """위젯마다 하나. 작업 신호 참조를 들고 있다가 끝나면 놓는다."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.pool = QThreadPool(self)
        self._pending: set[WorkerSignals] = set()

    def start(
        self,
        fn: Callable[..., Any],
        *args: Any,
        on_done: Callable[[Any], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_cancel: Callable[[], None] | None = None,
        on_progress: Callable[[int, int, str], None] | None = None,
        **kwargs: Any,
    ) -> WorkerSignals:
        worker = FunctionWorker(fn, *args, with_progress=on_progress is not None, **kwargs)
        signals = worker.signals
        self._pending.add(signals)

        def release(*_: Any) -> None:
            self._pending.discard(signals)

        if on_progress:
            signals.progress.connect(on_progress)
        signals.finished.connect(lambda result: (release(), on_done and on_done(result)))
        signals.failed.connect(lambda message: (release(), on_error and on_error(message)))
        signals.cancelled.connect(lambda: (release(), on_cancel and on_cancel()))
        self.pool.start(worker)
        return signals

    @property
    def busy(self) -> bool:
        return bool(self._pending)

    def wait(self, timeout_ms: int = 10_000) -> bool:
        return self.pool.waitForDone(timeout_ms)
