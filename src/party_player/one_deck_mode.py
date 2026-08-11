"""Explicit degraded one-deck operation without hidden fallback to two decks."""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from party_player.emergency_state import DeckHealth, EmergencyStateSnapshot


class AudioOperatingMode(StrEnum):
    TWO_DECK = "TWO_DECK"
    ONE_DECK = "ONE_DECK"


@dataclass(frozen=True, slots=True)
class AudioOperatingModeSnapshot:
    mode: AudioOperatingMode
    active_deck_id: str | None = None
    unavailable_deck_id: str | None = None
    reason: str = ""


class OneDeckModeService:
    """Own the safety gates for degraded sequential playback."""

    def __init__(self, audit: Callable[[str, dict[str, object]], None] | None = None) -> None:
        self._snapshot = AudioOperatingModeSnapshot(AudioOperatingMode.TWO_DECK)
        self._audit = audit

    def snapshot(self) -> AudioOperatingModeSnapshot:
        return self._snapshot

    def enter(self, active_deck_id: str, reason: str) -> AudioOperatingModeSnapshot:
        active = active_deck_id.upper()
        if active not in {"A", "B"}:
            raise ValueError("Unbekanntes Deck")
        unavailable = "B" if active == "A" else "A"
        self._snapshot = AudioOperatingModeSnapshot(
            AudioOperatingMode.ONE_DECK, active, unavailable, reason.strip()
        )
        self._record("ONE_DECK_MODE_ENTERED")
        return self._snapshot

    def can_use_deck(self, deck_id: str) -> bool:
        snapshot = self._snapshot
        return snapshot.mode == AudioOperatingMode.TWO_DECK or (
            deck_id.upper() == snapshot.active_deck_id
        )

    def crossfade_allowed(self) -> bool:
        return self._snapshot.mode == AudioOperatingMode.TWO_DECK

    def return_to_two_deck(
        self, health: EmergencyStateSnapshot, *, recovery_active: bool = False
    ) -> AudioOperatingModeSnapshot:
        if recovery_active:
            raise RuntimeError("Deck-Recovery läuft noch")
        if health.deck_a != DeckHealth.HEALTHY or health.deck_b != DeckHealth.HEALTHY:
            raise RuntimeError("Beide Decks müssen gesund sein")
        self._snapshot = AudioOperatingModeSnapshot(AudioOperatingMode.TWO_DECK)
        self._record("TWO_DECK_MODE_RESTORED")
        return self._snapshot

    def _record(self, event_code: str) -> None:
        if self._audit is not None:
            snapshot = self._snapshot
            self._audit(
                event_code,
                {
                    "mode": snapshot.mode.value,
                    "active_deck_id": snapshot.active_deck_id,
                    "unavailable_deck_id": snapshot.unavailable_deck_id,
                    "reason": snapshot.reason,
                },
            )
