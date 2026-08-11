"""Independent watchdog that captures the GUI stack during an actual blockage."""

from dataclasses import dataclass
from pathlib import Path
import logging
from threading import Event, Lock, Thread, current_thread
from time import monotonic
from collections.abc import Callable
from contextlib import contextmanager
from collections.abc import Iterator

from party_player.thread_dump import ThreadDumpWriter


@dataclass(frozen=True, slots=True)
class GuiCallbackSnapshot:
    """Thread-safe point-in-time state of heartbeat and application GUI work."""

    last_heartbeat_monotonic: float
    active_gui_callback: str | None
    active_gui_callback_started_at: float | None
    last_started_gui_callback: str | None
    last_completed_gui_callback: str | None
    last_completed_gui_callback_at: float | None
    active_catalog_render: str | None
    active_queue_render: str | None
    pending_layout_refreshes: int
    pending_focus_request: bool
    pending_catalog_chunks: int
    pending_queue_chunks: int
    catalog_rows_created: int
    queue_rows_created: int


class GuiCallbackState:
    """Share tiny callback assignments between Tk and the watchdog thread.

    The lock protects assignments and snapshots only. No file I/O, rendering,
    logging or callback execution is performed while it is held.
    """

    def __init__(self, *, clock: Callable[[], float] = monotonic) -> None:
        self._clock = clock
        self._lock = Lock()
        self._last_heartbeat = clock()
        self._active_stack: list[tuple[str, float]] = []
        self._last_started: str | None = None
        self._last_completed: str | None = None
        self._last_completed_at: float | None = None
        self._layout_state = (0, False, 0, 0, 0, 0)

    def heartbeat(self) -> None:
        """Publish one successful Tk heartbeat using a monotonic timestamp."""
        now = self._clock()
        with self._lock:
            self._last_heartbeat = now

    def mark_started(self, name: str) -> None:
        """Publish callback entry before any measured application work begins."""
        now = self._clock()
        with self._lock:
            self._active_stack.append((name, now))
            self._last_started = name

    def mark_completed(self, name: str) -> None:
        """Publish callback exit and restore an enclosing measured callback."""
        with self._lock:
            for index in range(len(self._active_stack) - 1, -1, -1):
                if self._active_stack[index][0] == name:
                    del self._active_stack[index]
                    break
            self._last_completed = name
            self._last_completed_at = self._clock()

    def update_layout_state(
        self,
        *,
        pending_layout_refreshes: int,
        pending_focus_request: bool,
        pending_catalog_chunks: int,
        pending_queue_chunks: int,
        catalog_rows_created: int,
        queue_rows_created: int,
    ) -> None:
        """Publish scalar GUI gauges without allowing the watchdog to touch Tk."""
        with self._lock:
            self._layout_state = (
                pending_layout_refreshes,
                pending_focus_request,
                pending_catalog_chunks,
                pending_queue_chunks,
                catalog_rows_created,
                queue_rows_created,
            )

    def snapshot(self) -> GuiCallbackSnapshot:
        """Copy the complete diagnostic state while holding the lock briefly."""
        with self._lock:
            active = self._active_stack[-1] if self._active_stack else None
            catalog = next(
                (name for name, _started in reversed(self._active_stack) if "catalog" in name),
                None,
            )
            queue = next(
                (name for name, _started in reversed(self._active_stack) if "queue" in name),
                None,
            )
            return GuiCallbackSnapshot(
                self._last_heartbeat,
                active[0] if active else None,
                active[1] if active else None,
                self._last_started,
                self._last_completed,
                self._last_completed_at,
                catalog,
                queue,
                *self._layout_state,
            )

    @contextmanager
    def track(self, name: str) -> Iterator[None]:
        """Publish one named nested GUI operation with guaranteed cleanup."""
        self.mark_started(name)
        try:
            yield
        finally:
            self.mark_completed(name)


class GuiHeartbeatWatchdog:
    """Monitor heartbeat age from a daemon thread without touching Tkinter."""

    def __init__(
        self,
        state: GuiCallbackState,
        *,
        diagnostics_directory: Path = Path("diagnostics"),
        test_context: Callable[[], str],
        playback_state: Callable[[], str],
        dispatcher_state: Callable[[], str],
        interval_seconds: float = 0.1,
        warning_threshold_ms: float = 250.0,
        critical_threshold_ms: float = 750.0,
        clock: Callable[[], float] = monotonic,
        writer: ThreadDumpWriter | None = None,
    ) -> None:
        self._state = state
        self._context = test_context
        self._playback = playback_state
        self._dispatcher = dispatcher_state
        self._interval = interval_seconds
        self._warning_threshold_ms = warning_threshold_ms
        self._critical_threshold_ms = critical_threshold_ms
        self._clock = clock
        self._writer = writer or ThreadDumpWriter(diagnostics_directory, clock=clock)
        self._stop = Event()
        self._started = Event()
        self._thread: Thread | None = None
        self._critical_block_active = False
        self._last_seen_heartbeat = state.snapshot().last_heartbeat_monotonic
        self._logger = logging.getLogger(__name__)

    @property
    def is_running(self) -> bool:
        """Return whether the daemon thread is currently alive."""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start one watchdog thread; repeated calls are harmless."""
        if self.is_running:
            return
        self._stop.clear()
        self._started.clear()
        self._state.heartbeat()
        self._thread = Thread(
            target=self._watchdog_loop,
            name="gui-heartbeat-watchdog",
            daemon=True,
        )
        self._thread.start()
        self._started.wait(0.2)

    def stop(self, timeout: float = 1.0) -> None:
        """Signal shutdown and briefly join without blocking application exit."""
        # A heavily loaded scheduler may not wake the daemon for its final
        # interval before shutdown. Preserve one last overdue-heartbeat check;
        # the critical-block guard prevents duplicate dumps.
        self._check_heartbeat()
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not current_thread():
            thread.join(timeout)
        self._thread = None

    def _watchdog_loop(self) -> None:
        """Capture the MainThread stack while its heartbeat is still overdue."""
        self._started.set()
        while not self._stop.is_set():
            self._check_heartbeat()
            if self._stop.wait(self._interval):
                break

    def _check_heartbeat(self) -> None:
        """Evaluate one snapshot immediately without delaying initial startup."""
        snapshot = self._state.snapshot()
        if snapshot.last_heartbeat_monotonic != self._last_seen_heartbeat:
            self._last_seen_heartbeat = snapshot.last_heartbeat_monotonic
            self._critical_block_active = False
            # Observing a new heartbeat is proof of recovery for this cycle.
            # Evaluate its age only on a later watchdog pass; otherwise a
            # scheduler-delayed check can misclassify that same heartbeat as
            # a second critical block.
            return
        delay_ms = max(
            0.0,
            (self._clock() - snapshot.last_heartbeat_monotonic) * 1000.0,
        )
        if delay_ms < self._warning_threshold_ms:
            self._critical_block_active = False
            return
        if delay_ms < self._critical_threshold_ms or self._critical_block_active:
            return
        self._critical_block_active = True
        try:
            self._writer.write(
                delay_ms,
                self._context(),
                self._playback(),
                self._dispatcher(),
                callback_snapshot=snapshot,
            )
        except Exception:
            # Diagnostics must never terminate playback or the watchdog loop.
            self._logger.exception("GUI-Watchdog konnte keinen Thread-Dump schreiben")
