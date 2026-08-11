"""Bounded Python and native-process memory diagnostics."""

from collections import deque
from dataclasses import dataclass
import os
from pathlib import Path
import sys
from threading import Lock, active_count
import tracemalloc


@dataclass(frozen=True, slots=True)
class MemorySample:
    process_rss_bytes: int | None
    process_rss_status: str
    python_traced_bytes: int
    python_peak_bytes: int
    active_thread_count: int
    gui_event_queue_size: int
    active_worker_count: int
    cover_cache_size: int
    registered_widget_count: int
    active_preview_count: int
    active_vlc_player_count: int


@dataclass(frozen=True, slots=True)
class MemoryGrowth:
    filename: str
    line_number: int
    object_count: int
    total_size_bytes: int


@dataclass(frozen=True, slots=True)
class MemoryStressCycle:
    cycle_number: int
    queue_size: int
    queue_row_views: int
    tk_widget_count: int
    tooltip_instances_current: int
    python_traced_bytes: int
    process_rss_bytes: int | None
    widgets_created_delta: int
    widgets_destroyed_delta: int


class MemoryMonitor:
    """Retain bounded aggregate samples and optional tracemalloc comparisons."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        tracemalloc_enabled: bool = False,
        maximum_samples: int = 500,
        maximum_growth_entries: int = 20,
    ) -> None:
        self.enabled = enabled
        self._samples: deque[MemorySample] = deque(maxlen=max(1, maximum_samples))
        self._maximum_growth_entries = max(1, maximum_growth_entries)
        self._growth: tuple[MemoryGrowth, ...] = ()
        self._before_snapshot: tracemalloc.Snapshot | None = None
        self._stress_cycles: deque[MemoryStressCycle] = deque(maxlen=10)
        self._last_widget_totals = (0, 0)
        self._lock = Lock()
        self._owns_tracemalloc = False
        if enabled and tracemalloc_enabled and not tracemalloc.is_tracing():
            tracemalloc.start(10)
            self._owns_tracemalloc = True

    def enable_tracemalloc(self) -> bool:
        """Explicitly enable Python allocation tracing for a diagnostic scenario."""
        if not self.enabled:
            return False
        if not tracemalloc.is_tracing():
            tracemalloc.start(10)
            self._owns_tracemalloc = True
        return True

    def sample(
        self,
        *,
        gui_event_queue_size: int,
        active_worker_count: int,
        cover_cache_size: int,
        registered_widget_count: int,
        active_preview_count: int,
        active_vlc_player_count: int,
    ) -> MemorySample | None:
        if not self.enabled:
            return None
        traced, peak = tracemalloc.get_traced_memory() if tracemalloc.is_tracing() else (0, 0)
        rss, rss_status = process_rss()
        sample = MemorySample(
            rss,
            rss_status,
            traced,
            peak,
            active_count(),
            gui_event_queue_size,
            active_worker_count,
            cover_cache_size,
            registered_widget_count,
            active_preview_count,
            active_vlc_player_count,
        )
        with self._lock:
            self._samples.append(sample)
        return sample

    def begin_snapshot(self) -> bool:
        if not self.enabled or not tracemalloc.is_tracing():
            return False
        self._before_snapshot = tracemalloc.take_snapshot()
        self._growth = ()
        return True

    def end_snapshot(self) -> tuple[MemoryGrowth, ...]:
        before = self._before_snapshot
        self._before_snapshot = None
        if before is None or not tracemalloc.is_tracing():
            return ()
        comparison = tracemalloc.take_snapshot().compare_to(before, "lineno")
        growth = tuple(
            MemoryGrowth(
                str(Path(item.traceback[0].filename)),
                item.traceback[0].lineno,
                item.count_diff,
                item.size_diff,
            )
            for item in comparison
            if item.size_diff > 0
        )[: self._maximum_growth_entries]
        with self._lock:
            self._growth = growth
        return growth

    def latest(self) -> MemorySample | None:
        with self._lock:
            return self._samples[-1] if self._samples else None

    def sample_count(self) -> int:
        with self._lock:
            return len(self._samples)

    def growth(self) -> tuple[MemoryGrowth, ...]:
        with self._lock:
            return self._growth

    def record_stress_cycle(
        self,
        cycle_number: int,
        queue_size: int,
        widget_diagnostics: dict[str, int],
    ) -> MemoryStressCycle | None:
        """Capture one bounded fill/scroll/clear cycle after explicit collection."""
        sample = self.latest()
        if sample is None:
            return None
        created = widget_diagnostics.get("widgets_created_total", 0)
        destroyed = widget_diagnostics.get("widgets_destroyed_total", 0)
        previous_created, previous_destroyed = self._last_widget_totals
        cycle = MemoryStressCycle(
            cycle_number,
            queue_size,
            widget_diagnostics.get("queue_row_views", 0),
            widget_diagnostics.get("tk_widget_count", 0),
            widget_diagnostics.get("tooltip_instances_current", 0),
            sample.python_traced_bytes,
            sample.process_rss_bytes,
            created - previous_created,
            destroyed - previous_destroyed,
        )
        self._last_widget_totals = (created, destroyed)
        with self._lock:
            self._stress_cycles.append(cycle)
        return cycle

    def stress_cycles(self) -> tuple[MemoryStressCycle, ...]:
        with self._lock:
            return tuple(self._stress_cycles)

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()
            self._growth = ()
            self._stress_cycles.clear()
            self._last_widget_totals = (0, 0)
        self._before_snapshot = None

    def close(self) -> None:
        """Idempotently release snapshots and tracing owned by this monitor."""
        self.reset()
        if self._owns_tracemalloc and tracemalloc.is_tracing():
            tracemalloc.stop()
        self._owns_tracemalloc = False
        self.enabled = False


def process_rss() -> tuple[int | None, str]:
    """Return RSS and its source, or an explicit unavailable status."""
    psutil_missing = False
    try:
        import psutil  # type: ignore[import-untyped]

        rss = int(psutil.Process().memory_info().rss)
        if rss > 0:
            return rss, "available"
    except ImportError:
        psutil_missing = True
    except OSError:
        pass
    if sys.platform == "win32":
        rss = _windows_rss_bytes()
        return (
            (rss, "available")
            if rss
            else (None, "psutil_not_available" if psutil_missing else "unavailable")
        )
    statm = Path("/proc/self/statm")
    if statm.exists():
        try:
            resident_pages = int(statm.read_text(encoding="ascii").split()[1])
            rss = resident_pages * int(os.sysconf("SC_PAGE_SIZE"))
            return (rss, "available") if rss > 0 else (None, "unavailable")
        except (OSError, ValueError, IndexError):
            return None, "psutil_not_available" if psutil_missing else "unavailable"
    return None, "psutil_not_available" if psutil_missing else "unavailable"


def process_rss_bytes() -> int | None:
    """Backward-compatible RSS value accessor without an invalid zero sentinel."""
    return process_rss()[0]


def _windows_rss_bytes() -> int:
    try:
        import ctypes
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        process = ctypes.windll.kernel32.GetCurrentProcess()
        if ctypes.windll.psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
            return int(counters.WorkingSetSize)
    except (AttributeError, OSError):
        return 0
    return 0
