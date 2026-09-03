"""Shared Qt worker-thread machinery: Job, JobSlot, and thread parking.

Replaces the five hand-rolled QThread loaders (date scan, timeline load,
buffer load, overview load, fleetwide search) that each re-invented result
signals, interruption polling, staleness checks, and stop routines — with
four different lifetime strategies between them.

Pattern:

    self._slot = JobSlot(self)
    ...
    self._slot.start(lambda job: do_work(args, job),
                     on_result=self._on_loaded,
                     on_progress=self._on_partial,
                     on_error=self._on_failed)

- The worker callable receives the Job; it should poll `job.interrupted()`
  between chunks and may call `job.emit_progress(payload)` for partials.
- Starting a new job retires the previous one: its signals are
  disconnected (stale results can never arrive), it is interrupted, and it
  is parked until it exits on its own. "At most one live job; later wins."
- Nothing here ever calls terminate(): killing a thread mid-network-call
  can corrupt the process. Retired workers notice the interruption once
  their current request times out.
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QObject, QThread, Signal


def park_thread_until_finished(registry: list, thread) -> None:
    """Keep `thread` alive until it finishes on its own.

    Deleting a QThread wrapper while the OS thread still runs crashes
    ("QThread destroyed while running"); the registry keeps the wrapper
    alive, and the thread deletes itself when finished.
    """
    registry.append(thread)
    thread.finished.connect(thread.deleteLater)

    def _release(t=thread, registry=registry):
        try:
            registry.remove(t)
        except ValueError:
            pass

    thread.finished.connect(_release)


class Job(QThread):
    """One background task: runs `fn(job)` off the UI thread.

    Emits `result(value)` on success, `error(message)` on an exception.
    The callable may emit intermediate payloads via `job.emit_progress`
    and should poll `job.interrupted()` between units of work.
    """

    result = Signal(object)
    error = Signal(str)
    progress = Signal(object)

    def __init__(self, fn: Callable, parent: QObject | None = None):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        try:
            value = self._fn(self)
        except Exception as exc:
            if not self.isInterruptionRequested():
                self.error.emit(str(exc))
            return
        if not self.isInterruptionRequested():
            self.result.emit(value)

    # Convenience API for worker callables
    def interrupted(self) -> bool:
        return self.isInterruptionRequested()

    def emit_progress(self, payload) -> None:
        self.progress.emit(payload)


class JobSlot(QObject):
    """Owns at most one live Job; starting a new one retires the previous.

    Retirement disconnects the old job's signals (so a stale result can
    never land), requests interruption, and parks the thread until it
    finishes on its own. This replaces per-widget stop routines,
    request-id staleness checks, and keep-alive reference hacks.
    """

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._job: Job | None = None
        self._retired: list[QThread] = []

    def start(
        self,
        fn: Callable,
        on_result: Callable | None = None,
        on_error: Callable | None = None,
        on_progress: Callable | None = None,
        on_finished: Callable | None = None,
    ) -> Job:
        self.retire()
        job = Job(fn)
        if on_result is not None:
            job.result.connect(on_result)
        if on_error is not None:
            job.error.connect(on_error)
        if on_progress is not None:
            job.progress.connect(on_progress)
        if on_finished is not None:
            # Runs on completion of THIS job only; a retired job's finished
            # is disconnected, so stale finalizers never fire.
            job.finished.connect(on_finished)
        job.finished.connect(lambda j=job: self._on_job_finished(j))
        self._job = job
        job.start()
        return job

    def is_running(self) -> bool:
        return self._job is not None

    def retire(self) -> None:
        job = self._job
        self._job = None
        if job is None:
            return
        try:
            for signal in (job.result, job.error, job.progress, job.finished):
                try:
                    signal.disconnect()
                except (RuntimeError, TypeError):
                    pass
            job.requestInterruption()
            if job.isRunning():
                park_thread_until_finished(self._retired, job)
            else:
                job.deleteLater()
        except RuntimeError:
            pass  # C++ side already gone

    def _on_job_finished(self, job: Job) -> None:
        if self._job is job:
            self._job = None
        job.deleteLater()

    def shutdown(self, wait_ms: int = 3000) -> None:
        """Interrupt everything and give it a bounded chance to exit.
        Called at app close; stragglers stay parked rather than terminated."""
        self.retire()
        for thread in list(self._retired):
            try:
                thread.wait(wait_ms)
            except RuntimeError:
                pass
