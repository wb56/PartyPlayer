"""Explicit emergency and per-deck health state independent from playback state."""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


class EmergencySystemState(StrEnum):
    NORMAL = "NORMAL"
    WARNING = "WARNING"
    DEGRADED = "DEGRADED"
    EMERGENCY_ACTIVE = "EMERGENCY_ACTIVE"
    RECOVERING = "RECOVERING"
    RECOVERY_FAILED = "RECOVERY_FAILED"


class DeckHealth(StrEnum):
    HEALTHY = "HEALTHY"
    BUFFERING = "BUFFERING"
    SUSPECTED_STALL = "SUSPECTED_STALL"
    STALLED = "STALLED"
    RECOVERING = "RECOVERING"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class EmergencyStateSnapshot:
    system: EmergencySystemState
    reason: str
    deck_a: DeckHealth
    deck_b: DeckHealth


AuditRecorder = Callable[[str, dict[str, object]], None]


class EmergencyStateService:
    """Validate and audit emergency state without controlling audio."""

    _ALLOWED: dict[EmergencySystemState, frozenset[EmergencySystemState]] = {
        EmergencySystemState.NORMAL: frozenset(
            {
                EmergencySystemState.WARNING,
                EmergencySystemState.DEGRADED,
                EmergencySystemState.EMERGENCY_ACTIVE,
            }
        ),
        EmergencySystemState.WARNING: frozenset(
            {
                EmergencySystemState.NORMAL,
                EmergencySystemState.DEGRADED,
                EmergencySystemState.EMERGENCY_ACTIVE,
                EmergencySystemState.RECOVERING,
            }
        ),
        EmergencySystemState.DEGRADED: frozenset(
            {
                EmergencySystemState.WARNING,
                EmergencySystemState.EMERGENCY_ACTIVE,
                EmergencySystemState.RECOVERING,
                EmergencySystemState.RECOVERY_FAILED,
            }
        ),
        EmergencySystemState.EMERGENCY_ACTIVE: frozenset(
            {
                EmergencySystemState.DEGRADED,
                EmergencySystemState.RECOVERING,
                EmergencySystemState.RECOVERY_FAILED,
            }
        ),
        EmergencySystemState.RECOVERING: frozenset(
            {
                EmergencySystemState.NORMAL,
                EmergencySystemState.DEGRADED,
                EmergencySystemState.EMERGENCY_ACTIVE,
                EmergencySystemState.RECOVERY_FAILED,
            }
        ),
        EmergencySystemState.RECOVERY_FAILED: frozenset(
            {
                EmergencySystemState.DEGRADED,
                EmergencySystemState.EMERGENCY_ACTIVE,
                EmergencySystemState.RECOVERING,
            }
        ),
    }

    def __init__(self, audit: AuditRecorder | None = None) -> None:
        self._system = EmergencySystemState.NORMAL
        self._reason = ""
        self._deck_health = {"A": DeckHealth.HEALTHY, "B": DeckHealth.HEALTHY}
        self._audit = audit

    def snapshot(self) -> EmergencyStateSnapshot:
        return EmergencyStateSnapshot(
            self._system,
            self._reason,
            self._deck_health["A"],
            self._deck_health["B"],
        )

    def transition(self, target: EmergencySystemState, reason: str) -> None:
        if target == self._system:
            return
        if target not in self._ALLOWED[self._system]:
            raise ValueError(f"Ungültiger Notfallzustand: {self._system} → {target}")
        previous = self._system
        self._system = target
        self._reason = reason.strip()
        self._record(
            "EMERGENCY_STATE_CHANGED",
            {"previous": previous.value, "state": target.value, "reason": self._reason},
        )

    def set_deck_health(self, deck_id: str, health: DeckHealth, reason: str = "") -> None:
        normalized = deck_id.upper()
        if normalized not in self._deck_health:
            raise ValueError("Unbekanntes Deck")
        previous = self._deck_health[normalized]
        if previous == health:
            return
        self._deck_health[normalized] = health
        self._record(
            "DECK_HEALTH_CHANGED",
            {
                "deck_id": normalized,
                "previous": previous.value,
                "health": health.value,
                "reason": reason.strip(),
            },
        )

    def _record(self, event_code: str, details: dict[str, object]) -> None:
        if self._audit is not None:
            self._audit(event_code, details)
