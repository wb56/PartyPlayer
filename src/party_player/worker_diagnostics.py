"""Thread snapshots and lightweight worker lifecycle tracking."""

from dataclasses import dataclass
from collections import deque
from threading import Lock, enumerate as enumerate_threads
from time import monotonic


@dataclass(frozen=True, slots=True)
class ThreadSnapshot:
    name: str
    identifier: int | None
    daemon: bool
    alive: bool


def collect_thread_snapshot() -> tuple[ThreadSnapshot, ...]:
    return tuple(
        ThreadSnapshot(thread.name, thread.ident, thread.daemon, thread.is_alive())
        for thread in enumerate_threads()
    )


@dataclass(frozen=True, slots=True)
class WorkerInfo:
    worker_id: str
    name: str
    category: str
    started_at_monotonic: float
    daemon: bool
    operation_id: str | None


@dataclass(frozen=True, slots=True)
class CompletedWorkerInfo:
    worker_id: str
    name: str
    category: str
    operation_id: str | None
    daemon: bool
    running_duration: float
    state: str


class WorkerRegistry:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._workers: dict[str, WorkerInfo] = {}
        self._history: deque[CompletedWorkerInfo] = deque(maxlen=500)
        self._lock = Lock()

    def started(self, worker: WorkerInfo) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._workers[worker.worker_id] = worker

    def finished(self, worker_id: str, state: str = "completed") -> None:
        if not self.enabled:
            return
        with self._lock:
            worker = self._workers.pop(worker_id, None)
            if worker is not None:
                self._history.append(
                    CompletedWorkerInfo(
                        worker.worker_id,
                        worker.name,
                        worker.category,
                        worker.operation_id,
                        worker.daemon,
                        max(0.0, monotonic() - worker.started_at_monotonic),
                        state,
                    )
                )

    def active(self) -> tuple[WorkerInfo, ...]:
        with self._lock:
            return tuple(self._workers.values())

    def runtimes(self) -> dict[str, float]:
        now = monotonic()
        return {worker.worker_id: now - worker.started_at_monotonic for worker in self.active()}

    def history(self) -> tuple[CompletedWorkerInfo, ...]:
        with self._lock:
            return tuple(self._history)

    def reset_history(self) -> None:
        """Discard completed diagnostics while retaining currently active workers."""
        with self._lock:
            self._history.clear()
