import pytest

from party_player.emergency_state import (
    DeckHealth,
    EmergencyStateService,
)
from party_player.one_deck_mode import AudioOperatingMode, OneDeckModeService


def test_one_deck_mode_allows_only_selected_deck_and_disables_crossfade() -> None:
    events: list[tuple[str, dict[str, object]]] = []
    service = OneDeckModeService(lambda code, details: events.append((code, details)))

    snapshot = service.enter("A", "Deck B ausgefallen")

    assert snapshot.mode == AudioOperatingMode.ONE_DECK
    assert snapshot.unavailable_deck_id == "B"
    assert service.can_use_deck("A")
    assert not service.can_use_deck("B")
    assert not service.crossfade_allowed()
    assert events[-1][0] == "ONE_DECK_MODE_ENTERED"


def test_two_deck_return_requires_both_decks_healthy() -> None:
    service = OneDeckModeService()
    state = EmergencyStateService()
    service.enter("A", "Deck B ausgefallen")
    state.set_deck_health("B", DeckHealth.FAILED)

    with pytest.raises(RuntimeError, match="Beide Decks"):
        service.return_to_two_deck(state.snapshot())

    state.set_deck_health("B", DeckHealth.HEALTHY)
    snapshot = service.return_to_two_deck(state.snapshot())

    assert snapshot.mode == AudioOperatingMode.TWO_DECK
    assert service.crossfade_allowed()


def test_two_deck_return_is_blocked_while_recovery_is_active() -> None:
    service = OneDeckModeService()
    state = EmergencyStateService()
    service.enter("B", "Deck A ausgefallen")

    with pytest.raises(RuntimeError, match="Recovery läuft"):
        service.return_to_two_deck(state.snapshot(), recovery_active=True)
