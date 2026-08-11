"""Isolated recovery of one deck without disturbing healthy playback."""

from collections.abc import Callable
from copy import copy
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from pathlib import Path
from threading import Lock
from threading import Event, Thread
from time import monotonic, sleep
from typing import cast

from party_player.audio.base import AudioBackend
from party_player.audio.factory import AudioBackendFactory
from party_player.deck_controller import DeckController
from party_player.emergency_state import DeckHealth, EmergencyStateService, EmergencySystemState
from party_player.enums import DeckState
from party_player.models import Deck, Track
from party_player.recovery_escalation import (
    GlobalRecoveryAssessment,
    GlobalRecoveryContext,
    GlobalRecoveryTrigger,
    RecoveryEscalationPolicy,
)


class AudioRecoveryPolicy(StrEnum):
    RESUME_POSITION = "RESUME_POSITION"
    RESTART_TRACK = "RESTART_TRACK"
    SKIP_TRACK = "SKIP_TRACK"
    LOAD_EMERGENCY = "LOAD_EMERGENCY"


class AudioRecoveryTimeoutError(TimeoutError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True, slots=True)
class DeckRestartAssessment:
    allowed: bool
    error_code: str = ""
    message: str = ""


@dataclass(frozen=True, slots=True)
class AudioRecoveryResult:
    success: bool
    state: str
    deck_id: str
    error_code: str = ""
    message: str = ""
    track_id: int | None = None
    restored_position: float = 0.0
    playback_confirmed: bool = False
    policy: AudioRecoveryPolicy = AudioRecoveryPolicy.RESUME_POSITION
    attempt: int = 0
    attempts_remaining: int = 0


@dataclass(frozen=True, slots=True)
class GlobalAudioRecoveryResult:
    success: bool
    state: str
    error_code: str = ""
    message: str = ""
    attempt: int = 0
    attempts_remaining: int = 0
    recovered_decks: tuple[str, ...] = ()


BackendFactory = Callable[[], AudioBackend]
AuditRecorder = Callable[[str, dict[str, object]], None]


