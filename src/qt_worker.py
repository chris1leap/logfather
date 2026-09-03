"""Shared helpers for Qt worker-thread lifetime management.

(Seed of the qt_worker module proposed in docs/CODE_REVIEW_2026-09.md; the
Job/JobSlot abstraction will land here when the five panel loaders are
unified.)
"""
from __future__ import annotations


def park_thread_until_finished(registry: list, thread) -> None:
    """Keep `thread` alive until it finishes on its own.

    Used when a worker did not stop within its wait deadline. terminate()
    mid-network-call can corrupt the process, and deleting a QThread wrapper
    while the OS thread still runs crashes ("QThread destroyed while
    running"). Our workers poll isInterruptionRequested() and exit once
    their current request times out; until then the registry keeps the
    wrapper alive, and the thread deletes itself when finished.
    """
    registry.append(thread)
    thread.finished.connect(thread.deleteLater)

    def _release(t=thread, registry=registry):
        try:
            registry.remove(t)
        except ValueError:
            pass

    thread.finished.connect(_release)
