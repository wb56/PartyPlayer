"""Independent, generation-safe audio player for one overlay channel."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from threading import RLock
from time import monotonic, sleep

from party_player.audio.base import AudioBackend
from party_player.overlay import (
    OverlayDefinition,
    OverlayRuntime,
    OverlayStateMachine,
    OverlayStatus,
    ResolvedOverlayPlayback,
    resolve_overlay,
)


class OverlayAudioPlayer:
    """Drive one backend without sharing state with either music deck."""

    SAFETY_STOP_FADE_MS = 50
    RAMP_INTERVAL_SECONDS = 0.02

    def __init__(
        self,
        backend: AudioBackend,
        *,
        on_status: Callable[[OverlayRuntime], None] | None = None,
    ) -> None:
        self._backend = backend
        self._machine = OverlayStateMachine()
        self._on_status = on_status
        self._lock = RLock()
        self._ramp_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="vlc-volume-overlay",
        )
        self._ramp_future: Future[None] | None = None
        self._current_fade_volume = 0.0
        self._master_muted = False
        self._closed = False

    @property
    def runtime(self) -> OverlayRuntime:
        with self._lock:
            return self._machine.runtime

    def set_status_callback(
        self,
        callback: Callable[[OverlayRuntime], None] | None,
    ) -> None:
        """Attach the coordinator after composition without recreating VLC."""

        with self._lock:
            self._on_status = callback

    def begin_prepare(self, definition: OverlayDefinition) -> int:
        """Publish PREPARING before worker-side metadata or NAS access."""

        with self._lock:
            self._require_open()
            generation = self._machine.begin_prepare(definition)
            self._publish()
            return generation

    def prepare(
        self,
        definition: OverlayDefinition,
        *,
        duration_ms: int,
        generation: int | None = None,
    ) -> int:
        """Prepare and adopt a medium; callers may invoke this in their prepare worker."""

        if generation is None:
            generation = self.begin_prepare(definition)
        else:
            with self._lock:
                self._require_open()
                runtime = self._machine.runtime
                if (
                    runtime.generation != generation
                    or runtime.status != OverlayStatus.PREPARING
                    or runtime.definition != definition
                ):
                    return generation
        prepared: object | None = None
        try:
            resolved = resolve_overlay(definition, duration_ms=duration_ms)
            prepared = self._backend.prepare(resolved.path)
            with self._lock:
                if generation != self._machine.runtime.generation:
                    self._backend.release_prepared(prepared)
                    return generation
                self._backend.load_prepared(resolved.path, prepared)
                prepared = None
                if self._machine.prepared(generation, resolved):
                    self._publish()
            return generation
        except Exception as exc:
            if prepared is not None:
                self._backend.release_prepared(prepared)
            with self._lock:
                if self._machine.fail(generation, exc):
                    self._publish()
            raise

    def report_prepare_failure(
        self,
        definition: OverlayDefinition,
        error: BaseException,
    ) -> None:
        """Publish metadata/file failures that happen before backend preparation."""

        with self._lock:
            if self._closed:
                return
            generation = self._machine.begin_prepare(definition)
            self._publish()
            if self._machine.fail(generation, error):
                self._publish()

    def fail(self, generation: int, error: BaseException) -> bool:
        """Fail one current pre-player operation without creating a new generation."""

        with self._lock:
            failed = self._machine.fail(generation, error)
            if failed:
                self._publish()
            return failed

    def start(self, generation: int) -> bool:
        """Start at Cue-In and apply the configured fade-in asynchronously."""

        with self._lock:
            self._require_open()
            if not self._machine.start(generation):
                return False
            playback = self._playback()
            self._apply_volume(0.0 if playback.fade_in_ms else playback.volume)
            self._backend.seek(playback.cue_in_ms / 1000.0)
            self._backend.play()
            self._publish()
            if playback.fade_in_ms:
                self._submit_ramp(
                    generation,
                    start=0.0,
                    target=playback.volume,
                    duration_ms=playback.fade_in_ms,
                    completion=self._fade_in_complete,
                )
            return True

    def fade_out(self) -> bool:
        """Request the normal fade-out; repeated requests are idempotent."""

        with self._lock:
            self._require_open()
            previous = self._machine.runtime
            generation = self._machine.begin_fade_out()
            if generation is None:
                return False
            if previous.status == OverlayStatus.FADING_OUT:
                return True
            playback = self._playback()
            self._publish()
            self._submit_ramp(
                generation,
                start=playback.volume,
                target=0.0,
                duration_ms=playback.fade_out_ms,
                completion=self._finish_and_stop,
            )
            return True

    def stop(self) -> bool:
        """Cancel prepare/play/fade through a short click-safe volume ramp."""

        with self._lock:
            self._require_open()
            previous = self._machine.runtime
            generation = self._machine.begin_stop()
            if generation is None:
                return False
            if previous.status == OverlayStatus.STOPPING:
                return True
            volume = previous.playback.volume if previous.playback is not None else 0.0
            self._publish()
            self._submit_ramp(
                generation,
                start=volume,
                target=0.0,
                duration_ms=self.SAFETY_STOP_FADE_MS,
                completion=self._finish_and_stop,
            )
            return True

    def update_position(self) -> None:
        """Detect Cue-Out or natural backend completion from a lightweight timer."""

        with self._lock:
            if self._machine.runtime.status not in {
                OverlayStatus.FADING_IN,
                OverlayStatus.PLAYING,
            }:
                return
            playback = self._playback()
            position_ms = round(self._backend.get_position() * 1000)
            generation = self._machine.runtime.generation
            if self._machine.update_position(generation, position_ms):
                self._publish()
            if position_ms >= playback.cue_out_ms - playback.fade_out_ms:
                self.fade_out()
            elif self._backend.is_finished():
                self._finish_and_stop(generation)

    def set_output_device(self, device_id: str) -> None:
        self._backend.set_output_device(device_id)

    def set_master_muted(self, muted: bool) -> None:
        """Apply global emergency mute without changing overlay runtime state."""

        with self._lock:
            self._master_muted = bool(muted)
            self._backend.set_volume(0.0 if self._master_muted else self._current_fade_volume)

    def close(self) -> None:
        """Invalidate ramps, stop playback, and release the sole backend player."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._machine.reset()
        self._ramp_executor.shutdown(wait=True, cancel_futures=True)
        self._backend.close()

    def stop_and_wait(self, timeout_seconds: float = 0.25) -> bool:
        """Apply the safety fade and wait only for its tightly bounded completion."""

        stopped = self.stop()
        with self._lock:
            future = self._ramp_future
        if not stopped or future is None:
            return stopped
        try:
            future.result(timeout=max(0.0, timeout_seconds))
        except FutureTimeoutError:
            return False
        return True

    def _submit_ramp(
        self,
        generation: int,
        *,
        start: float,
        target: float,
        duration_ms: int,
        completion: Callable[[int], None],
    ) -> None:
        def ramp() -> None:
            duration = max(0.0, duration_ms / 1000.0)
            started = monotonic()
            while True:
                with self._lock:
                    if self._closed or generation != self._machine.runtime.generation:
                        return
                elapsed = monotonic() - started
                progress = 1.0 if duration == 0 else min(1.0, elapsed / duration)
                with self._lock:
                    if self._closed or generation != self._machine.runtime.generation:
                        return
                    self._apply_volume(start + ((target - start) * progress))
                if progress >= 1.0:
                    completion(generation)
                    return
                sleep(min(self.RAMP_INTERVAL_SECONDS, max(0.0, duration - elapsed)))

        self._ramp_future = self._ramp_executor.submit(ramp)

    def _fade_in_complete(self, generation: int) -> None:
        with self._lock:
            if self._machine.fade_in_complete(generation):
                self._publish()

    def _finish_and_stop(self, generation: int) -> None:
        with self._lock:
            if self._machine.finish(generation):
                self._backend.stop()
                self._publish()

    def _playback(self) -> ResolvedOverlayPlayback:
        playback = self._machine.runtime.playback
        if playback is None:
            raise RuntimeError("Kein Overlay vorbereitet")
        return playback

    def _apply_volume(self, volume: float) -> None:
        self._current_fade_volume = max(0.0, min(1.0, volume))
        self._backend.set_volume(0.0 if self._master_muted else self._current_fade_volume)

    def _publish(self) -> None:
        if self._on_status is not None:
            self._on_status(self._machine.runtime)

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("Overlay-Player ist geschlossen")
