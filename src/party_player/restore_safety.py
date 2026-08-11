"""Domain safety gate for starting and committing a database restore."""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


class RestoreSafetyBlocker(StrEnum):
    DECK_A_NOT_STOPPED = "DECK_A_NOT_STOPPED"
    DECK_B_NOT_STOPPED = "DECK_B_NOT_STOPPED"
    CROSSFADE_ACTIVE = "CROSSFADE_ACTIVE"
    OVERLAY_ACTIVE = "OVERLAY_ACTIVE"
    AUDIO_RECOVERY_ACTIVE = "AUDIO_RECOVERY_ACTIVE"
    DECK_RECOVERY_ACTIVE = "DECK_RECOVERY_ACTIVE"
    EMERGENCY_ACTION_ACTIVE = "EMERGENCY_ACTION_ACTIVE"
    CUE_ANALYSIS_ACTIVE = "CUE_ANALYSIS_ACTIVE"
    LOUDNESS_ANALYSIS_ACTIVE = "LOUDNESS_ANALYSIS_ACTIVE"
    STATE_UNAVAILABLE = "STATE_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class RestoreSafetySnapshot:
    deck_a_stopped: bool
    deck_b_stopped: bool
    crossfade_active: bool
    overlay_active: bool
    audio_recovery_active: bool
    deck_recovery_active: bool
    emergency_action_active: bool
    cue_analysis_active: bool
    loudness_analysis_active: bool


@dataclass(frozen=True, slots=True)
class RestoreSafetyReason:
    code: RestoreSafetyBlocker
    message: str


@dataclass(frozen=True, slots=True)
class RestoreSafetyResult:
    allowed: bool
    reasons: tuple[RestoreSafetyReason, ...]


class RestoreSafetyGate:
    """Evaluate every safety condition without short-circuiting."""

    def __init__(self, snapshot: Callable[[], RestoreSafetySnapshot]) -> None:
        self._snapshot = snapshot

    def evaluate(self) -> RestoreSafetyResult:
        try:
            state = self._snapshot()
        except Exception:
            reason = RestoreSafetyReason(
                RestoreSafetyBlocker.STATE_UNAVAILABLE,
                "Der aktuelle Betriebszustand konnte nicht sicher ermittelt werden.",
            )
            return RestoreSafetyResult(False, (reason,))

        checks = (
            (
                not state.deck_a_stopped,
                RestoreSafetyBlocker.DECK_A_NOT_STOPPED,
                "Deck A ist nicht gestoppt.",
            ),
            (
                not state.deck_b_stopped,
                RestoreSafetyBlocker.DECK_B_NOT_STOPPED,
                "Deck B ist nicht gestoppt.",
            ),
            (
                state.crossfade_active,
                RestoreSafetyBlocker.CROSSFADE_ACTIVE,
                "Ein Crossfade ist aktiv.",
            ),
            (state.overlay_active, RestoreSafetyBlocker.OVERLAY_ACTIVE, "Ein Overlay ist aktiv."),
            (
                state.audio_recovery_active,
                RestoreSafetyBlocker.AUDIO_RECOVERY_ACTIVE,
                "Eine Audio-Recovery ist aktiv.",
            ),
            (
                state.deck_recovery_active,
                RestoreSafetyBlocker.DECK_RECOVERY_ACTIVE,
                "Eine Deck-Recovery ist aktiv.",
            ),
            (
                state.emergency_action_active,
                RestoreSafetyBlocker.EMERGENCY_ACTION_ACTIVE,
                "Eine Notfallaktion ist aktiv.",
            ),
            (
                state.cue_analysis_active,
                RestoreSafetyBlocker.CUE_ANALYSIS_ACTIVE,
                "Eine Cue-Analyse ist aktiv.",
            ),
            (
                state.loudness_analysis_active,
                RestoreSafetyBlocker.LOUDNESS_ANALYSIS_ACTIVE,
                "Eine Lautheitsanalyse ist aktiv.",
            ),
        )
        reasons = tuple(
            RestoreSafetyReason(code, message) for blocked, code, message in checks if blocked
        )
        return RestoreSafetyResult(not reasons, reasons)
