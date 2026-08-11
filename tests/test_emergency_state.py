import pytest

from party_player.emergency_state import (
    DeckHealth,
    EmergencyStateService,
    EmergencySystemState,
)


def test_system_and_deck_health_are_independent_and_audited() -> None:
    events: list[tuple[str, dict[str, object]]] = []
    service = EmergencyStateService(lambda code, details: events.append((code, details)))

    service.set_deck_health("B", DeckHealth.SUSPECTED_STALL, "Position unverändert")
    service.transition(EmergencySystemState.WARNING, "Deck B wird geprüft")
    snapshot = service.snapshot()

    assert snapshot.system == EmergencySystemState.WARNING
    assert snapshot.deck_a == DeckHealth.HEALTHY
    assert snapshot.deck_b == DeckHealth.SUSPECTED_STALL
    assert [event[0] for event in events] == [
        "DECK_HEALTH_CHANGED",
        "EMERGENCY_STATE_CHANGED",
    ]
    assert events[0][1]["deck_id"] == "B"


def test_invalid_system_jump_and_unknown_deck_are_rejected() -> None:
    service = EmergencyStateService()

    with pytest.raises(ValueError, match="Ungültiger Notfallzustand"):
        service.transition(EmergencySystemState.RECOVERING, "Kein Fehler erkannt")
    with pytest.raises(ValueError, match="Unbekanntes Deck"):
        service.set_deck_health("C", DeckHealth.FAILED)


def test_recovery_has_explicit_success_and_failure_paths() -> None:
    service = EmergencyStateService()
    service.transition(EmergencySystemState.DEGRADED, "Nur Deck A verfügbar")
    service.transition(EmergencySystemState.RECOVERING, "Deck B wird repariert")
    service.transition(EmergencySystemState.RECOVERY_FAILED, "Backend antwortet nicht")
    service.transition(EmergencySystemState.EMERGENCY_ACTIVE, "Notfalltitel läuft")
    service.transition(EmergencySystemState.RECOVERING, "Normalbetrieb wird geprüft")
    service.transition(EmergencySystemState.NORMAL, "Beide Decks gesund")

    assert service.snapshot().system == EmergencySystemState.NORMAL
