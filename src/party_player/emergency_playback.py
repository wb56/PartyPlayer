"""Two-phase preparation and confirmed activation of local emergency audio."""

from dataclasses import dataclass, replace
from collections.abc import Callable
from enum import StrEnum
import logging
from threading import Event, Lock, Thread
from time import monotonic, sleep

from party_player.crossfader_service import CrossfaderService
from party_player.deck_controller import DeckController
from party_player.emergency_playlist import EmergencyMediaType, LocalEmergencyPlaylistService
from party_player.emergency_playlist import EmergencyPlaylistValidation
from party_player.emergency_state import DeckHealth, EmergencyStateService, EmergencySystemState
from party_player.enums import DeckState
from party_player.audio_recovery import AudioRecoveryTimeoutError
from party_player.audio.base import RuntimeClipProtectionBackend
from party_player.models import Track
from party_player.cue_points import ResolvedTrackBoundaries
from party_player.loudness import ResolvedLoudnessSettings


@dataclass(frozen=True, slots=True)
class EmergencyPlaybackResult:
    success: bool
    state: str
    deck_id: str | None = None
    track_id: int | None = None
    error_code: str = ""
    message: str = ""
    attempt: int = 0
    attempts_remaining: int = 0
    cue_in: float = 0.0
    effective_gain_db: float = 0.0
    clip_protection_enabled: bool = False


class EmergencyHandoverStrategy(StrEnum):
    SAFE_HANDOVER = "SAFE_HANDOVER"
    IMMEDIATE_REPLACE = "IMMEDIATE_REPLACE"


