"""Non-blocking transient music ducking for the independent overlay channel."""

from __future__ import annotations

import math
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import RLock
from time import monotonic, sleep


def db_to_linear(db: float) -> float:
    """Convert a safe attenuation in dB to a linear gain factor."""

    bounded = max(-60.0, min(0.0, float(db)))
    return math.pow(10.0, bounded / 20.0)


class DuckingController:
    """Ramp one shared factor and invalidate superseded attack/release work."""

    RAMP_INTERVAL_SECONDS = 0.02

    def __init__(
        self,
        apply_factor: Callable[[float], None],
        *,
        on_changed: Callable[[float, str], None] | None = None,
    ) -> None:
        self._apply_factor = apply_factor
        self._on_changed = on_changed
        self._factor = 1.0
        self._generation = 0
        self._closed = False
        self._lock = RLock()
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="overlay-ducking",
        )

    @property
    def factor(self) -> float:
        with self._lock:
            return self._factor

    def set_changed_callback(
        self,
        callback: Callable[[float, str], None] | None,
    ) -> None:
        with self._lock:
            self._on_changed = callback

    def attack(self, target_db: float, duration_ms: int) -> int:
        """Lower music toward *target_db*, smoothly replacing any prior ramp."""

        return self._begin_ramp(db_to_linear(target_db), duration_ms, "attack")

    def release(self, duration_ms: int) -> int:
        """Restore unmodified music volume."""

        return self._begin_ramp(1.0, duration_ms, "release")

    def reset(self) -> None:
        """Immediately and unconditionally restore music after any failure."""

        with self._lock:
            self._generation += 1
            self._set_factor(1.0, "idle")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._generation += 1
            self._set_factor(1.0, "idle")
        self._executor.shutdown(wait=True, cancel_futures=True)

    def _begin_ramp(self, target: float, duration_ms: int, phase: str) -> int:
        with self._lock:
            if self._closed:
                raise RuntimeError("Ducking-Controller ist geschlossen")
            self._generation += 1
            generation = self._generation
            start = self._factor

        def ramp() -> None:
            duration = max(0.0, duration_ms / 1000.0)
            started = monotonic()
            while True:
                with self._lock:
                    if self._closed or generation != self._generation:
                        return
                    elapsed = monotonic() - started
                    progress = 1.0 if duration == 0 else min(1.0, elapsed / duration)
                    self._set_factor(start + ((target - start) * progress), phase)
                if progress >= 1.0:
                    return
                sleep(min(self.RAMP_INTERVAL_SECONDS, max(0.0, duration - elapsed)))

        self._executor.submit(ramp)
        return generation

    def _set_factor(self, factor: float, phase: str) -> None:
        normalized = max(0.001, min(1.0, factor))
        if math.isclose(normalized, self._factor, abs_tol=0.0005):
            return
        self._factor = normalized
        self._apply_factor(normalized)
        if self._on_changed is not None:
            self._on_changed(normalized, phase)
