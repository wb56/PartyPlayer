"""State-driven choreography for automatic two-deck transitions."""

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from threading import Event, Lock, Thread
from time import monotonic

from party_player.crossfader_service import CrossfaderService
from party_player.deck_controller import DeckController
from party_player.enums import DeckState
from party_player.cue_points import ResolvedTrackBoundaries
from party_player.performance_monitor import PerformanceMonitor


@dataclass(frozen=True, slots=True)
class CrossfadeLevelSample:
    elapsed_ms: float
    position: float
    normalization_a: float
    normalization_b: float
    backend_volume_a: float
    backend_volume_b: float


@dataclass(frozen=True, slots=True)
class CrossfadeLevelDiagnostic:
    sample_count: int
    direction: str
    duration_ms: float
    maximum_sample_gap_ms: float
    position_monotonic: bool
    reached_target: bool
    audio_ramp_complete: bool


class TransitionState(StrEnum):
    IDLE = "idle"
    PRELOAD = "preload"
    BUFFERING = "buffering"
    READY = "ready"
    WAIT_FOR_ACTUAL_PLAYBACK = "wait_for_actual_playback"
    CROSSFADE = "crossfade"
    VERIFY_COMPLETION = "verify_completion"
    STOP_FIRST_DECK = "stop_first_deck"
    UNLOAD_FIRST_DECK = "unload_first_deck"
    LOAD_NEXT_TRACK = "load_next_track"
    ABORTED = "aborted"
    FAILED = "failed"


