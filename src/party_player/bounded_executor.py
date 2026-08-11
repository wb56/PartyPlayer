"""Thread-pool wrapper with a fixed running-plus-pending task capacity."""

from concurrent.futures import Executor, Future, ThreadPoolExecutor, TimeoutError
from collections.abc import Callable
from threading import BoundedSemaphore, Condition
from time import monotonic
from typing import Any


class BoundedThreadPoolExecutor(Executor):
    """Reject submissions instead of allowing ThreadPoolExecutor's queue to grow."""

    def __init__(self, *, max_workers: int, maximum_pending: int, thread_name_prefix: str) -> None:
        self._capacity = BoundedSemaphore(max(max_workers, maximum_pending))
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix=thread_name_prefix
        )
        self._max_workers = max_workers
        self._closed = False
        self._accepting = True
        self._pending = 0
        self._finalizer_pending = False
        self._condition = Condition()

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        if not self._capacity.acquire(blocking=False):
            raise RuntimeError("Arbeitswarteschlange hat ihre feste Kapazität erreicht")
        with self._condition:
            if self._closed or not self._accepting:
                self._capacity.release()
                raise RuntimeError("Arbeitswarteschlange nimmt keine neuen Aufträge an")
            self._pending += 1
        try:
            future = self._executor.submit(fn, *args, **kwargs)
        except Exception:
            with self._condition:
                self._pending -= 1
                self._condition.notify_all()
            self._capacity.release()
            raise
        future.add_done_callback(self._completed)
        return future

    def block_new_work(self) -> bool:
        with self._condition:
            if self._closed or self._finalizer_pending:
                return False
            self._accepting = False
            return True

    def resume_new_work(self) -> bool:
        with self._condition:
            if self._closed or self._finalizer_pending:
                return False
            self._accepting = True
            return True

    def drain(self, timeout: float) -> bool:
        deadline = monotonic() + max(0.0, timeout)
        with self._condition:
            while self._pending:
                remaining = deadline - monotonic()
                if remaining <= 0.0:
                    return False
                self._condition.wait(remaining)
            return True

    def run_owner_finalizer(self, callback: Callable[[], bool], timeout: float = 2.0) -> bool:
        """Run a finalizer on the sole worker after blocking and draining."""
        with self._condition:
            if (
                self._closed
                or self._accepting
                or self._pending
                or self._finalizer_pending
                or self._max_workers != 1
            ):
                return False
            self._finalizer_pending = True
        future = self._executor.submit(callback)
        future.add_done_callback(self._finalizer_completed)
        try:
            result = bool(future.result(timeout=max(0.1, timeout)))
        except (TimeoutError, RuntimeError):
            return False
        with self._condition:
            self._finalizer_pending = False
            self._condition.notify_all()
        return result

    def _completed(self, _future: Future[Any]) -> None:
        with self._condition:
            self._pending -= 1
            self._condition.notify_all()
        self._capacity.release()

    def _finalizer_completed(self, _future: Future[Any]) -> None:
        with self._condition:
            self._finalizer_pending = False
            self._condition.notify_all()

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        with self._condition:
            self._closed = True
            self._accepting = False
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)
