"""Progress-based deck health monitoring independent from playback control."""

from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from collections.abc import Callable
import logging

from party_player.deck_controller import DeckController
from party_player.emergency_state import DeckHealth, EmergencyStateService, EmergencySystemState
from party_player.enums import DeckState


@dataclass(frozen=True, slots=True)
class DeckHealthObservation:
    deck_id: str
    health: DeckHealth
    reason: str
    stalled_seconds: float
    network_source: bool


@dataclass(slots=True)
class _DeckProgress:
    track_id: int | None = None
    position: float = 0.0
    last_progress_at: float = 0.0
    suspected_at: float | None = None
    consecutive_command_failures: int = 0
    backend_state: str = "UNKNOWN"
    last_health: DeckHealth | None = None
    stall_logged_at: float | None = None


class DeckHealthMonitor:
    """Detect a confirmed stall only when expected playback stops progressing."""

    def __init__(
        self,
        state: EmergencyStateService,
        *,
        local_stall_seconds: float = 4.0,
        network_stall_seconds: float = 10.0,
        confirmation_seconds: float = 2.0,
        progress_epsilon_seconds: float = 0.05,
        command_failure_threshold: int = 3,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._state = state
        self._local_timeout = max(0.1, local_stall_seconds)
        self._network_timeout = max(self._local_timeout, network_stall_seconds)
        self._confirmation = max(0.1, confirmation_seconds)
        self._epsilon = max(0.001, progress_epsilon_seconds)
        self._command_failure_threshold = max(1, command_failure_threshold)
        self._clock = clock
        self._logger = logging.getLogger(__name__)
        self._diagnostic_context: Callable[[str], dict[str, object]] = lambda _deck_id: {}
        self._progress = {"A": _DeckProgress(), "B": _DeckProgress()}
        self._output_device_failure_health: dict[str, DeckHealth] | None = None

    def set_diagnostic_context_provider(self, provider: Callable[[str], dict[str, object]]) -> None:
        self._diagnostic_context = provider

    def bind(self, deck: DeckController) -> None:
        """Receive all load and transport outcomes from a deck."""
        deck_id = deck.model.deck_id
        deck.set_command_result_callback(
            lambda command, succeeded, detail: self.report_command_result(
                deck_id, command, succeeded, detail
            )
        )

    def report_command_result(
        self, deck_id: str, command: str, succeeded: bool, detail: str = ""
    ) -> None:
        """Escalate consecutive backend command failures, not isolated glitches."""
        progress = self._progress[deck_id]
        if succeeded:
            progress.consecutive_command_failures = 0
            return
        progress.consecutive_command_failures += 1
        count = progress.consecutive_command_failures
        reason = f"Audiobefehl {command} fehlgeschlagen ({count}×)"
        if detail:
            reason = f"{reason}: {detail}"
        if count >= self._command_failure_threshold:
            self._state.set_deck_health(deck_id, DeckHealth.FAILED, reason)
            self._raise_degraded(reason)
        elif count >= 2:
            self._state.set_deck_health(deck_id, DeckHealth.SUSPECTED_STALL, reason)
            self._raise_warning(reason)

    def report_output_device(
        self, configured_device_id: str, available_device_ids: set[str]
    ) -> bool:
        """Record whether an explicitly configured shared output still exists."""
        configured = configured_device_id.strip()
        if not configured:
            return True
        if configured in available_device_ids:
            return True
        reason = "Konfiguriertes Audiogerät ist nicht verfügbar"
        if self._output_device_failure_health is None:
            snapshot = self._state.snapshot()
            self._output_device_failure_health = {
                "A": snapshot.deck_a,
                "B": snapshot.deck_b,
            }
        for deck_id in self._progress:
            self._state.set_deck_health(deck_id, DeckHealth.FAILED, reason)
        self._raise_degraded(reason)
        return False

    def confirm_output_device_recovered(self) -> None:
        """Restore health captured before device loss after operator confirmation."""
        previous = self._output_device_failure_health
        if previous is None:
            return
        self._output_device_failure_health = None
        for deck_id, health in previous.items():
            self._state.set_deck_health(
                deck_id, health, "Audiogerät wiederhergestellt und bestätigt"
            )
        snapshot = self._state.snapshot()
        if (
            snapshot.system == EmergencySystemState.DEGRADED
            and snapshot.deck_a == DeckHealth.HEALTHY
            and snapshot.deck_b == DeckHealth.HEALTHY
        ):
            self._state.transition(EmergencySystemState.RECOVERING, "Audiogerät wird bestätigt")
            self._state.transition(
                EmergencySystemState.NORMAL, "Audiogerät wiederhergestellt und bestätigt"
            )

    def observe(self, deck: DeckController) -> DeckHealthObservation:
        now = self._clock()
        deck_id = deck.model.deck_id
        progress = self._progress[deck_id]
        track = deck.model.loaded_track
        track_id = track.id if track is not None else None
        network = self._is_network_path(track.file_path if track is not None else "")
        backend_state = deck.model.backend_state
        if backend_state != progress.backend_state:
            self._logger.info(
                "audio.vlc_state deck=%s previous=%s current=%s expected=%s context=%s",
                deck_id,
                progress.backend_state,
                backend_state,
                deck.model.state.value,
                self._diagnostic_context(deck_id),
            )
            progress.backend_state = backend_state
        if deck.model.state == DeckState.PLAYING and backend_state in {
            "BUFFERING",
            "PAUSED",
            "STOPPED",
            "ERROR",
        }:
            self._logger.warning(
                "audio.vlc_unexpected_state deck=%s state=%s position=%.3f context=%s",
                deck_id,
                backend_state,
                deck.model.position,
                self._diagnostic_context(deck_id),
            )

        if deck.model.state == DeckState.ERROR:
            return self._set_failed(deck_id, deck.model.error_message or "Deckfehler", network)
        if deck.model.state != DeckState.PLAYING:
            self._reset_progress(progress, track_id, deck.model.position, now)
            current = self._health(deck_id)
            if current != DeckHealth.FAILED:
                self._state.set_deck_health(
                    deck_id, DeckHealth.HEALTHY, "Keine Wiedergabe erwartet"
                )
                current = DeckHealth.HEALTHY
                self._normalize_warning_if_healthy()
            return DeckHealthObservation(
                deck_id, current, "Keine Wiedergabe erwartet", 0.0, network
            )

        if progress.track_id != track_id:
            self._reset_progress(progress, track_id, deck.model.position, now)
            self._state.set_deck_health(deck_id, DeckHealth.BUFFERING, "Wiedergabestart")
            return DeckHealthObservation(
                deck_id, DeckHealth.BUFFERING, "Wiedergabestart", 0.0, network
            )

        if deck.model.position >= progress.position + self._epsilon:
            elapsed = max(0.0, now - progress.last_progress_at)
            delta = deck.model.position - progress.position
            if elapsed > 0 and delta > max(2.0, elapsed * 3.0):
                self._logger.warning(
                    "audio.position_jump deck=%s delta=%.3f elapsed=%.3f position=%.3f context=%s",
                    deck_id,
                    delta,
                    elapsed,
                    deck.model.position,
                    self._diagnostic_context(deck_id),
                )
            if progress.stall_logged_at is not None:
                self._logger.warning(
                    "audio.stall_recovered deck=%s duration=%.3f position=%.3f context=%s",
                    deck_id,
                    now - progress.stall_logged_at,
                    deck.model.position,
                    self._diagnostic_context(deck_id),
                )
            self._reset_progress(progress, track_id, deck.model.position, now)
            self._state.set_deck_health(deck_id, DeckHealth.HEALTHY, "Position läuft fort")
            self._normalize_warning_if_healthy()
            return DeckHealthObservation(
                deck_id, DeckHealth.HEALTHY, "Position läuft fort", 0.0, network
            )

        stalled_for = max(0.0, now - progress.last_progress_at)
        timeout = self._network_timeout if network else self._local_timeout
        try:
            backend_playing = deck.backend.is_playing()
        except Exception as exc:
            return self._set_failed(deck_id, f"Backend-Abfrage fehlgeschlagen: {exc}", network)
        reason = (
            "Position ohne Fortschritt" if backend_playing else "Backend meldet keine Wiedergabe"
        )
        if stalled_for < timeout:
            self._state.set_deck_health(deck_id, DeckHealth.BUFFERING, reason)
            return DeckHealthObservation(
                deck_id, DeckHealth.BUFFERING, reason, stalled_for, network
            )
        if progress.suspected_at is None:
            progress.suspected_at = now
            progress.stall_logged_at = now
            self._logger.warning(
                "audio.stall_suspected deck=%s stalled_seconds=%.3f position=%.3f "
                "backend_state=%s network_source=%s context=%s",
                deck_id,
                stalled_for,
                deck.model.position,
                backend_state,
                network,
                self._diagnostic_context(deck_id),
            )
            self._state.set_deck_health(deck_id, DeckHealth.SUSPECTED_STALL, reason)
            self._raise_warning(reason)
            return DeckHealthObservation(
                deck_id, DeckHealth.SUSPECTED_STALL, reason, stalled_for, network
            )
        if now - progress.suspected_at < self._confirmation:
            return DeckHealthObservation(
                deck_id, DeckHealth.SUSPECTED_STALL, reason, stalled_for, network
            )
        self._state.set_deck_health(deck_id, DeckHealth.STALLED, reason)
        self._raise_degraded(reason)
        return DeckHealthObservation(deck_id, DeckHealth.STALLED, reason, stalled_for, network)

    @staticmethod
    def _is_network_path(file_path: str) -> bool:
        normalized = str(Path(file_path)) if file_path else ""
        return normalized.startswith(("\\\\", "//"))

    @staticmethod
    def _reset_progress(
        progress: _DeckProgress,
        track_id: int | None,
        position: float,
        now: float,
    ) -> None:
        progress.track_id = track_id
        progress.position = position
        progress.last_progress_at = now
        progress.suspected_at = None
        progress.stall_logged_at = None

    def _health(self, deck_id: str) -> DeckHealth:
        snapshot = self._state.snapshot()
        return snapshot.deck_a if deck_id == "A" else snapshot.deck_b

    def _set_failed(self, deck_id: str, reason: str, network: bool) -> DeckHealthObservation:
        self._state.set_deck_health(deck_id, DeckHealth.FAILED, reason)
        self._raise_degraded(reason)
        return DeckHealthObservation(deck_id, DeckHealth.FAILED, reason, 0.0, network)

    def _raise_warning(self, reason: str) -> None:
        if self._state.snapshot().system == EmergencySystemState.NORMAL:
            self._state.transition(EmergencySystemState.WARNING, reason)

    def _raise_degraded(self, reason: str) -> None:
        system = self._state.snapshot().system
        if system == EmergencySystemState.NORMAL:
            self._state.transition(EmergencySystemState.WARNING, reason)
            system = EmergencySystemState.WARNING
        if system == EmergencySystemState.WARNING:
            self._state.transition(EmergencySystemState.DEGRADED, reason)

    def _normalize_warning_if_healthy(self) -> None:
        snapshot = self._state.snapshot()
        if (
            snapshot.system == EmergencySystemState.WARNING
            and snapshot.deck_a == DeckHealth.HEALTHY
            and snapshot.deck_b == DeckHealth.HEALTHY
        ):
            self._state.transition(EmergencySystemState.NORMAL, "Decks wieder gesund")
