"""Coordination boundary for explicit emergency playback actions."""

from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock

from party_player.emergency_playback import EmergencyPlaybackResult, EmergencyPlaybackService
from party_player.emergency_playback import EmergencyHandoverStrategy
from party_player.emergency_playlist import EmergencyMediaType, EmergencyPlaylistValidation
from party_player.emergency_state import (
    DeckHealth,
    EmergencyStateService,
    EmergencyStateSnapshot,
    EmergencySystemState,
)
from party_player.audio_recovery import (
    AudioRecoveryPolicy,
    AudioRecoveryResult,
    AudioRecoveryService,
    DeckRestartAssessment,
    GlobalAudioRecoveryResult,
)
from party_player.recovery_escalation import GlobalRecoveryTrigger


class EmergencyController:
    """Serialize emergency commands and expose structured outcomes."""

    def __init__(
        self,
        playback: EmergencyPlaybackService,
        state: EmergencyStateService,
        audit: Callable[[str, dict[str, object]], None] | None = None,
        recovery: AudioRecoveryService | None = None,
    ) -> None:
        self._playback = playback
        self._state = state
        self._audit = audit
        self._recovery = recovery
        self._action_lock = Lock()
        self._last_result = EmergencyPlaybackResult(False, "IDLE")

    @property
    def last_result(self) -> EmergencyPlaybackResult:
        return self._last_result

    def snapshot(self) -> EmergencyStateSnapshot:
        return self._state.snapshot()

    def playlist_validation(self) -> EmergencyPlaylistValidation:
        return self._playback.playlist_validation()

    def prepare(self) -> EmergencyPlaybackResult:
        return self._run("EMERGENCY_PREPARE", self._playback.prepare_primary)

    def preload_primary_silently(self) -> EmergencyPlaybackResult:
        """Optionally preload at startup; this method can never activate playback."""
        return self._run("EMERGENCY_SILENT_PRELOAD", self._playback.preload_primary_silently)

    def activate(self) -> EmergencyPlaybackResult:
        return self._run("EMERGENCY_ACTIVATE", self._playback.activate_prepared)

    def play_media(
        self, media_type: EmergencyMediaType, *, loop: bool = False
    ) -> EmergencyPlaybackResult:
        """Prepare and start one typed local medium as one serialized action."""
        selected_type = EmergencyMediaType(media_type)

        def operation() -> EmergencyPlaybackResult:
            prepared = self._playback.prepare_media(selected_type, loop=loop)
            return self._playback.activate_prepared() if prepared.success else prepared

        return self._run(
            "EMERGENCY_MEDIA_PLAY",
            operation,
            extra={"media_type": selected_type.value, "loop": loop},
        )

    def immediate_replace(self, affected_deck_id: str) -> EmergencyPlaybackResult:
        """Hard-mute unacceptable output before serializing the replacement action."""
        normalized = affected_deck_id.upper()
        muted = self._playback.mute_deck_immediately(normalized)
        if not muted.success:
            return muted
        return self._run(
            "EMERGENCY_IMMEDIATE_REPLACE",
            lambda: self._playback.immediate_replace(normalized),
            extra={
                "strategy": EmergencyHandoverStrategy.IMMEDIATE_REPLACE.value,
                "affected_deck_id": normalized,
            },
        )

    def can_restart_deck_independently(self, deck_id: str) -> DeckRestartAssessment:
        if self._recovery is None:
            return DeckRestartAssessment(False, "RECOVERY_NOT_CONFIGURED")
        return self._recovery.can_restart_deck_independently(deck_id)

    def recovery_active(self) -> bool:
        return self._recovery is not None and self._recovery.recovery_active()

    def recover_deck(
        self,
        deck_id: str,
        policy: AudioRecoveryPolicy = AudioRecoveryPolicy.RESUME_POSITION,
    ) -> AudioRecoveryResult:
        if self._recovery is None:
            return AudioRecoveryResult(False, "BLOCKED", deck_id.upper(), "RECOVERY_NOT_CONFIGURED")
        result = self._recovery.recover_deck(deck_id, policy)
        self._playback.invalidate_prepared()
        return result

    def recover_all_audio_backends(self) -> GlobalAudioRecoveryResult:
        if self._recovery is None:
            return GlobalAudioRecoveryResult(
                False, "BLOCKED", "RECOVERY_NOT_CONFIGURED", "Recovery ist nicht konfiguriert"
            )
        result = self._recovery.recover_all_backends(GlobalRecoveryTrigger.OPERATOR_REQUEST)
        self._playback.invalidate_prepared()
        return result

    def report_transition_failure(
        self, outgoing_deck_id: str, incoming_deck_id: str, reason: str
    ) -> EmergencyStateSnapshot:
        """Record a failed handover independently from any repair decision."""
        outgoing = outgoing_deck_id.upper()
        incoming = incoming_deck_id.upper()
        self._state.set_deck_health(incoming, DeckHealth.FAILED, reason)
        system = self._state.snapshot().system
        if system == EmergencySystemState.NORMAL:
            self._state.transition(EmergencySystemState.WARNING, reason)
            system = EmergencySystemState.WARNING
        if system == EmergencySystemState.WARNING:
            self._state.transition(EmergencySystemState.DEGRADED, reason)
        if self._audit is not None:
            self._audit(
                "AUDIO_TRANSITION_FAILED",
                {
                    "outgoing_deck_id": outgoing,
                    "incoming_deck_id": incoming,
                    "reason": reason,
                },
            )
        return self._state.snapshot()

    def stabilize_failed_deck(self, deck_id: str) -> "EmergencyEscalationResult":
        """Confirm emergency audio before attempting an isolated deck restart."""
        normalized = deck_id.upper()
        if not self._action_lock.acquire(blocking=False):
            return EmergencyEscalationResult(
                False,
                "BUSY",
                normalized,
                error_code="EMERGENCY_ACTION_IN_PROGRESS",
                message="Eine Notfallaktion läuft bereits",
            )
        try:
            prepared = self._playback.prepare_primary()
            if not prepared.success:
                return self._finish_escalation(
                    EmergencyEscalationResult(
                        False,
                        "PREPARATION_FAILED",
                        normalized,
                        playback=prepared,
                        error_code=prepared.error_code,
                        message=prepared.message,
                    )
                )
            if prepared.deck_id == normalized:
                return self._finish_escalation(
                    EmergencyEscalationResult(
                        False,
                        "UNSAFE_TARGET",
                        normalized,
                        playback=prepared,
                        error_code="EMERGENCY_USES_FAILED_DECK",
                        message="Notfalltitel wurde auf dem zu reparierenden Deck vorbereitet",
                    )
                )
            activated = self._playback.activate_prepared()
            self._last_result = activated
            if not activated.success:
                return self._finish_escalation(
                    EmergencyEscalationResult(
                        False,
                        "ACTIVATION_FAILED",
                        normalized,
                        playback=activated,
                        error_code=activated.error_code,
                        message=activated.message,
                    )
                )
            if self._recovery is None:
                return self._finish_escalation(
                    EmergencyEscalationResult(
                        True,
                        "EMERGENCY_PLAYING_SINGLE_DECK",
                        normalized,
                        playback=activated,
                        error_code="RECOVERY_NOT_CONFIGURED",
                        message="Notfallwiedergabe läuft; Einzelreparatur ist nicht konfiguriert",
                    )
                )
            assessment = self._recovery.can_restart_deck_independently(normalized)
            if not assessment.allowed:
                return self._finish_escalation(
                    EmergencyEscalationResult(
                        True,
                        "EMERGENCY_PLAYING_SINGLE_DECK",
                        normalized,
                        playback=activated,
                        assessment=assessment,
                        error_code=assessment.error_code,
                        message=assessment.message,
                    )
                )
            recovery = self._recovery.recover_deck(normalized)
            return self._finish_escalation(
                EmergencyEscalationResult(
                    recovery.success,
                    "RECOVERED" if recovery.success else "RECOVERY_FAILED",
                    normalized,
                    playback=activated,
                    assessment=assessment,
                    recovery=recovery,
                    error_code=recovery.error_code,
                    message=recovery.message,
                )
            )
        finally:
            self._action_lock.release()

    def _finish_escalation(
        self, result: "EmergencyEscalationResult"
    ) -> "EmergencyEscalationResult":
        if self._audit is not None:
            self._audit(
                "EMERGENCY_ESCALATION",
                {
                    "success": result.success,
                    "state": result.state,
                    "failed_deck_id": result.failed_deck_id,
                    "emergency_deck_id": (
                        result.playback.deck_id if result.playback is not None else None
                    ),
                    "recovery_attempted": result.recovery is not None,
                    "error_code": result.error_code,
                    "message": result.message,
                },
            )
        return result

    def _run(
        self,
        event_code: str,
        operation: Callable[[], EmergencyPlaybackResult],
        *,
        extra: dict[str, object] | None = None,
    ) -> EmergencyPlaybackResult:
        if not self._action_lock.acquire(blocking=False):
            return EmergencyPlaybackResult(
                False,
                "BUSY",
                error_code="EMERGENCY_ACTION_IN_PROGRESS",
                message="Eine Notfallaktion läuft bereits",
            )
        try:
            result = operation()
            self._last_result = result
            if self._audit is not None:
                details: dict[str, object] = {
                    "success": result.success,
                    "state": result.state,
                    "deck_id": result.deck_id,
                    "track_id": result.track_id,
                    "error_code": result.error_code,
                    "message": result.message,
                    "attempt": result.attempt,
                    "attempts_remaining": result.attempts_remaining,
                    "cue_in": result.cue_in,
                    "effective_gain_db": result.effective_gain_db,
                    "clip_protection_enabled": result.clip_protection_enabled,
                }
                if extra is not None:
                    details.update(extra)
                self._audit(event_code, details)
            return result
        finally:
            self._action_lock.release()


@dataclass(frozen=True, slots=True)
class EmergencyEscalationResult:
    success: bool
    state: str
    failed_deck_id: str
    playback: EmergencyPlaybackResult | None = None
    assessment: DeckRestartAssessment | None = None
    recovery: AudioRecoveryResult | None = None
    error_code: str = ""
    message: str = ""
