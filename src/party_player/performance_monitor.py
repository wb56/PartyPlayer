"""Low-overhead operation timing and GUI heartbeat diagnostics."""

from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import logging
from threading import Lock, current_thread
from time import monotonic
import math


@dataclass(frozen=True, slots=True)
class PerformanceSettings:
    enabled: bool = True
    gui_operation_warning_ms: float = 50.0
    gui_step_warning_ms: float = 25.0
    gui_heartbeat_interval_ms: int = 100
    gui_heartbeat_warning_ms: float = 250.0
    gui_heartbeat_critical_ms: float = 750.0
    gui_event_queue_capacity: int = 1000
    gui_event_max_items_per_cycle: int = 50
    gui_event_budget_ms: float = 8.0
    slow_warning_rate_limit_seconds: float = 5.0


@dataclass(frozen=True, slots=True)
class OperationTiming:
    operation: str
    elapsed_ms: float
    thread_name: str
    context: dict[str, object]


@dataclass(frozen=True, slots=True)
class OperationStatistics:
    count: int
    total_duration_ms: float
    average_duration_ms: float
    maximum_duration_ms: float
    last_duration_ms: float
    slow_operation_count: int
    minimum_value_ms: float
    maximum_value_ms: float
    average_value_ms: float
    maximum_absolute_value_ms: float


class _MutableStatistics:
    def __init__(self) -> None:
        self.count = 0
        self.total = 0.0
        self.minimum = float("inf")
        self.maximum = float("-inf")
        self.maximum_absolute = 0.0
        self.last = 0.0
        self.slow = 0
        self.recent: deque[float] = deque(maxlen=500)


class PerformanceMonitor:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = monotonic,
        logger: logging.Logger | None = None,
        warning_rate_limit_seconds: float = 5.0,
        enabled: bool = True,
    ) -> None:
        self._clock = clock
        self._logger = logger or logging.getLogger(__name__)
        self._rate_limit = warning_rate_limit_seconds
        self.enabled = enabled
        self._statistics: dict[str, _MutableStatistics] = {}
        self._last_warning: dict[str, float] = {}
        self._lock = Lock()
        self._scenario_started_at: float | None = None
        self._invalid_timing_sample_count = 0
        self._negative_timing_sample_count = 0
        self._orphaned_timing_context_count = 0
        self._invalid_samples: deque[tuple[str, float, str]] = deque(maxlen=100)
        self._counters: dict[str, int] = {}
        self._gauges: dict[str, float] = {}

    def begin_scenario(self) -> None:
        """Start the plausibility window used by diagnostic timing validation."""
        with self._lock:
            self._scenario_started_at = self._clock()
            self._invalid_timing_sample_count = 0
            self._negative_timing_sample_count = 0
            self._orphaned_timing_context_count = 0
            self._invalid_samples.clear()

    def reset_statistics(self) -> None:
        """Start a fresh diagnostic measurement window."""
        with self._lock:
            self._statistics.clear()
            self._last_warning.clear()
            self._counters.clear()
            self._gauges.clear()
            self._scenario_started_at = None

    def increment_counter(self, name: str, amount: int = 1) -> int:
        """Increment and return one path-free diagnostic counter."""
        if not self.enabled:
            return 0
        if amount < 0:
            raise ValueError("Counter increments must not be negative")
        with self._lock:
            value = self._counters.get(name, 0) + amount
            self._counters[name] = value
            return value

    def set_gauge(self, name: str, value: float | int | bool) -> None:
        """Set one current-state gauge without unbounded labels."""
        if not self.enabled:
            return
        numeric_value = float(value)
        if not math.isfinite(numeric_value):
            raise ValueError("Gauge values must be finite")
        with self._lock:
            self._gauges[name] = numeric_value

    def counters(self) -> dict[str, int]:
        """Return a stable copy of all diagnostic counters."""
        with self._lock:
            return dict(self._counters) if self.enabled else {}

    def gauges(self) -> dict[str, float]:
        """Return a stable copy of all current-state gauges."""
        with self._lock:
            return dict(self._gauges) if self.enabled else {}

    @contextmanager
    def measure(
        self,
        operation: str,
        *,
        warning_threshold_ms: float,
        context: dict[str, object] | None = None,
    ) -> Iterator[None]:
        started = self._clock()
        if not self.enabled:
            yield
            return
        try:
            yield
        finally:
            elapsed_ms = max(0.0, (self._clock() - started) * 1000.0)
            self.record(operation, elapsed_ms, warning_threshold_ms, context)

    def record(
        self,
        operation: str,
        elapsed_ms: float,
        warning_threshold_ms: float,
        context: dict[str, object] | None = None,
    ) -> OperationTiming:
        if not self.enabled:
            return OperationTiming(operation, elapsed_ms, current_thread().name, context or {})
        with self._lock:
            invalid_reason: str | None = None
            signed_value = operation.endswith("duration_deviation_ms")
            if not math.isfinite(elapsed_ms):
                invalid_reason = "not_finite"
            elif elapsed_ms < 0 and not signed_value:
                self._negative_timing_sample_count += 1
                invalid_reason = "negative_duration"
            elif self._scenario_started_at is not None:
                scenario_elapsed_ms = max(0.0, (self._clock() - self._scenario_started_at) * 1000.0)
                if elapsed_ms > scenario_elapsed_ms + 1.0:
                    invalid_reason = "exceeds_scenario_duration"
            if invalid_reason is not None:
                self._invalid_timing_sample_count += 1
                self._invalid_samples.append((operation, elapsed_ms, invalid_reason))
                return OperationTiming(
                    operation,
                    elapsed_ms,
                    current_thread().name,
                    {
                        **(context or {}),
                        "measurement_status": "invalid",
                        "measurement_reason": invalid_reason,
                    },
                )
            stats = self._statistics.setdefault(operation, _MutableStatistics())
            stats.count += 1
            stats.total += elapsed_ms
            stats.minimum = min(stats.minimum, elapsed_ms)
            stats.maximum = max(stats.maximum, elapsed_ms)
            stats.maximum_absolute = max(stats.maximum_absolute, abs(elapsed_ms))
            stats.last = elapsed_ms
            stats.recent.append(elapsed_ms)
        timing = OperationTiming(operation, elapsed_ms, current_thread().name, context or {})
        if elapsed_ms > warning_threshold_ms:
            with self._lock:
                stats.slow += 1
                now = self._clock()
                should_log = (
                    now - self._last_warning.get(operation, float("-inf")) >= self._rate_limit
                )
                if should_log:
                    self._last_warning[operation] = now
            if should_log:
                self._logger.warning(
                    "Langsame Operation %s: %.1f ms, Thread=%s, Kontext=%s",
                    operation,
                    elapsed_ms,
                    timing.thread_name,
                    timing.context,
                )
        return timing

    def statistics(self) -> dict[str, OperationStatistics]:
        if not self.enabled:
            return {}
        with self._lock:
            return {
                name: OperationStatistics(
                    item.count,
                    item.total,
                    item.total / item.count if item.count else 0.0,
                    item.maximum,
                    item.last,
                    item.slow,
                    item.minimum if item.count else 0.0,
                    item.maximum if item.count else 0.0,
                    item.total / item.count if item.count else 0.0,
                    item.maximum_absolute,
                )
                for name, item in self._statistics.items()
            }

    def validation_counters(self) -> dict[str, int]:
        with self._lock:
            return {
                "invalid_timing_sample_count": self._invalid_timing_sample_count,
                "negative_timing_sample_count": self._negative_timing_sample_count,
                "orphaned_timing_context_count": self._orphaned_timing_context_count,
            }

    def invalid_samples(self) -> tuple[tuple[str, float, str], ...]:
        with self._lock:
            return tuple(self._invalid_samples)