class EmergencyPlaybackService:
    """Keep current audio intact until muted emergency playback is confirmed."""

    def __init__(
        self,
        playlist: LocalEmergencyPlaylistService,
        state: EmergencyStateService,
        deck_a: DeckController,
        deck_b: DeckController,
        crossfader: CrossfaderService,
        handover_duration_seconds: float = 0.75,
        media_load_timeout_seconds: float = 5.0,
        playback_start_timeout_seconds: float = 2.0,
        playback_confirmation_seconds: float = 2.0,
        total_activation_timeout_seconds: float = 8.0,
        maximum_start_attempts: int = 3,
        cue_provider: Callable[[Track], ResolvedTrackBoundaries] | None = None,
        loudness_provider: Callable[[Track], ResolvedLoudnessSettings] | None = None,
        playback_started: (
            Callable[[EmergencyPlaybackResult, EmergencyMediaType, Track], None] | None
        ) = None,
    ) -> None:
        self._playlist = playlist
        self._state = state
        self._decks = (deck_a, deck_b)
        self._crossfader = crossfader
        self._handover_duration = max(0.1, min(handover_duration_seconds, 3.0))
        self._media_load_timeout = max(0.05, media_load_timeout_seconds)
        self._playback_start_timeout = max(0.05, playback_start_timeout_seconds)
        self._playback_confirmation = max(0.05, playback_confirmation_seconds)
        self._total_activation_timeout = max(0.1, total_activation_timeout_seconds)
        self._maximum_start_attempts = max(1, maximum_start_attempts)
        self._failed_start_attempts = 0
        self._cue_provider = cue_provider
        self._loudness_provider = loudness_provider
        self._playback_started = playback_started
        self._logger = logging.getLogger(__name__)
        self._prepared: tuple[DeckController, int, EmergencyMediaType, bool] | None = None
        self._prepared_safety = (0.0, 0.0, False)
        self._silent_primary: tuple[Track, object, DeckController] | None = None
        self._handover_lock = Lock()
        self._handover_generation = 0

    def playlist_validation(self) -> EmergencyPlaylistValidation:
        """Expose the cached startup validation without touching media or storage."""
        return self._playlist.validation()

    def prepare_primary(self) -> EmergencyPlaybackResult:
        return self.prepare_media(EmergencyMediaType.PRIMARY)

    def preload_primary_silently(self) -> EmergencyPlaybackResult:
        """Parse/cache the primary medium without changing either deck player."""
        tracks = self._playlist.candidates(EmergencyMediaType.PRIMARY)
        if not tracks:
            return EmergencyPlaybackResult(
                False,
                "NOT_READY",
                error_code="EMERGENCY_MEDIA_NOT_READY",
                message="Kein geprüfter lokaler Primärtitel verfügbar",
            )
        snapshot = self._state.snapshot()
        health = {"A": snapshot.deck_a, "B": snapshot.deck_b}
        source = next(
            (deck for deck in self._decks if health[deck.model.deck_id] == DeckHealth.HEALTHY),
            None,
        )
        if source is None:
            return EmergencyPlaybackResult(
                False,
                "NO_HEALTHY_DECK",
                error_code="NO_HEALTHY_DECK_FOR_PRELOAD",
                message="Kein gesundes Deck zum sicheren Vorladen verfügbar",
            )
        track = tracks[0]
        playing_before = tuple(deck.backend.is_playing() for deck in self._decks)
        try:
            prepared = self._run_timed(
                lambda: source.prepare(track),
                self._media_load_timeout,
                "EMERGENCY_PRELOAD_TIMEOUT",
                "Notfalltitel konnte nicht rechtzeitig vorgeladen werden",
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return EmergencyPlaybackResult(
                False,
                "FAILED",
                source.model.deck_id,
                track.id,
                (
                    exc.error_code
                    if isinstance(exc, AudioRecoveryTimeoutError)
                    else "EMERGENCY_PRELOAD_FAILED"
                ),
                str(exc),
            )
        if tuple(deck.backend.is_playing() for deck in self._decks) != playing_before:
            source.backend.release_prepared(prepared)
            return EmergencyPlaybackResult(
                False,
                "FAILED",
                source.model.deck_id,
                track.id,
                "EMERGENCY_PRELOAD_CHANGED_PLAYBACK",
                "Vorladen hat den Wiedergabestatus unerwartet verändert",
            )
        self.invalidate_prepared()
        self._silent_primary = (track, prepared, source)
        return EmergencyPlaybackResult(True, "PRELOADED_SILENT", source.model.deck_id, track.id)

    def invalidate_prepared(self) -> None:
        """Discard a preload after deck/backend lifecycle changes."""
        self._prepared = None
        self._prepared_safety = (0.0, 0.0, False)
        if self._silent_primary is not None:
            _track, prepared, source = self._silent_primary
            source.backend.release_prepared(prepared)
            self._silent_primary = None

    def mute_deck_immediately(self, deck_id: str) -> EmergencyPlaybackResult:
        normalized = deck_id.upper()
        deck = next(
            (candidate for candidate in self._decks if candidate.model.deck_id == normalized),
            None,
        )
        if deck is None:
            return EmergencyPlaybackResult(
                False, "BLOCKED", error_code="UNKNOWN_DECK", message="Unbekanntes Deck"
            )
        deck.set_emergency_muted(True)
        return EmergencyPlaybackResult(True, "MUTED", normalized)

    def immediate_replace(self, affected_deck_id: str) -> EmergencyPlaybackResult:
        """Replace unacceptable output after its deck has already been hard-muted."""
        normalized = affected_deck_id.upper()
        affected = next(
            (candidate for candidate in self._decks if candidate.model.deck_id == normalized),
            None,
        )
        if affected is None:
            return EmergencyPlaybackResult(
                False, "BLOCKED", error_code="UNKNOWN_DECK", message="Unbekanntes Deck"
            )
        affected.set_emergency_muted(True)
        try:
            affected.stop()
        except (OSError, RuntimeError):
            pass
        self._state.set_deck_health(
            normalized, DeckHealth.FAILED, "Unzumutbare Ausgabe sofort ersetzt"
        )
        if self._state.snapshot().system == EmergencySystemState.NORMAL:
            self._state.transition(EmergencySystemState.WARNING, "Unzumutbare Audioausgabe")
        if self._state.snapshot().system == EmergencySystemState.WARNING:
            self._state.transition(EmergencySystemState.DEGRADED, "Sofortiger Notfallersatz")
        prepared = self.prepare_primary()
        if not prepared.success or self._prepared is None:
            return prepared
        target = self._prepared[0]
        target.set_fade_level_immediately(0.0)
        activated = self.activate_prepared()
        if activated.success:
            self._start_immediate_fade_in(target)
        return activated

    def prepare_media(
        self, media_type: EmergencyMediaType, *, loop: bool = False
    ) -> EmergencyPlaybackResult:
        selected_type = EmergencyMediaType(media_type)
        if loop and not self._playlist.loop_allowed(selected_type):
            return EmergencyPlaybackResult(
                False,
                "BLOCKED",
                error_code="LOOP_NOT_ALLOWED",
                message="Schleifen sind ausschließlich für Pausenmusik erlaubt",
            )
        tracks = self._playlist.candidates(selected_type)
        if not tracks:
            return EmergencyPlaybackResult(
                False,
                "NOT_READY",
                error_code="EMERGENCY_MEDIA_NOT_READY",
                message=f"Kein geprüftes lokales Medium für {selected_type.value} verfügbar",
            )
        snapshot = self._state.snapshot()
        health = {"A": snapshot.deck_a, "B": snapshot.deck_b}
        deck = next(
            (
                candidate
                for candidate in self._decks
                if health[candidate.model.deck_id] == DeckHealth.HEALTHY
                and not candidate.backend.is_playing()
                and candidate.model.state != DeckState.PLAYING
            ),
            None,
        )
        if deck is None:
            return EmergencyPlaybackResult(
                False,
                "NO_HEALTHY_DECK",
                error_code="NO_HEALTHY_INACTIVE_DECK",
                message="Kein gesundes inaktives Deck verfügbar",
            )
        track = tracks[0]
        deadline = monotonic() + self._total_activation_timeout
        self._state.set_deck_health(deck.model.deck_id, DeckHealth.BUFFERING, selected_type.value)
        try:
            deck.set_transition_muted(True)
            prepared = self._run_timed(
                lambda: deck.prepare(track),
                self._bounded_timeout(deadline, self._media_load_timeout),
                "EMERGENCY_PREPARE_TIMEOUT",
                "Notfallmedium konnte nicht rechtzeitig vorbereitet werden",
            )
            self._run_timed(
                lambda: deck.load_prepared(track, prepared),
                self._bounded_timeout(deadline, self._media_load_timeout),
                "EMERGENCY_LOAD_TIMEOUT",
                "Notfallmedium konnte nicht rechtzeitig geladen werden",
            )
            # Normal deck loading clears its preparation gate. Emergency audio
            # must remain inaudible until playback has been positively confirmed.
            deck.set_transition_muted(True)
            deck.set_emergency_muted(True)
            safety = self._apply_audio_safety(deck, track, deadline)
            if deck.backend.is_playing():
                deck.stop()
                raise RuntimeError("Notfallmedium startete während des Vorladens unerwartet")
        except (OSError, RuntimeError, ValueError) as exc:
            self._state.set_deck_health(deck.model.deck_id, DeckHealth.FAILED, str(exc))
            self._state.transition(EmergencySystemState.DEGRADED, "Notfalltitel nicht ladbar")
            return EmergencyPlaybackResult(
                False,
                "FAILED",
                deck.model.deck_id,
                track.id,
                (
                    exc.error_code
                    if isinstance(exc, AudioRecoveryTimeoutError)
                    else "EMERGENCY_LOAD_FAILED"
                ),
                str(exc),
            )
        self._state.set_deck_health(deck.model.deck_id, DeckHealth.HEALTHY, "Notfallmedium bereit")
        self._prepared = (deck, track.id, selected_type, loop)
        self._prepared_safety = safety
        return EmergencyPlaybackResult(
            True,
            "PREPARED",
            deck.model.deck_id,
            track.id,
            cue_in=safety[0],
            effective_gain_db=safety[1],
            clip_protection_enabled=safety[2],
        )

    def activate_prepared(self) -> EmergencyPlaybackResult:
        if self._prepared is None and self._silent_primary is not None:
            materialized = self._materialize_silent_primary()
            if not materialized.success:
                return materialized
        if self._prepared is None:
            return EmergencyPlaybackResult(
                False,
                "NOT_PREPARED",
                error_code="EMERGENCY_NOT_PREPARED",
                message="Notfalltitel wurde noch nicht vorbereitet",
            )
        if self._failed_start_attempts >= self._maximum_start_attempts:
            return EmergencyPlaybackResult(
                False,
                "BLOCKED",
                error_code="EMERGENCY_START_ATTEMPTS_EXHAUSTED",
                message="Maximale Anzahl Notfallstartversuche erreicht",
                attempt=self._failed_start_attempts,
                attempts_remaining=0,
            )
        deck, track_id, media_type, loop_enabled = self._prepared
        cue_in, effective_gain_db, clip_protection_enabled = self._prepared_safety
        loaded = deck.model.loaded_track
        if loaded is None or loaded.id != track_id:
            self._prepared = None
            return EmergencyPlaybackResult(
                False,
                "NOT_PREPARED",
                deck.model.deck_id,
                track_id,
                "EMERGENCY_PRELOAD_STALE",
                "Das vorgeladene Notfallmedium wurde zwischenzeitlich ersetzt",
            )
        attempt = self._failed_start_attempts + 1
        deadline = monotonic() + self._total_activation_timeout
        try:
            deck.set_transition_muted(True)
            self._run_timed(
                deck.play,
                self._bounded_timeout(deadline, self._playback_start_timeout),
                "EMERGENCY_START_TIMEOUT",
                "Notfallwiedergabe konnte nicht rechtzeitig gestartet werden",
            )
            confirmation_deadline = min(deadline, monotonic() + self._playback_confirmation)
            while monotonic() < confirmation_deadline:
                if deck.backend.is_playing():
                    break
                sleep(0.02)
            else:
                raise AudioRecoveryTimeoutError(
                    "EMERGENCY_CONFIRMATION_TIMEOUT",
                    "Notfallwiedergabe wurde nicht rechtzeitig bestätigt",
                )
        except (OSError, RuntimeError) as exc:
            self._failed_start_attempts += 1
            self._state.set_deck_health(deck.model.deck_id, DeckHealth.FAILED, str(exc))
            self._state.transition(EmergencySystemState.DEGRADED, "Notfallstart fehlgeschlagen")
            return EmergencyPlaybackResult(
                False,
                "FAILED",
                deck.model.deck_id,
                track_id,
                (
                    exc.error_code
                    if isinstance(exc, AudioRecoveryTimeoutError)
                    else "EMERGENCY_START_FAILED"
                ),
                str(exc),
                self._failed_start_attempts,
                max(0, self._maximum_start_attempts - self._failed_start_attempts),
            )
        deck.set_emergency_muted(False)
        deck.set_transition_muted(False)
        self._failed_start_attempts = 0
        temporary = media_type in {
            EmergencyMediaType.JINGLE,
            EmergencyMediaType.ANNOUNCEMENT,
        }
        outgoing = self._start_safe_handover(deck, stop_outgoing=not temporary)
        if loop_enabled:
            self._start_loop_monitor(deck, track_id)
        elif temporary:
            self._start_temporary_return(deck, track_id, outgoing)
        self._state.transition(EmergencySystemState.EMERGENCY_ACTIVE, f"{media_type.value} läuft")
        result = EmergencyPlaybackResult(
            True,
            "PLAYING",
            deck.model.deck_id,
            track_id,
            attempt=attempt,
            attempts_remaining=self._maximum_start_attempts,
            cue_in=cue_in,
            effective_gain_db=effective_gain_db,
            clip_protection_enabled=clip_protection_enabled,
        )
        if self._playback_started is not None and deck.model.loaded_track is not None:
            track = deck.model.loaded_track

            def publish_started() -> None:
                try:
                    assert self._playback_started is not None
                    self._playback_started(result, media_type, track)
                except Exception:
                    self._logger.exception(
                        "Asynchrone Notfall-History konnte nicht eingeplant werden"
                    )

            Thread(
                target=publish_started,
                name="emergency-playback-history",
                daemon=True,
            ).start()
        return result

    def _materialize_silent_primary(self) -> EmergencyPlaybackResult:
        cached = self._silent_primary
        if cached is None:
            return EmergencyPlaybackResult(False, "NOT_PREPARED")
        track, prepared, _source = cached
        snapshot = self._state.snapshot()
        health = {"A": snapshot.deck_a, "B": snapshot.deck_b}
        deck = next(
            (
                candidate
                for candidate in self._decks
                if health[candidate.model.deck_id] == DeckHealth.HEALTHY
                and not candidate.backend.is_playing()
                and candidate.model.state != DeckState.PLAYING
            ),
            None,
        )
        if deck is None:
            return EmergencyPlaybackResult(
                False,
                "NO_HEALTHY_DECK",
                error_code="NO_HEALTHY_INACTIVE_DECK",
                message="Kein gesundes inaktives Deck verfügbar",
            )
        try:
            deck.set_transition_muted(True)
            deck.set_emergency_muted(True)
            deck.load_prepared(track, prepared)
            deck.set_transition_muted(True)
            deck.set_emergency_muted(True)
            safety = self._apply_audio_safety(
                deck, track, monotonic() + self._total_activation_timeout
            )
        except (OSError, RuntimeError, ValueError) as exc:
            self._silent_primary = None
            return EmergencyPlaybackResult(
                False,
                "FAILED",
                deck.model.deck_id,
                track.id,
                "EMERGENCY_LOAD_FAILED",
                str(exc),
            )
        self._silent_primary = None
        self._prepared = (deck, track.id, EmergencyMediaType.PRIMARY, False)
        self._prepared_safety = safety
        return EmergencyPlaybackResult(
            True,
            "PREPARED",
            deck.model.deck_id,
            track.id,
            cue_in=safety[0],
            effective_gain_db=safety[1],
            clip_protection_enabled=safety[2],
        )

    def _apply_audio_safety(
        self, deck: DeckController, track: Track, deadline: float
    ) -> tuple[float, float, bool]:
        duration = max(0.0, float(track.duration_seconds or 0.0))
        boundaries = (
            self._cue_provider(track)
            if self._cue_provider is not None
            else ResolvedTrackBoundaries(
                0.0,
                duration,
                0.0,
                "FILE_BOUNDARY",
                "FILE_BOUNDARY",
                "EMERGENCY_DEFAULT",
            )
        )
        cue_in = max(0.0, min(boundaries.cue_in, duration))
        deck.model.cue_in = cue_in
        deck.model.cue_out = max(cue_in, boundaries.cue_out)
        deck.model.cue_in_source = boundaries.cue_in_source
        deck.model.cue_out_source = boundaries.cue_out_source
        self._run_timed(
            lambda: deck.seek(cue_in),
            self._bounded_timeout(deadline, self._media_load_timeout),
            "EMERGENCY_CUE_SEEK_TIMEOUT",
            "Cue In konnte nicht rechtzeitig angewendet werden",
        )
        resolved = (
            self._loudness_provider(track)
            if self._loudness_provider is not None
            else ResolvedLoudnessSettings(
                0.0,
                0.0,
                1.0,
                "EMERGENCY_DEFAULT",
                False,
                "TRACK",
                True,
                -1.0,
            )
        )
        safe_loudness = replace(
            resolved,
            runtime_clip_protection_enabled=True,
            output_peak_ceiling_dbfs=min(-1.0, resolved.output_peak_ceiling_dbfs),
        )
        runtime_clip_supported = (
            isinstance(deck.backend, RuntimeClipProtectionBackend)
            and deck.backend.supports_runtime_clip_protection()
        )
        if not runtime_clip_supported and safe_loudness.effective_gain_db > 0.0:
            safe_loudness = replace(
                safe_loudness,
                effective_gain_db=0.0,
                linear_gain_factor=1.0,
                peak_limited=True,
            )
        deck.set_resolved_loudness(safe_loudness)
        return (
            cue_in,
            safe_loudness.effective_gain_db,
            safe_loudness.runtime_clip_protection_enabled,
        )

    def _start_safe_handover(
        self, emergency_deck: DeckController, *, stop_outgoing: bool = True
    ) -> DeckController:
        """Move audio first, then stop the unchanged outgoing track."""
        with self._handover_lock:
            self._handover_generation += 1
            generation = self._handover_generation
        target = 0.0 if emergency_deck.model.deck_id == "A" else 1.0
        outgoing = next(deck for deck in self._decks if deck is not emergency_deck)
        outgoing_track = outgoing.model.loaded_track
        start = self._crossfader.position
        duration = self._handover_duration

        def ramp() -> None:
            started = monotonic()
            while True:
                with self._handover_lock:
                    if generation != self._handover_generation:
                        return
                progress = min(1.0, (monotonic() - started) / duration)
                eased = progress * progress * (3.0 - 2.0 * progress)
                self._crossfader.set_position(start + (target - start) * eased)
                if progress >= 1.0:
                    if (
                        stop_outgoing
                        and outgoing_track is not None
                        and outgoing.model.loaded_track is outgoing_track
                        and outgoing.backend.is_playing()
                    ):
                        outgoing.stop()
                    return
                sleep(0.02)

        Thread(target=ramp, name="emergency-handover", daemon=True).start()
        return outgoing

    def _start_immediate_fade_in(self, deck: DeckController) -> None:
        duration = min(0.5, self._handover_duration)

        def ramp() -> None:
            started = monotonic()
            while True:
                progress = min(1.0, (monotonic() - started) / duration)
                deck.set_fade_level_immediately(progress)
                if progress >= 1.0:
                    return
                sleep(0.02)

        Thread(target=ramp, name="emergency-immediate-fade-in", daemon=True).start()

    def _start_loop_monitor(self, deck: DeckController, track_id: int) -> None:
        generation = self._handover_generation

        def monitor() -> None:
            while generation == self._handover_generation:
                prepared = self._prepared
                if prepared is None or prepared[0] is not deck or prepared[1] != track_id:
                    return
                if deck.backend.is_finished():
                    deck.seek(0.0)
                    deck.play()
                sleep(0.05)

        Thread(target=monitor, name="emergency-break-loop", daemon=True).start()

    def _start_temporary_return(
        self, deck: DeckController, track_id: int, outgoing: DeckController
    ) -> None:
        generation = self._handover_generation
        target = 0.0 if outgoing.model.deck_id == "A" else 1.0

        def monitor() -> None:
            while generation == self._handover_generation:
                prepared = self._prepared
                if prepared is None or prepared[0] is not deck or prepared[1] != track_id:
                    return
                if deck.backend.is_finished():
                    start = self._crossfader.position
                    started = monotonic()
                    while generation == self._handover_generation:
                        progress = min(1.0, (monotonic() - started) / self._handover_duration)
                        self._crossfader.set_position(start + (target - start) * progress)
                        if progress >= 1.0:
                            deck.stop()
                            return
                        sleep(0.02)
                    return
                sleep(0.05)

        Thread(target=monitor, name="emergency-temporary-return", daemon=True).start()

    def _bounded_timeout(self, deadline: float, operation_timeout: float) -> float:
        remaining = deadline - monotonic()
        if remaining <= 0.0:
            raise AudioRecoveryTimeoutError(
                "EMERGENCY_TOTAL_TIMEOUT", "Notfallaktion hat ihr Zeitlimit überschritten"
            )
        return min(operation_timeout, remaining)

    @staticmethod
    def _run_timed(
        operation: Callable[[], object],
        timeout_seconds: float,
        error_code: str,
        message: str,
    ) -> object:
        completed = Event()
        guard = Lock()
        state: dict[str, object] = {"status": "running"}

        def worker() -> None:
            try:
                value = operation()
            except BaseException as exc:
                with guard:
                    state["error"] = exc
                    state["status"] = "completed"
                completed.set()
                return
            with guard:
                state["value"] = value
                state["status"] = "completed"
            completed.set()

        Thread(target=worker, name=f"emergency-{error_code.lower()}", daemon=True).start()
        completed.wait(timeout_seconds)
        with guard:
            if state["status"] != "completed":
                state["status"] = "timed_out"
                raise AudioRecoveryTimeoutError(error_code, message)
            error = state.get("error")
            value = state.get("value")
        if isinstance(error, BaseException):
            raise error
        return value