class TransitionController:
    """Coordinate playback confirmation and an elapsed-time crossfade."""

    # Network-backed media can need several seconds before VLC reports actual
    # playback even though the play command itself succeeded.  Keep the outgoing
    # deck audible while waiting instead of treating normal NAS latency as a
    # failed handover.
    START_WAIT_STEPS = 160
    START_WAIT_INTERVAL_MS = 50
    START_RETRY_STEP = 20
    FADE_INTERVAL_MS = 16
    RENDER_INTERVAL_MS = 100

    def __init__(
        self,
        crossfader: CrossfaderService,
        schedule: Callable[[int, Callable[[], None]], object],
        render: Callable[[], None],
        complete: Callable[[DeckController, int | None, int | None], None],
        fade_duration: float = 7.0,
        performance_monitor: PerformanceMonitor | None = None,
        failure: Callable[[str, DeckController, DeckController], None] | None = None,
    ) -> None:
        self.crossfader = crossfader
        self._schedule = schedule
        self._render = render
        self._complete = complete
        self.fade_duration = fade_duration
        self.state = TransitionState.IDLE
        self._generation = 0
        self._logger = logging.getLogger(__name__)
        self._performance = performance_monitor or PerformanceMonitor()
        self._completion_pending = False
        self._failure = failure
        self._level_samples: deque[CrossfadeLevelSample] = deque(maxlen=1000)
        self._level_samples_lock = Lock()

    def level_samples(self) -> tuple[CrossfadeLevelSample, ...]:
        """Return bounded crossfade gain samples for diagnostic reports."""
        with self._level_samples_lock:
            return tuple(self._level_samples)

    def level_diagnostic(self) -> CrossfadeLevelDiagnostic:
        """Summarize the latest bounded audio ramp without touching GUI state."""
        samples = self.level_samples()
        if not samples:
            return CrossfadeLevelDiagnostic(0, "none", 0.0, 0.0, True, False, False)
        first, last = samples[0], samples[-1]
        direction = "A_TO_B" if last.position >= first.position else "B_TO_A"
        deltas = [
            current.elapsed_ms - previous.elapsed_ms
            for previous, current in zip(samples, samples[1:], strict=False)
        ]
        if direction == "A_TO_B":
            monotonic_position = all(
                current.position + 1e-9 >= previous.position
                for previous, current in zip(samples, samples[1:], strict=False)
            )
            reached_target = last.position >= 0.999
        else:
            monotonic_position = all(
                current.position <= previous.position + 1e-9
                for previous, current in zip(samples, samples[1:], strict=False)
            )
            reached_target = last.position <= 0.001
        maximum_gap = max(deltas, default=0.0)
        return CrossfadeLevelDiagnostic(
            len(samples),
            direction,
            max(0.0, last.elapsed_ms - first.elapsed_ms),
            maximum_gap,
            monotonic_position,
            reached_target,
            monotonic_position and reached_target and maximum_gap <= 100.0,
        )

    @property
    def is_transitioning(self) -> bool:
        return self.state not in {
            TransitionState.IDLE,
            TransitionState.PRELOAD,
            TransitionState.BUFFERING,
            TransitionState.READY,
            TransitionState.ABORTED,
            TransitionState.FAILED,
        }

    def preload_started(self, deck_id: str) -> None:
        if self.is_transitioning:
            return
        self.state = TransitionState.PRELOAD
        self._logger.info("Preload für Deck %s gestartet", deck_id)
        self.state = TransitionState.BUFFERING

    def preload_ready(self, deck_id: str, elapsed: float) -> None:
        if self.is_transitioning:
            return
        self.state = TransitionState.READY
        self._logger.info("Preload für Deck %s nach %.3f Sekunden bereit", deck_id, elapsed)

    def preload_failed(self, deck_id: str, error: Exception) -> None:
        if self.is_transitioning:
            return
        self.state = TransitionState.FAILED
        self._logger.warning("Preload für Deck %s fehlgeschlagen: %s", deck_id, error)

    def begin(
        self,
        outgoing: DeckController,
        incoming: DeckController,
        outgoing_queue_id: int | None,
        boundaries: ResolvedTrackBoundaries | None = None,
    ) -> None:
        if self.is_transitioning:
            return
        self._generation += 1
        generation = self._generation
        outgoing_track_id = (
            outgoing.model.loaded_track.id if outgoing.model.loaded_track is not None else None
        )
        incoming_start_position = incoming.model.position
        self.state = TransitionState.WAIT_FOR_ACTUAL_PLAYBACK
        self._logger.info(
            "Übergang wartet auf Deck %s; ausgehend Deck %s",
            incoming.model.deck_id,
            outgoing.model.deck_id,
        )
        self._wait_for_playback(
            outgoing,
            incoming,
            outgoing_track_id,
            outgoing_queue_id,
            generation,
            0,
            boundaries,
            incoming_start_position,
        )

    def abort(self, reason: str) -> None:
        if not self.is_transitioning:
            return
        self._generation += 1
        self._completion_pending = False
        self.state = TransitionState.ABORTED
        self._logger.info("Automatischer Übergang abgebrochen: %s", reason)

    def reset(self) -> None:
        self._generation += 1
        self._completion_pending = False
        self.state = TransitionState.IDLE

    def _wait_for_playback(
        self,
        outgoing: DeckController,
        incoming: DeckController,
        outgoing_track_id: int | None,
        outgoing_queue_id: int | None,
        generation: int,
        step: int,
        boundaries: ResolvedTrackBoundaries | None,
        incoming_start_position: float,
    ) -> None:
        if generation != self._generation:
            return
        backend_playing = incoming.backend.is_playing()
        actual_position = incoming.backend.get_position()
        requested_position = incoming_start_position
        # Some VLC outputs advance their media clock several seconds before
        # ``is_playing()`` becomes reliable. A forward-moving clock is direct
        # evidence of decoded playback and must not leave audible audio muted.
        position_advanced = actual_position >= requested_position + 0.05
        if (backend_playing and actual_position >= requested_position - 0.25) or position_advanced:
            evidence = "VLC-Status" if backend_playing else "Positionsfortschritt"
            self._logger.info(
                "Wiedergabe auf Deck %s bei %.2f Sekunden bestätigt (%s)",
                incoming.model.deck_id,
                actual_position,
                evidence,
            )
            self._start_crossfade(
                outgoing,
                incoming,
                outgoing_track_id,
                outgoing_queue_id,
                generation,
                boundaries,
            )
            return
        if (
            outgoing.model.loaded_track is None
            or incoming.model.loaded_track is None
            or incoming.model.state != DeckState.PLAYING
        ):
            self.abort("Deckzustand während der Startprüfung geändert")
            return
        if step == self.START_RETRY_STEP:
            try:
                incoming.play()
                self._logger.warning(
                    "Wiedergabestart auf Deck %s wird einmalig wiederholt",
                    incoming.model.deck_id,
                )
            except Exception as exc:
                self._logger.warning(
                    "Wiederholung des Wiedergabestarts auf Deck %s fehlgeschlagen: %s",
                    incoming.model.deck_id,
                    exc,
                )
        if step >= self.START_WAIT_STEPS:
            self.state = TransitionState.FAILED
            if self._failure is not None:
                self._failure("INCOMING_PLAYBACK_NOT_CONFIRMED", outgoing, incoming)
            self._logger.warning(
                "Deck %s meldet keine tatsächliche Wiedergabe; Übergang wird ausgelassen",
                incoming.model.deck_id,
            )
            return

        def transition_playback_check() -> None:
            self._wait_for_playback(
                outgoing,
                incoming,
                outgoing_track_id,
                outgoing_queue_id,
                generation,
                step + 1,
                boundaries,
                incoming_start_position,
            )

        self._schedule(self.START_WAIT_INTERVAL_MS, transition_playback_check)

    def _start_crossfade(
        self,
        outgoing: DeckController,
        incoming: DeckController,
        outgoing_track_id: int | None,
        outgoing_queue_id: int | None,
        generation: int,
        boundaries: ResolvedTrackBoundaries | None,
    ) -> None:
        with self._level_samples_lock:
            self._level_samples.clear()
        self.state = TransitionState.CROSSFADE
        incoming.set_transition_muted(False)
        start = self.crossfader.position
        target = 0.0 if incoming.model.deck_id == "A" else 1.0
        cue_out = boundaries.cue_out if boundaries is not None else outgoing.model.duration
        configured_duration = (
            boundaries.fade_duration if boundaries is not None else self.fade_duration
        )
        remaining = max(0.0, cue_out - outgoing.model.position)
        duration = max(0.25, min(configured_duration, remaining))
        started_at = monotonic()
        ramp_first_at: float | None = None
        ramp_last_at: float | None = None
        self._logger.info("Crossfade gestartet; Dauer %.2f Sekunden", duration)
        fade_finished = Event()
        tick_delays: list[float] = []
        volume_command_count = 0

        def audio_fade() -> None:
            """Advance audio independently from a busy Tkinter event queue."""
            nonlocal volume_command_count, ramp_first_at, ramp_last_at
            previous_tick = monotonic()
            while generation == self._generation:
                now = monotonic()
                if volume_command_count:
                    tick_delays.append(max(0.0, now - previous_tick))
                previous_tick = now
                progress = min(1.0, (now - started_at) / duration)
                eased_progress = progress * progress * (3.0 - 2.0 * progress)
                if generation != self._generation:
                    return
                self.crossfader.set_position(start + (target - start) * eased_progress)
                with self._level_samples_lock:
                    self._level_samples.append(
                        CrossfadeLevelSample(
                            elapsed_ms=(now - started_at) * 1000.0,
                            position=self.crossfader.position,
                            normalization_a=self.crossfader.deck_a.normalization_factor,
                            normalization_b=self.crossfader.deck_b.normalization_factor,
                            backend_volume_a=self.crossfader.deck_a.effective_volume,
                            backend_volume_b=self.crossfader.deck_b.effective_volume,
                        )
                    )
                if ramp_first_at is None:
                    ramp_first_at = now
                ramp_last_at = now
                volume_command_count += 1
                if progress >= 1.0:
                    fade_finished.set()
                    return
                fade_finished.wait(self.FADE_INTERVAL_MS / 1000.0)

        Thread(
            target=audio_fade,
            name=f"crossfade-{outgoing.model.deck_id}-{incoming.model.deck_id}",
            daemon=True,
        ).start()

        def render_tick() -> None:
            if generation != self._generation:
                fade_finished.set()
                return
            self._render()
            if not fade_finished.is_set():
                self._schedule(self.RENDER_INTERVAL_MS, render_tick)
                return
            self.state = TransitionState.VERIFY_COMPLETION
            if not incoming.backend.is_playing():
                self.state = TransitionState.FAILED
                self._logger.error("Eingehendes Deck spielt am Crossfade-Ende nicht mehr")
                if self._failure is not None:
                    self._failure("INCOMING_PLAYBACK_LOST", outgoing, incoming)
                return
            self._logger.info(
                "Crossfade-Audiotakt abgeschlossen; Dauer %.2f Sekunden",
                monotonic() - started_at,
            )
            actual_duration = monotonic() - started_at
            completion_detected_at = monotonic()
            actual_ramp_duration = max(
                0.0,
                (ramp_last_at or completion_detected_at) - (ramp_first_at or started_at),
            )
            start_delay = max(0.0, (ramp_first_at or started_at) - started_at)
            completion_detection_delay = max(
                0.0, completion_detected_at - (ramp_last_at or completion_detected_at)
            )
            maximum_delay = max(tick_delays, default=0.0)
            average_delay = sum(tick_delays) / len(tick_delays) if tick_delays else 0.0
            context: dict[str, object] = {
                "requested_fade_duration": duration,
                "actual_wall_clock_duration": actual_duration,
                "maximum_tick_delay": maximum_delay,
                "average_tick_delay": average_delay,
                "volume_command_count": volume_command_count,
            }
            self._performance.record(
                "crossfade.timing",
                actual_duration * 1000.0,
                (duration + max(0.1, duration * 0.05)) * 1000.0,
                context,
            )
            for metric, value in (
                ("crossfade.configured_duration_ms", configured_duration * 1000.0),
                ("crossfade.actual_ramp_duration_ms", actual_ramp_duration * 1000.0),
                ("crossfade.start_delay_ms", start_delay * 1000.0),
                (
                    "crossfade.completion_detection_delay_ms",
                    completion_detection_delay * 1000.0,
                ),
                (
                    "crossfade.duration_deviation_ms",
                    (actual_ramp_duration - configured_duration) * 1000.0,
                ),
            ):
                self._performance.record(metric, value, max(100.0, duration * 50.0))
            tolerance = max(0.1, duration * 0.05)
            if actual_duration - duration > tolerance:
                self._logger.warning("Crossfade-Timing-Abweichung: %s", context)

            # Return to Tk first so the final slider position is painted before
            # eject/queue persistence can block while VLC releases the old media.
            if not self._completion_pending:
                self._completion_pending = True
                self._schedule(0, finish_transition)

        def finish_transition() -> None:
            if generation != self._generation:
                self._completion_pending = False
                return
            self.state = TransitionState.STOP_FIRST_DECK
            self.state = TransitionState.UNLOAD_FIRST_DECK
            cleanup_started = monotonic()
            completion_started = monotonic()
            with self._performance.measure(
                "playback.transition_completion",
                warning_threshold_ms=500.0,
                context={"outgoing_deck": outgoing.model.deck_id},
            ):
                self._complete(outgoing, outgoing_track_id, outgoing_queue_id)
            completion_ms = (monotonic() - completion_started) * 1000.0
            self._performance.record("crossfade.completion_processing_ms", completion_ms, 50.0)
            self._performance.record(
                "crossfade.total_transition_ms",
                (monotonic() - started_at) * 1000.0,
                (duration + 0.1) * 1000.0,
            )
            self.state = TransitionState.LOAD_NEXT_TRACK
            self._logger.info(
                "Crossfade abgeschlossen; Deck-Bereinigung %.2f Sekunden",
                monotonic() - cleanup_started,
            )
            self.state = TransitionState.IDLE
            self._completion_pending = False

        self._schedule(self.RENDER_INTERVAL_MS, render_tick)