class AudioRecoveryService:
    """Own deck recovery and its resource-safety decision."""

    def __init__(
        self,
        state: EmergencyStateService,
        deck_a: DeckController,
        deck_b: DeckController,
        backend_factories: dict[str, BackendFactory] | AudioBackendFactory,
        *,
        independent_players: bool = False,
        preserves_shared_instance: bool = False,
        preserves_output_device: bool = False,
        global_reinitialization_active: Callable[[], bool] = lambda: False,
        audit: AuditRecorder | None = None,
        resume_safety_offset_seconds: float = 1.0,
        playback_confirmation_seconds: float = 1.0,
        emergency_track_provider: Callable[[], Track | None] | None = None,
        maximum_attempts_per_deck: int = 3,
        maximum_global_attempts: int = 2,
        player_creation_timeout_seconds: float = 3.0,
        media_load_timeout_seconds: float = 8.0,
        backend_release_timeout_seconds: float = 2.0,
        total_recovery_timeout_seconds: float = 15.0,
        escalation_policy: RecoveryEscalationPolicy | None = None,
    ) -> None:
        self._state = state
        self._decks = {"A": deck_a, "B": deck_b}
        self._backend_factories: dict[str, BackendFactory]
        self._audio_backend_factory: AudioBackendFactory | None = None
        if isinstance(backend_factories, AudioBackendFactory):
            self._audio_backend_factory = backend_factories
            lifecycle = backend_factories.lifecycle()
            self._backend_factories = {
                deck_id: partial(backend_factories.create_deck_backend, deck_id)
                for deck_id in self._decks
            }
            self._independent_players = lifecycle.independent_players
            self._preserves_shared_instance = lifecycle.preserves_shared_resource_on_deck_close
            self._preserves_output_device = lifecycle.preserves_output_device_on_deck_restart
        else:
            self._backend_factories = backend_factories
            self._independent_players = independent_players
            self._preserves_shared_instance = preserves_shared_instance
            self._preserves_output_device = preserves_output_device
        self._global_reinitialization_active = global_reinitialization_active
        self._audit = audit
        self._resume_safety_offset = max(0.0, resume_safety_offset_seconds)
        self._playback_confirmation = max(0.1, playback_confirmation_seconds)
        self._emergency_track_provider = emergency_track_provider
        self._maximum_attempts = max(1, maximum_attempts_per_deck)
        self._failed_attempts = {"A": 0, "B": 0}
        self._maximum_global_attempts = max(1, maximum_global_attempts)
        self._failed_global_attempts = 0
        self._player_creation_timeout = max(0.05, player_creation_timeout_seconds)
        self._media_load_timeout = max(0.05, media_load_timeout_seconds)
        self._backend_release_timeout = max(0.05, backend_release_timeout_seconds)
        self._total_recovery_timeout = max(0.1, total_recovery_timeout_seconds)
        self._deck_locks = {"A": Lock(), "B": Lock()}
        self._global_backend_lock = Lock()
        self._recovery_state_lock = Lock()
        self._active_deck_recoveries: set[str] = set()
        self._deck_recovery_origin = EmergencySystemState.NORMAL
        self._deck_recovery_failure_pending = False
        self._escalation_policy = escalation_policy or RecoveryEscalationPolicy()

    def can_restart_deck_independently(self, deck_id: str) -> DeckRestartAssessment:
        normalized = deck_id.upper()
        if normalized not in self._decks:
            return DeckRestartAssessment(False, "UNKNOWN_DECK", "Unbekanntes Deck")
        if self._global_reinitialization_active() or self._global_backend_lock.locked():
            return DeckRestartAssessment(
                False, "GLOBAL_RECOVERY_ACTIVE", "Globale Audio-Reparatur läuft"
            )
        if not self._independent_players:
            return DeckRestartAssessment(
                False, "PLAYERS_NOT_INDEPENDENT", "Decks verwenden denselben Player"
            )
        if not self._preserves_shared_instance:
            return DeckRestartAssessment(
                False,
                "SHARED_INSTANCE_NOT_PRESERVED",
                "Einzelreparatur würde die gemeinsame VLC-Instanz ersetzen",
            )
        if not self._preserves_output_device:
            return DeckRestartAssessment(
                False,
                "OUTPUT_DEVICE_NOT_PRESERVED",
                "Einzelreparatur würde das gemeinsame Ausgabegerät verändern",
            )
        if normalized not in self._backend_factories:
            return DeckRestartAssessment(
                False, "BACKEND_FACTORY_MISSING", "Kein Ersatz-Backend konfiguriert"
            )
        return DeckRestartAssessment(True)

    def recovery_active(self) -> bool:
        """Expose lock state without granting callers control over recovery locks."""
        return self._global_backend_lock.locked() or any(
            lock.locked() for lock in self._deck_locks.values()
        )

    def assess_global_recovery(self, trigger: GlobalRecoveryTrigger) -> GlobalRecoveryAssessment:
        snapshot = self._state.snapshot()
        health = {"A": snapshot.deck_a, "B": snapshot.deck_b}
        healthy_ids = {deck_id for deck_id, value in health.items() if value == DeckHealth.HEALTHY}
        context = GlobalRecoveryContext(
            healthy_deck_playing=any(
                self._decks[deck_id].backend.is_playing() for deck_id in healthy_ids
            ),
            emergency_playback_can_be_prepared=(
                self._emergency_track_provider is not None
                and self._emergency_track_provider() is not None
                and bool(healthy_ids)
            ),
            stable_one_deck_mode_possible=bool(healthy_ids),
        )
        return self._escalation_policy.assess_global_recovery(trigger, context)

    def recover_all_backends(
        self, trigger: GlobalRecoveryTrigger = GlobalRecoveryTrigger.AUTOMATIC
    ) -> GlobalAudioRecoveryResult:
        """Explicitly replace both deck backends after fully muted preparation."""
        assessment = self.assess_global_recovery(trigger)
        if self._audit is not None:
            self._audit(
                "AUDIO_GLOBAL_RECOVERY_ASSESSED",
                {
                    "allowed": assessment.allowed,
                    "trigger": assessment.trigger.value,
                    "next_stage": assessment.next_stage.value,
                    "error_code": assessment.error_code,
                    "message": assessment.message,
                },
            )
        if not assessment.allowed:
            return self._finish_global(
                GlobalAudioRecoveryResult(
                    False, "BLOCKED", assessment.error_code, assessment.message
                )
            )
        if self._failed_global_attempts >= self._maximum_global_attempts:
            return self._finish_global(
                GlobalAudioRecoveryResult(
                    False,
                    "BLOCKED",
                    "GLOBAL_RECOVERY_ATTEMPTS_EXHAUSTED",
                    "Maximale Anzahl globaler Reparaturversuche erreicht",
                    attempt=self._failed_global_attempts,
                )
            )
        if not self._global_backend_lock.acquire(blocking=False):
            return self._finish_global(
                GlobalAudioRecoveryResult(
                    False, "BUSY", "GLOBAL_RECOVERY_ACTIVE", "Globale Reparatur läuft"
                )
            )
        acquired_deck_locks: list[Lock] = []
        replacements: dict[str, AudioBackend] = {}
        committed = False
        try:
            for lock in self._deck_locks.values():
                if not lock.acquire(blocking=False):
                    return self._finish_global(
                        GlobalAudioRecoveryResult(
                            False,
                            "BUSY",
                            "DECK_RECOVERY_ACTIVE",
                            "Eine isolierte Deck-Reparatur läuft bereits",
                        )
                    )
                acquired_deck_locks.append(lock)
            missing = sorted(set(self._decks) - set(self._backend_factories))
            if missing:
                return self._finish_global(
                    GlobalAudioRecoveryResult(
                        False,
                        "BLOCKED",
                        "BACKEND_FACTORY_MISSING",
                        f"Kein Ersatz-Backend für Deck {', '.join(missing)} konfiguriert",
                    )
                )
            before = self._state.snapshot()
            previous_health = {"A": before.deck_a, "B": before.deck_b}
            attempt = self._failed_global_attempts + 1
            deadline = monotonic() + self._total_recovery_timeout
            if before.system == EmergencySystemState.NORMAL:
                self._state.transition(
                    EmergencySystemState.WARNING, "Globale Audio-Reparatur angefordert"
                )
            if self._state.snapshot().system != EmergencySystemState.RECOVERING:
                self._state.transition(EmergencySystemState.RECOVERING, "Globale Audio-Reparatur")
            for deck_id in self._decks:
                self._state.set_deck_health(
                    deck_id, DeckHealth.RECOVERING, "Globale Backend-Erneuerung"
                )
            models = {deck_id: copy(deck.model) for deck_id, deck in self._decks.items()}
            gains = {deck_id: deck.normalization_factor for deck_id, deck in self._decks.items()}
            try:
                for deck_id in self._decks:
                    replacement = cast(
                        AudioBackend,
                        self._run_timed(
                            self._backend_factories[deck_id],
                            self._bounded_timeout(deadline, self._player_creation_timeout),
                            "GLOBAL_PLAYER_CREATION_TIMEOUT",
                            f"Ersatzplayer für Deck {deck_id} wurde nicht rechtzeitig erzeugt",
                            late_cleanup=self._close_backend_safely,
                        ),
                    )
                    replacements[deck_id] = replacement
                    replacement.set_volume(0.0)
                    model = models[deck_id]
                    track = model.loaded_track
                    if track is not None:
                        safe_position = self._safe_resume_position(model)
                        self._run_timed(
                            partial(replacement.load, Path(track.file_path)),
                            self._bounded_timeout(deadline, self._media_load_timeout),
                            "GLOBAL_MEDIA_LOAD_TIMEOUT",
                            f"Titel auf Deck {deck_id} wurde nicht rechtzeitig geladen",
                        )
                        self._run_timed(
                            partial(replacement.seek, safe_position),
                            self._bounded_timeout(deadline, self._media_load_timeout),
                            "GLOBAL_SEEK_TIMEOUT",
                            f"Position auf Deck {deck_id} wurde nicht rechtzeitig gesetzt",
                        )
                        model.position = safe_position
                        if model.state == DeckState.PLAYING:
                            self._run_timed(
                                replacement.play,
                                self._bounded_timeout(deadline, self._playback_confirmation),
                                "GLOBAL_PLAYBACK_START_TIMEOUT",
                                f"Deck {deck_id} wurde nicht rechtzeitig gestartet",
                            )
                            if not self._confirm_playback(replacement, safe_position):
                                raise AudioRecoveryTimeoutError(
                                    "GLOBAL_PLAYBACK_CONFIRMATION_TIMEOUT",
                                    f"Deck {deck_id} bestätigte die Wiedergabe nicht",
                                )
            except Exception as exc:
                self._failed_global_attempts = attempt
                for deck_id, health in previous_health.items():
                    self._state.set_deck_health(deck_id, health, "Globale Reparatur verworfen")
                self._state.transition(EmergencySystemState.RECOVERY_FAILED, str(exc))
                return self._finish_global(
                    GlobalAudioRecoveryResult(
                        False,
                        "FAILED",
                        (
                            exc.error_code
                            if isinstance(exc, AudioRecoveryTimeoutError)
                            else "GLOBAL_RECOVERY_FAILED"
                        ),
                        str(exc),
                        attempt,
                        max(0, self._maximum_global_attempts - attempt),
                    )
                )
            previous_backends: list[AudioBackend] = []
            for deck_id, deck in self._decks.items():
                previous_backends.append(
                    deck.commit_recovered_backend(
                        replacements[deck_id], models[deck_id], normalization_factor=gains[deck_id]
                    )
                )
                deck.set_emergency_muted(True)
                self._state.set_deck_health(deck_id, DeckHealth.HEALTHY, "Backend ersetzt")
            committed = True
            cleanup_pending = False
            for backend in previous_backends:
                try:
                    self._run_timed(
                        backend.close,
                        self._bounded_timeout(deadline, self._backend_release_timeout),
                        "GLOBAL_BACKEND_RELEASE_TIMEOUT",
                        "Altes Backend wurde nicht rechtzeitig freigegeben",
                    )
                except Exception:
                    cleanup_pending = True
            self._failed_global_attempts = 0
            self._state.transition(EmergencySystemState.NORMAL, "Globale Reparatur abgeschlossen")
            return self._finish_global(
                GlobalAudioRecoveryResult(
                    True,
                    "RECOVERED_CLEANUP_PENDING" if cleanup_pending else "RECOVERED_MUTED",
                    "GLOBAL_BACKEND_RELEASE_TIMEOUT" if cleanup_pending else "",
                    "Ersatzbackends aktiv; Ausgabe bleibt bis zur Freigabe stumm",
                    attempt,
                    self._maximum_global_attempts,
                    ("A", "B"),
                )
            )
        finally:
            if not committed:
                for replacement in replacements.values():
                    self._close_backend_safely(replacement)
            for lock in reversed(acquired_deck_locks):
                lock.release()
            self._global_backend_lock.release()

    def _finish_global(self, result: GlobalAudioRecoveryResult) -> GlobalAudioRecoveryResult:
        if self._audit is not None:
            self._audit(
                "AUDIO_GLOBAL_RECOVERY",
                {
                    "success": result.success,
                    "state": result.state,
                    "error_code": result.error_code,
                    "message": result.message,
                    "attempt": result.attempt,
                    "attempts_remaining": result.attempts_remaining,
                    "recovered_decks": result.recovered_decks,
                },
            )
        return result

    def recover_deck(
        self,
        deck_id: str,
        policy: AudioRecoveryPolicy = AudioRecoveryPolicy.RESUME_POSITION,
    ) -> AudioRecoveryResult:
        normalized = deck_id.upper()
        if normalized in self._failed_attempts and (
            self._failed_attempts[normalized] >= self._maximum_attempts
        ):
            return self._finish(
                AudioRecoveryResult(
                    False,
                    "BLOCKED",
                    normalized,
                    "RECOVERY_ATTEMPTS_EXHAUSTED",
                    "Maximale Anzahl isolierter Reparaturversuche erreicht",
                    policy=policy,
                    attempt=self._failed_attempts[normalized],
                    attempts_remaining=0,
                )
            )
        assessment = self.can_restart_deck_independently(normalized)
        if not assessment.allowed:
            return self._finish(
                AudioRecoveryResult(
                    False,
                    "BLOCKED",
                    normalized,
                    assessment.error_code,
                    assessment.message,
                    policy=policy,
                )
            )
        lock = self._deck_locks[normalized]
        if not lock.acquire(blocking=False):
            return self._finish(
                AudioRecoveryResult(
                    False,
                    "BUSY",
                    normalized,
                    "DECK_RECOVERY_ACTIVE",
                    "Dieses Deck wird bereits repariert",
                    policy=policy,
                )
            )
        try:
            self._begin_deck_recovery(normalized)
            attempt = self._failed_attempts[normalized] + 1
            recovery_deadline = monotonic() + self._total_recovery_timeout
            self._state.set_deck_health(normalized, DeckHealth.RECOVERING, "Backend-Neustart")
            try:
                shared_before = (
                    self._audio_backend_factory.lifecycle().shared_resource_identity
                    if self._audio_backend_factory is not None
                    else None
                )
                replacement = cast(
                    AudioBackend,
                    self._run_timed(
                        self._backend_factories[normalized],
                        self._bounded_timeout(recovery_deadline, self._player_creation_timeout),
                        "PLAYER_CREATION_TIMEOUT",
                        "Ersatzplayer konnte nicht rechtzeitig erzeugt werden",
                        late_cleanup=self._close_backend_safely,
                    ),
                )
                if self._audio_backend_factory is not None:
                    shared_after = self._audio_backend_factory.lifecycle().shared_resource_identity
                    if shared_before is not None and shared_after != shared_before:
                        raise AudioRecoveryTimeoutError(
                            "SHARED_RESOURCE_CHANGED_DURING_DECK_RECOVERY",
                            "Einzeldeck-Recovery hat die gemeinsame Audioressource verändert",
                        )
                deck = self._decks[normalized]
                restored_model = copy(deck.model)
                normalization_factor = deck.normalization_factor
                was_playing = restored_model.state == DeckState.PLAYING
                track, safe_position, should_play, restored_model = self._apply_policy(
                    policy, restored_model, was_playing
                )
                if policy in {
                    AudioRecoveryPolicy.SKIP_TRACK,
                    AudioRecoveryPolicy.LOAD_EMERGENCY,
                }:
                    normalization_factor = 1.0
                replacement.set_volume(0.0)
                if track is not None:
                    self._run_timed(
                        lambda: replacement.load(Path(track.file_path)),
                        self._bounded_timeout(recovery_deadline, self._media_load_timeout),
                        "MEDIA_LOAD_TIMEOUT",
                        "Titel konnte nicht rechtzeitig in den Ersatzplayer geladen werden",
                        late_cleanup=lambda _value: self._close_backend_safely(replacement),
                    )
                    self._run_timed(
                        lambda: replacement.seek(safe_position),
                        self._bounded_timeout(recovery_deadline, self._media_load_timeout),
                        "SEEK_TIMEOUT",
                        "Sichere Wiedergabeposition wurde nicht rechtzeitig angewendet",
                        late_cleanup=lambda _value: self._close_backend_safely(replacement),
                    )
                    if should_play:
                        self._run_timed(
                            replacement.play,
                            self._bounded_timeout(recovery_deadline, self._playback_confirmation),
                            "PLAYBACK_START_TIMEOUT",
                            "Ersatzplayer konnte nicht rechtzeitig gestartet werden",
                            late_cleanup=lambda _value: self._close_backend_safely(replacement),
                        )
                        if not self._confirm_playback(replacement, safe_position):
                            raise AudioRecoveryTimeoutError(
                                "PLAYBACK_CONFIRMATION_TIMEOUT",
                                "Ersatzplayer bestätigt Wiedergabe oder Seek-Position nicht",
                            )
                restored_model.position = safe_position
                previous = deck.commit_recovered_backend(
                    replacement,
                    restored_model,
                    normalization_factor=normalization_factor,
                )
                try:
                    self._run_timed(
                        previous.close,
                        self._bounded_timeout(recovery_deadline, self._backend_release_timeout),
                        "BACKEND_RELEASE_TIMEOUT",
                        "Altes Backend konnte nicht rechtzeitig gestoppt und freigegeben werden",
                    )
                except AudioRecoveryTimeoutError as exc:
                    self._state.set_deck_health(
                        normalized, DeckHealth.HEALTHY, "Ersatzbackend aktiv"
                    )
                    self._failed_attempts[normalized] = 0
                    self._complete_deck_recovery(
                        normalized,
                        success=True,
                        reason="Deck repariert; alte Backend-Freigabe läuft nach",
                    )
                    return self._finish(
                        AudioRecoveryResult(
                            True,
                            "RECOVERED_CLEANUP_PENDING",
                            normalized,
                            exc.error_code,
                            str(exc),
                            track_id=track.id if track is not None else None,
                            restored_position=safe_position,
                            playback_confirmed=should_play,
                            policy=policy,
                            attempt=attempt,
                            attempts_remaining=self._maximum_attempts,
                        )
                    )
            except Exception as exc:
                self._failed_attempts[normalized] = attempt
                operation_still_running = isinstance(
                    exc, AudioRecoveryTimeoutError
                ) and exc.error_code in {
                    "MEDIA_LOAD_TIMEOUT",
                    "SEEK_TIMEOUT",
                    "PLAYBACK_START_TIMEOUT",
                }
                if "replacement" in locals() and not operation_still_running:
                    try:
                        replacement.close()
                    except Exception:
                        pass
                self._state.set_deck_health(normalized, DeckHealth.FAILED, str(exc))
                self._complete_deck_recovery(normalized, success=False, reason=str(exc))
                error_code = (
                    exc.error_code
                    if isinstance(exc, AudioRecoveryTimeoutError)
                    else "DECK_RESTART_FAILED"
                )
                return self._finish(
                    AudioRecoveryResult(
                        False,
                        "FAILED",
                        normalized,
                        error_code,
                        str(exc),
                        policy=policy,
                        attempt=attempt,
                        attempts_remaining=max(0, self._maximum_attempts - attempt),
                    )
                )
            self._failed_attempts[normalized] = 0
            self._state.set_deck_health(normalized, DeckHealth.HEALTHY, "Backend ersetzt")
            self._complete_deck_recovery(
                normalized,
                success=True,
                reason="Isolierte Deck-Reparatur abgeschlossen",
            )
            return self._finish(
                AudioRecoveryResult(
                    True,
                    "RECOVERED",
                    normalized,
                    track_id=track.id if track is not None else None,
                    restored_position=safe_position,
                    playback_confirmed=should_play,
                    policy=policy,
                    attempt=attempt,
                    attempts_remaining=self._maximum_attempts,
                )
            )
        finally:
            with self._recovery_state_lock:
                still_registered = normalized in self._active_deck_recoveries
            if still_registered:
                self._complete_deck_recovery(
                    normalized,
                    success=False,
                    reason="Deck-Reparatur unerwartet beendet",
                )
            lock.release()

    def _begin_deck_recovery(self, deck_id: str) -> None:
        with self._recovery_state_lock:
            before = self._state.snapshot()
            if not self._active_deck_recoveries:
                self._deck_recovery_origin = before.system
                self._deck_recovery_failure_pending = False
                if before.system == EmergencySystemState.NORMAL:
                    self._state.transition(
                        EmergencySystemState.WARNING,
                        "Isolierte Deck-Reparatur angefordert",
                    )
                if self._state.snapshot().system != EmergencySystemState.RECOVERING:
                    self._state.transition(
                        EmergencySystemState.RECOVERING,
                        "Isolierte Deck-Reparatur",
                    )
            self._active_deck_recoveries.add(deck_id)

    def _complete_deck_recovery(self, deck_id: str, *, success: bool, reason: str) -> None:
        with self._recovery_state_lock:
            if deck_id not in self._active_deck_recoveries:
                return
            self._active_deck_recoveries.remove(deck_id)
            self._deck_recovery_failure_pending |= not success
            if self._active_deck_recoveries:
                return
            target = (
                EmergencySystemState.RECOVERY_FAILED
                if self._deck_recovery_failure_pending
                else (
                    EmergencySystemState.EMERGENCY_ACTIVE
                    if self._deck_recovery_origin == EmergencySystemState.EMERGENCY_ACTIVE
                    else EmergencySystemState.NORMAL
                )
            )
            self._state.transition(target, reason)

    def _finish(self, result: AudioRecoveryResult) -> AudioRecoveryResult:
        if self._audit is not None:
            self._audit(
                "AUDIO_DECK_RECOVERY",
                {
                    "success": result.success,
                    "state": result.state,
                    "deck_id": result.deck_id,
                    "error_code": result.error_code,
                    "message": result.message,
                    "policy": result.policy.value,
                    "attempt": result.attempt,
                    "attempts_remaining": result.attempts_remaining,
                },
            )
        return result

    def _bounded_timeout(self, deadline: float, operation_timeout: float) -> float:
        remaining = deadline - monotonic()
        if remaining <= 0.0:
            raise AudioRecoveryTimeoutError(
                "TOTAL_RECOVERY_TIMEOUT", "Gesamtrecovery hat ihr Zeitlimit überschritten"
            )
        return min(operation_timeout, remaining)

    def _run_timed(
        self,
        operation: Callable[[], object],
        timeout_seconds: float,
        error_code: str,
        message: str,
        *,
        late_cleanup: Callable[[object], None] | None = None,
    ) -> object:
        completed = Event()
        guard = Lock()
        state: dict[str, object] = {"status": "running"}

        def worker() -> None:
            try:
                value = operation()
            except BaseException as exc:
                cleanup = False
                with guard:
                    cleanup = state["status"] == "timed_out"
                    state["error"] = exc
                    state["status"] = "completed"
                completed.set()
                if cleanup and late_cleanup is not None:
                    late_cleanup(None)
                return
            cleanup = False
            with guard:
                cleanup = state["status"] == "timed_out"
                state["value"] = value
                state["status"] = "completed"
            completed.set()
            if cleanup and late_cleanup is not None:
                late_cleanup(value)

        Thread(target=worker, name=f"audio-recovery-{error_code.lower()}", daemon=True).start()
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

    @staticmethod
    def _close_backend_safely(value: object) -> None:
        close = getattr(value, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def _confirm_playback(self, backend: AudioBackend, expected_position: float) -> bool:
        deadline = monotonic() + self._playback_confirmation
        while monotonic() < deadline:
            position = backend.get_position()
            if (
                backend.is_playing()
                and position >= max(0.0, expected_position - 2.0)
                and position <= expected_position + 5.0
            ):
                return True
            sleep(0.02)
        return False

    def _apply_policy(
        self,
        policy: AudioRecoveryPolicy,
        restored_model: Deck,
        was_playing: bool,
    ) -> tuple[Track | None, float, bool, Deck]:
        if policy == AudioRecoveryPolicy.SKIP_TRACK:
            empty = Deck(deck_id=restored_model.deck_id, volume=restored_model.volume)
            return None, 0.0, False, empty
        if policy == AudioRecoveryPolicy.LOAD_EMERGENCY:
            track = (
                self._emergency_track_provider()
                if self._emergency_track_provider is not None
                else None
            )
            if track is None:
                raise RuntimeError("Kein geprüfter lokaler Notfalltitel verfügbar")
            emergency_model = Deck(
                deck_id=restored_model.deck_id,
                loaded_track=track,
                state=DeckState.PLAYING if was_playing else DeckState.LOADED,
                volume=restored_model.volume,
                duration=track.duration_seconds or 0.0,
            )
            return track, 0.0, was_playing, emergency_model
        track = restored_model.loaded_track
        if policy == AudioRecoveryPolicy.RESTART_TRACK:
            safe_position = max(0.0, restored_model.cue_in)
        else:
            safe_position = self._safe_resume_position(restored_model)
        return track, safe_position, was_playing, restored_model

    def _safe_resume_position(self, model: object) -> float:
        position = float(getattr(model, "position", 0.0))
        cue_in = max(0.0, float(getattr(model, "cue_in", 0.0)))
        duration = max(0.0, float(getattr(model, "duration", 0.0)))
        safe = max(cue_in, position - self._resume_safety_offset)
        if duration > 0.0:
            safe = min(safe, duration)
        return safe
