"""Bounded thread-safe event bridge into the GUI thread."""

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
import logging
from threading import Lock
from time import monotonic


class GuiEventType(StrEnum):
    CALLBACK = "callback"
    STATUS_MESSAGE = "status_message"
    ERROR_MESSAGE = "error_message"
    COVER_READY = "cover_ready"
    PRELOAD_COMPLETED = "preload_completed"
    PRELOAD_FAILED = "preload_failed"
    CUE_PREVIEW_STATUS = "cue_preview_status"
    IMPORT_PROGRESS = "import_progress"
    IMPORT_COMPLETED = "import_completed"
    QUEUE_CHANGED = "queue_changed"
    DECK_STATE_CHANGED = "deck_state_changed"


@dataclass(frozen=True, slots=True)
class GuiEvent:
    event_type: GuiEventType
    source: str
    payload: object
    operation_id: str | None = None
    coalesce_key: str | None = None


@dataclass(frozen=True, slots=True)
class DispatcherStatistics:
    pending: int
    maximum_pending: int
    published: int
    processed: int
    coalesced: int
    discarded: int
    critical_overflow: int
    maximum_items_processed_per_cycle: int
    maximum_dispatch_duration_ms: float
    average_dispatch_duration_ms: float


class GuiEventQueueFull(RuntimeError):
    pass


class GuiEventDispatcher:
    def __init__(
        self,
        *,
        capacity: int = 1000,
        max_items_per_cycle: int = 50,
        budget_ms: float = 8.0,
        clock: Callable[[], float] = monotonic,
        diagnostics_enabled: bool = True,
    ) -> None:
        self._capacity = max(1, capacity)
        self._max_items = max(1, max_items_per_cycle)
        self._budget_ms = max(0.1, budget_ms)
        self._clock = clock
        self._diagnostics_enabled = diagnostics_enabled
        self._events: deque[GuiEvent] = deque()
        self._lock = Lock()
        self._maximum_pending = 0
        self._published = 0
        self._processed = 0
        self._coalesced = 0
        self._discarded = 0
        self._critical_overflow = 0
        self._dispatch_cycles = 0
        self._dispatch_total_ms = 0.0
        self._dispatch_maximum_ms = 0.0
        self._maximum_items_per_cycle = 0
        self._logger = logging.getLogger(__name__)

    def publish(self, event: GuiEvent) -> bool:
        with self._lock:
            if self._diagnostics_enabled:
                self._published += 1
            if event.coalesce_key is not None:
                for index, queued in enumerate(self._events):
                    if queued.coalesce_key == event.coalesce_key:
                        self._events[index] = event
                        if self._diagnostics_enabled:
                            self._coalesced += 1
                        return True
            if len(self._events) >= self._capacity:
                removable = next(
                    (i for i, queued in enumerate(self._events) if queued.coalesce_key is not None),
                    None,
                )
                if removable is not None:
                    del self._events[removable]
                    if self._diagnostics_enabled:
                        self._discarded += 1
                elif event.coalesce_key is not None:
                    if self._diagnostics_enabled:
                        self._discarded += 1
                    return False
                else:
                    if self._diagnostics_enabled:
                        self._critical_overflow += 1
                    raise GuiEventQueueFull(
                        "GUI-Ereigniswarteschlange enthält nur kritische Events"
                    )
            self._events.append(event)
            if self._diagnostics_enabled:
                self._maximum_pending = max(self._maximum_pending, len(self._events))
            return True

    def process_pending_events(self, handler: Callable[[GuiEvent], None]) -> int:
        started = self._clock()
        processed = 0
        while processed < self._max_items:
            if (self._clock() - started) * 1000.0 >= self._budget_ms:
                break
            with self._lock:
                if not self._events:
                    break
                event = self._events.popleft()
            try:
                handler(event)
            except Exception:
                self._logger.exception("GUI-Ereignis konnte nicht verarbeitet werden: %s", event)
            processed += 1
        if not self._diagnostics_enabled:
            return processed
        self._processed += processed
        elapsed_ms = max(0.0, (self._clock() - started) * 1000.0)
        self._dispatch_cycles += 1
        self._dispatch_total_ms += elapsed_ms
        self._dispatch_maximum_ms = max(self._dispatch_maximum_ms, elapsed_ms)
        self._maximum_items_per_cycle = max(self._maximum_items_per_cycle, processed)
        return processed

    def statistics(self) -> DispatcherStatistics:
        with self._lock:
            pending = len(self._events)
        return DispatcherStatistics(
            pending,
            self._maximum_pending,
            self._published,
            self._processed,
            self._coalesced,
            self._discarded,
            self._critical_overflow,
            self._maximum_items_per_cycle,
            self._dispatch_maximum_ms,
            self._dispatch_total_ms / self._dispatch_cycles if self._dispatch_cycles else 0.0,
        )

    def reset_statistics(self) -> None:
        """Reset diagnostics without discarding already queued GUI events."""
        with self._lock:
            self._maximum_pending = len(self._events)
            self._published = 0
            self._processed = 0
            self._coalesced = 0
            self._discarded = 0
            self._critical_overflow = 0
            self._dispatch_cycles = 0
            self._dispatch_total_ms = 0.0
            self._dispatch_maximum_ms = 0.0
            self._maximum_items_per_cycle = 0
