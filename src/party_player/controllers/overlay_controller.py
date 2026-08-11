"""Coordinator for overlay preparation, playback, ducking, and UI events."""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from threading import RLock

from tinytag import TinyTag, TinyTagException

from party_player.bounded_executor import BoundedThreadPoolExecutor
from party_player.ducking import DuckingController
from party_player.overlay import (
    OverlayDefinition,
    OverlayPlayResult,
    OverlayRuntime,
    OverlayStatus,
)
from party_player.overlay_player import OverlayAudioPlayer
from party_player.performance_monitor import PerformanceMonitor


def overlay_duration_ms(path: Path) -> int:
    """Read duration outside the GUI thread without loading audio into Python."""

    try:
        duration = TinyTag.get(path, tags=False, duration=True).duration
    except (TinyTagException, OSError) as exc:
        raise ValueError(f"Overlay-Metadaten konnten nicht gelesen werden: {path}") from exc
    milliseconds = round(float(duration or 0.0) * 1000)
    if milliseconds <= 0:
        raise ValueError(f"Overlay-Dauer konnte nicht ermittelt werden: {path}")
    return milliseconds


class OverlayController:
    """The sole coordinator for the independent manual overlay channel."""

    def __init__(
        self,
        player: OverlayAudioPlayer,
        ducking: DuckingController,
        *,
        publish_status: Callable[[OverlayRuntime], None],
        dispatch: Callable[[Callable[[], None]], None] | None = None,
        duration_resolver: Callable[[Path], int] = overlay_duration_ms,
        record_history: (
            Callable[
                [OverlayDefinition, datetime, datetime, OverlayPlayResult, str],
                None,
            ]
            | None
        ) = None,
        wall_clock: Callable[[], datetime] = datetime.now,
        performance_monitor: PerformanceMonitor | None = None,
        prepare_cache_size: int = 6,
    ) -> None:
        self._player = player
        self._ducking = ducking
        self._publish_status = publish_status
        self._dispatch = dispatch or (lambda callback: callback())
        self._duration_resolver = duration_resolver
        self._record_history = record_history
        self._wall_clock = wall_clock
        self._performance = performance_monitor or PerformanceMonitor()
        self._prepare_executor = BoundedThreadPoolExecutor(
            max_workers=1,
            maximum_pending=2,
            thread_name_prefix="overlay-prepare",
        )
        self._lock = RLock()
        self._pending_definition: OverlayDefinition | None = None
        self._closed = False
        self._started_at: datetime | None = None
        self._terminal_result: OverlayPlayResult | None = None
        self._recorded_generation: int | None = None
        self._prepare_worker_active = False
        self._request_generation = 0
        self._prepare_aborted_count = 0
        self._prepare_cache_hits = 0
        self._prepare_cache_misses = 0
        self._prepare_cache_size = max(1, prepare_cache_size)
        self._duration_cache: OrderedDict[Path, tuple[int, int, int]] = OrderedDict()
        self._logger = logging.getLogger(__name__)

    @property
    def runtime(self) -> OverlayRuntime:
        return self._player.runtime

    def is_active(self) -> bool:
        """Return an I/O-free activity gate for global audio configuration."""
        return self.runtime.status not in {
            OverlayStatus.IDLE,
            OverlayStatus.FINISHED,
            OverlayStatus.FAILED,
        }

    def start(self, definition: OverlayDefinition) -> None:
        """Prepare and start, or safely replace the currently active overlay."""

        with self._lock:
            self._require_open()
        active = self.is_active()
        if active:
            with self._lock:
                self._pending_definition = definition
                self._terminal_result = OverlayPlayResult.STOPPED
                # Invalidate metadata/file work for the outgoing definition
                # immediately; its callback may return before the safety stop
                # has completed and queued the replacement.
                self._request_generation += 1
            self._player.stop()
            return
        with self._lock:
            self._submit_prepare(definition)

    def fade_out(self) -> bool:
        with self._performance.measure("overlay.fade_out", warning_threshold_ms=10.0):
            faded = self._player.fade_out()
        if faded:
            with self._lock:
                self._terminal_result = OverlayPlayResult.FADED_OUT
        return faded

    def stop(self) -> bool:
        with self._lock:
            self._pending_definition = None
            self._terminal_result = OverlayPlayResult.STOPPED
            self._request_generation += 1
        with self._performance.measure("overlay.stop", warning_threshold_ms=10.0):
            stopped = self._player.stop()
        if not stopped:
            self._ducking.reset()
        return stopped

    def update_position(self) -> None:
        """Perform only lightweight backend polling from a dedicated timer."""

        self._player.update_position()

    def diagnostics(self) -> dict[str, object]:
        with self._lock:
            return {
                "prepare_worker": "overlay-prepare",
                "prepare_worker_active": self._prepare_worker_active,
                "pending_switch": (
                    self._pending_definition.name if self._pending_definition is not None else ""
                ),
                "prepare_aborted_count": self._prepare_aborted_count,
                "prepare_cache_hits": self._prepare_cache_hits,
                "prepare_cache_misses": self._prepare_cache_misses,
                "prepare_cache_size": len(self._duration_cache),
            }

    def player_status_changed(self, runtime: OverlayRuntime) -> None:
        """Receive player events; safe to call from audio/prepare workers."""

        with self._lock:
            if runtime.status == OverlayStatus.PLAYING and self._started_at is None:
                self._started_at = self._wall_clock()
            elif runtime.status == OverlayStatus.FADING_OUT and self._terminal_result is None:
                self._terminal_result = OverlayPlayResult.COMPLETED
            elif runtime.status == OverlayStatus.FAILED:
                self._terminal_result = OverlayPlayResult.FAILED
            if runtime.status in {OverlayStatus.FINISHED, OverlayStatus.FAILED}:
                self._record_terminal_history(runtime)
        if runtime.status in {OverlayStatus.FINISHED, OverlayStatus.FAILED}:
            self._performance.record(
                "overlay.finish" if runtime.status == OverlayStatus.FINISHED else "overlay.failed",
                0.0,
                100.0,
                {"generation": runtime.generation},
            )
            self._ducking.release(
                runtime.definition.ducking_release_ms if runtime.definition is not None else 0
            )
            self._performance.record("ducking.release", 0.0, 100.0)

        def publish_snapshot() -> None:
            self._publish_status(runtime)

        self._dispatch(publish_snapshot)
        if runtime.status == OverlayStatus.FINISHED:
            with self._lock:
                pending, self._pending_definition = self._pending_definition, None
                if pending is not None and not self._closed:
                    self._submit_prepare(pending)

    def close(self) -> None:
        """Cancel new work and guarantee music restoration before shutdown."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._pending_definition = None
            self._request_generation += 1
            self._terminal_result = OverlayPlayResult.STOPPED
        self._player.stop_and_wait()
        # The stop callback queues the terminal history record on this executor.
        # Let already accepted work drain so application shutdown cannot discard
        # the final overlay result.
        self._prepare_executor.shutdown(wait=True, cancel_futures=False)
        with self._lock:
            self._duration_cache.clear()
        self._player.close()
        self._ducking.close()

    def _submit_prepare(self, definition: OverlayDefinition) -> None:
        with self._lock:
            self._started_at = None
            self._terminal_result = None
            self._request_generation += 1
            request_generation = self._request_generation

        def prepare_and_start() -> None:
            with self._lock:
                self._prepare_worker_active = True
            player_generation: int | None = None
            try:
                player_generation = self._player.begin_prepare(definition)
                with self._performance.measure(
                    "overlay.prepare",
                    warning_threshold_ms=500.0,
                    context={"overlay_id": definition.overlay_id},
                ):
                    duration_ms = self._cached_duration(Path(definition.file_path))
                    if not self._request_is_current(request_generation):
                        self._record_prepare_abort()
                        return
                    generation = self._player.prepare(
                        definition,
                        duration_ms=duration_ms,
                        generation=player_generation,
                    )
                current_generation = self.runtime.generation
                with self._lock:
                    if (
                        self._closed
                        or request_generation != self._request_generation
                        or generation != current_generation
                    ):
                        self._record_prepare_abort()
                        return
                    if definition.ducking_enabled:
                        self._ducking.attack(
                            definition.ducking_db,
                            definition.ducking_attack_ms,
                        )
                        self._performance.record("ducking.attack", 0.0, 100.0)
                    else:
                        self._ducking.reset()
                with self._performance.measure(
                    "overlay.start",
                    warning_threshold_ms=50.0,
                    context={"overlay_id": definition.overlay_id},
                ):
                    self._player.start(generation)
            except Exception as exc:
                self._ducking.reset()
                runtime = self.runtime
                if self._request_is_current(request_generation) and not (
                    runtime.definition == definition and runtime.status == OverlayStatus.FAILED
                ):
                    if player_generation is not None:
                        self._player.fail(player_generation, exc)
                    else:
                        self._player.report_prepare_failure(definition, exc)
                generation = self.runtime.generation
                self._logger.error(
                    "overlay.failed overlay_id=%s generation=%s error_type=%s path=%s message=%s",
                    definition.overlay_id,
                    generation,
                    type(exc).__name__,
                    definition.file_path,
                    exc,
                )
            finally:
                with self._lock:
                    self._prepare_worker_active = False

        try:
            self._prepare_executor.submit(prepare_and_start)
        except RuntimeError as exc:
            self._ducking.reset()
            raise RuntimeError("Overlay-Vorbereitung ist ausgelastet") from exc

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("Overlay-Controller ist geschlossen")

    def _cached_duration(self, path: Path) -> int:
        stat = path.stat()
        signature = (stat.st_size, stat.st_mtime_ns)
        with self._lock:
            cached = self._duration_cache.get(path)
            if cached is not None and cached[:2] == signature:
                self._duration_cache.move_to_end(path)
                self._prepare_cache_hits += 1
                self._performance.record("overlay.prepare_cache_hit_total", 1.0, 100.0)
                return cached[2]
            self._prepare_cache_misses += 1
            self._performance.record("overlay.prepare_cache_miss_total", 1.0, 100.0)
        duration_ms = self._duration_resolver(path)
        with self._lock:
            self._duration_cache[path] = (*signature, duration_ms)
            self._duration_cache.move_to_end(path)
            while len(self._duration_cache) > self._prepare_cache_size:
                self._duration_cache.popitem(last=False)
        return duration_ms

    def _request_is_current(self, request_generation: int) -> bool:
        with self._lock:
            return not self._closed and request_generation == self._request_generation

    def _record_prepare_abort(self) -> None:
        with self._lock:
            self._prepare_aborted_count += 1
        self._performance.record("overlay.prepare_aborted_total", 1.0, 100.0)

    def _record_terminal_history(self, runtime: OverlayRuntime) -> None:
        if (
            self._record_history is None
            or runtime.definition is None
            or self._recorded_generation == runtime.generation
        ):
            return
        result = self._terminal_result or (
            OverlayPlayResult.FAILED
            if runtime.status == OverlayStatus.FAILED
            else OverlayPlayResult.COMPLETED
        )
        completed_at = self._wall_clock()
        started_at = self._started_at or completed_at
        self._recorded_generation = runtime.generation
        definition = runtime.definition
        error = runtime.error

        def persist_history() -> None:
            assert self._record_history is not None
            self._record_history(
                definition,
                started_at,
                completed_at,
                result,
                error,
            )

        try:
            self._prepare_executor.submit(persist_history)
        except RuntimeError:
            self._logger.error(
                "Overlay-Historie konnte nicht eingereiht werden: %s",
                definition.name,
            )