@dataclass(frozen=True, slots=True)
class HeartbeatStatistics:
    last_delay_ms: float
    maximum_delay_ms: float
    average_delay_ms: float
    count: int
    warning_count: int
    critical_count: int


class GuiHeartbeat:
    """Measure Tk scheduling delay and report warning/recovery statistics.

    Cause capture deliberately belongs to the independent heartbeat watchdog;
    this Tk callback never writes a diagnostic dump after the blockage ended.
    """

    def __init__(
        self,
        settings: PerformanceSettings,
        *,
        clock: Callable[[], float] = monotonic,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._logger = logger or logging.getLogger(__name__)
        self._expected_at: float | None = None
        self._count = 0
        self._total_delay = 0.0
        self._last_delay = 0.0
        self._maximum_delay = 0.0
        self._warnings = 0
        self._critical = 0
        self._last_log = float("-inf")
        self._series_count = 0
        self._series_maximum = 0.0

    def start(self) -> None:
        if not self._settings.enabled:
            return
        self._expected_at = self._clock() + self._settings.gui_heartbeat_interval_ms / 1000.0

    def reset_statistics(self) -> None:
        """Reset counters while preserving the next expected heartbeat."""
        self._count = 0
        self._total_delay = 0.0
        self._last_delay = 0.0
        self._maximum_delay = 0.0
        self._warnings = 0
        self._critical = 0
        self._series_count = 0
        self._series_maximum = 0.0

    def beat(self) -> float:
        if not self._settings.enabled:
            return 0.0
        now = self._clock()
        if self._expected_at is None:
            self.start()
            return 0.0
        delay_ms = max(0.0, (now - self._expected_at) * 1000.0)
        self._expected_at = now + self._settings.gui_heartbeat_interval_ms / 1000.0
        self._count += 1
        self._total_delay += delay_ms
        self._last_delay = delay_ms
        self._maximum_delay = max(self._maximum_delay, delay_ms)
        if delay_ms >= self._settings.gui_heartbeat_warning_ms:
            self._warnings += 1
            self._series_count += 1
            self._series_maximum = max(self._series_maximum, delay_ms)
            if delay_ms >= self._settings.gui_heartbeat_critical_ms:
                self._critical += 1
            if now - self._last_log >= self._settings.slow_warning_rate_limit_seconds:
                self._last_log = now
                self._logger.warning("GUI-Heartbeat verzögert: %.1f ms", delay_ms)
        elif self._series_count:
            self._logger.info(
                "GUI-Heartbeat erholt: %d verzögerte Callbacks, Maximum %.1f ms",
                self._series_count,
                self._series_maximum,
            )
            self._series_count = 0
            self._series_maximum = 0.0
        return delay_ms

    def statistics(self) -> HeartbeatStatistics:
        return HeartbeatStatistics(
            self._last_delay,
            self._maximum_delay,
            self._total_delay / self._count if self._count else 0.0,
            self._count,
            self._warnings,
            self._critical,
        )
