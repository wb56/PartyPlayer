"""Central, auditable policy for escalating audio recovery."""

from dataclasses import dataclass
from enum import StrEnum


class RecoveryEscalationStage(StrEnum):
    PRESERVE_AUDIBLE_PLAYBACK = "PRESERVE_AUDIBLE_PLAYBACK"
    PREPARE_EMERGENCY_ON_HEALTHY_DECK = "PREPARE_EMERGENCY_ON_HEALTHY_DECK"
    CONFIRM_EMERGENCY_PLAYBACK = "CONFIRM_EMERGENCY_PLAYBACK"
    RECOVER_FAILED_DECK = "RECOVER_FAILED_DECK"
    ONE_DECK_MODE = "ONE_DECK_MODE"
    REAPPLY_OUTPUT_DEVICE = "REAPPLY_OUTPUT_DEVICE"
    REINITIALIZE_ALL_BACKENDS = "REINITIALIZE_ALL_BACKENDS"
    OPERATOR_INTERVENTION = "OPERATOR_INTERVENTION"


class GlobalRecoveryTrigger(StrEnum):
    AUTOMATIC = "AUTOMATIC"
    OPERATOR_REQUEST = "OPERATOR_REQUEST"
    OUTPUT_DEVICE_FAILURE = "OUTPUT_DEVICE_FAILURE"
    SHARED_RESOURCE_FAILURE = "SHARED_RESOURCE_FAILURE"


@dataclass(frozen=True, slots=True)
class GlobalRecoveryContext:
    healthy_deck_playing: bool = False
    emergency_playback_can_be_prepared: bool = False
    stable_one_deck_mode_possible: bool = False


@dataclass(frozen=True, slots=True)
class GlobalRecoveryAssessment:
    allowed: bool
    trigger: GlobalRecoveryTrigger
    next_stage: RecoveryEscalationStage
    error_code: str = ""
    message: str = ""


class RecoveryEscalationPolicy:
    """Keep global reinitialization behind all less disruptive recovery stages."""

    ORDER = tuple(RecoveryEscalationStage)

    def assess_global_recovery(
        self, trigger: GlobalRecoveryTrigger, context: GlobalRecoveryContext
    ) -> GlobalRecoveryAssessment:
        trigger = GlobalRecoveryTrigger(trigger)
        if trigger in {
            GlobalRecoveryTrigger.OPERATOR_REQUEST,
            GlobalRecoveryTrigger.OUTPUT_DEVICE_FAILURE,
            GlobalRecoveryTrigger.SHARED_RESOURCE_FAILURE,
        }:
            return GlobalRecoveryAssessment(
                True, trigger, RecoveryEscalationStage.REINITIALIZE_ALL_BACKENDS
            )
        if context.healthy_deck_playing:
            return self._blocked(
                trigger,
                RecoveryEscalationStage.PRESERVE_AUDIBLE_PLAYBACK,
                "HEALTHY_DECK_PLAYING",
                "Globale Reparatur gesperrt: Ein gesundes Deck spielt hörbar",
            )
        if context.emergency_playback_can_be_prepared:
            return self._blocked(
                trigger,
                RecoveryEscalationStage.PREPARE_EMERGENCY_ON_HEALTHY_DECK,
                "EMERGENCY_PLAYBACK_AVAILABLE",
                "Globale Reparatur gesperrt: Notfallwiedergabe kann vorbereitet werden",
            )
        if context.stable_one_deck_mode_possible:
            return self._blocked(
                trigger,
                RecoveryEscalationStage.ONE_DECK_MODE,
                "ONE_DECK_MODE_AVAILABLE",
                "Globale Reparatur gesperrt: Stabiler Ein-Deck-Betrieb ist möglich",
            )
        return self._blocked(
            trigger,
            RecoveryEscalationStage.OPERATOR_INTERVENTION,
            "GLOBAL_RECOVERY_REQUIRES_JUSTIFICATION",
            "Globale Reparatur erfordert Bedienerfreigabe oder einen nachgewiesenen gemeinsamen Ausfall",
        )

    @staticmethod
    def _blocked(
        trigger: GlobalRecoveryTrigger,
        stage: RecoveryEscalationStage,
        error_code: str,
        message: str,
    ) -> GlobalRecoveryAssessment:
        return GlobalRecoveryAssessment(False, trigger, stage, error_code, message)
