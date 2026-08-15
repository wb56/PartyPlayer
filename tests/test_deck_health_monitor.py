from party_player.audio.fake_backend import FakeAudioBackend
from party_player.deck_controller import DeckController
from party_player.deck_health_monitor import DeckHealthMonitor
from party_player.emergency_state import DeckHealth, EmergencyStateService, EmergencySystemState
from party_player.enums import DeckState
from party_player.models import Track
import logging


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def playing_deck(path: str = "local.mp3") -> tuple[DeckController, FakeAudioBackend]:
    backend = FakeAudioBackend()
    deck = DeckController("A", backend)
    deck.load(Track(1, path, "Titel", "", "", 180), validate_file=False)
    deck.play()
    return deck, backend


def test_stall_requires_timeout_and_separate_confirmation() -> None:
    clock = FakeClock()
    state = EmergencyStateService()
    monitor = DeckHealthMonitor(
        state,
        local_stall_seconds=4,
        network_stall_seconds=10,
        confirmation_seconds=2,
        clock=clock,
    )
    deck, _backend = playing_deck()

    assert monitor.observe(deck).health == DeckHealth.BUFFERING
    clock.advance(3.9)
    assert monitor.observe(deck).health == DeckHealth.BUFFERING
    clock.advance(0.1)
    assert monitor.observe(deck).health == DeckHealth.SUSPECTED_STALL
    assert state.snapshot().system == EmergencySystemState.WARNING
    clock.advance(1.9)
    assert monitor.observe(deck).health == DeckHealth.SUSPECTED_STALL
    clock.advance(0.1)
    assert monitor.observe(deck).health == DeckHealth.STALLED
    assert state.snapshot().system == EmergencySystemState.DEGRADED


def test_position_progress_clears_suspicion_and_warning() -> None:
    clock = FakeClock()
    state = EmergencyStateService()
    monitor = DeckHealthMonitor(state, local_stall_seconds=1, confirmation_seconds=1, clock=clock)
    deck, backend = playing_deck()
    monitor.observe(deck)
    clock.advance(1)
    assert monitor.observe(deck).health == DeckHealth.SUSPECTED_STALL

    backend.position = 0.5
    deck.update_status()
    recovered = monitor.observe(deck)

    assert recovered.health == DeckHealth.HEALTHY
    assert state.snapshot().system == EmergencySystemState.NORMAL


def test_stall_diagnostics_include_worker_gui_statistics_and_source_context(
    caplog,
) -> None:
    clock = FakeClock()
    monitor = DeckHealthMonitor(
        EmergencyStateService(),
        local_stall_seconds=1,
        network_stall_seconds=1,
        confirmation_seconds=1,
        clock=clock,
    )
    monitor.set_diagnostic_context_provider(
        lambda _deck_id: {
            "gui_heartbeat_age_ms": 1250.0,
            "active_workers": ("queue-statistics",),
            "queue_statistics_pending": True,
            "source_state": "ERREICHBAR",
        }
    )
    deck, backend = playing_deck(r"\\server\music\track.mp3")
    deck.update_status()
    monitor.observe(deck)
    clock.advance(1)

    with caplog.at_level(logging.WARNING):
        monitor.observe(deck)

    message = "\n".join(record.getMessage() for record in caplog.records)
    assert "audio.stall_suspected" in message
    assert "queue-statistics" in message
    assert "gui_heartbeat_age_ms" in message
    assert "ERREICHBAR" in message
    backend.position = 0.5
    deck.update_status()
    clock.advance(0.5)
    monitor.observe(deck)
    assert any("audio.stall_recovered" in record.getMessage() for record in caplog.records)


def test_pause_is_never_reported_as_stall() -> None:
    clock = FakeClock()
    state = EmergencyStateService()
    monitor = DeckHealthMonitor(state, local_stall_seconds=1, clock=clock)
    deck, _backend = playing_deck()
    monitor.observe(deck)
    deck.pause()
    clock.advance(100)

    result = monitor.observe(deck)

    assert result.health == DeckHealth.HEALTHY
    assert state.snapshot().system == EmergencySystemState.NORMAL


def test_network_source_uses_longer_stall_timeout() -> None:
    clock = FakeClock()
    state = EmergencyStateService()
    monitor = DeckHealthMonitor(state, local_stall_seconds=2, network_stall_seconds=8, clock=clock)
    deck, _backend = playing_deck(r"\\server\music\track.mp3")
    monitor.observe(deck)
    clock.advance(3)

    result = monitor.observe(deck)

    assert result.network_source
    assert result.health == DeckHealth.BUFFERING


def test_backend_playing_flag_alone_does_not_hide_missing_progress() -> None:
    clock = FakeClock()
    state = EmergencyStateService()
    monitor = DeckHealthMonitor(state, local_stall_seconds=1, confirmation_seconds=1, clock=clock)
    deck, backend = playing_deck()
    assert backend.is_playing()
    monitor.observe(deck)
    clock.advance(1)

    result = monitor.observe(deck)

    assert result.health == DeckHealth.SUSPECTED_STALL
    assert result.reason == "Position ohne Fortschritt"


def test_backend_query_error_becomes_failed_health_instead_of_escaping() -> None:
    class BrokenBackend(FakeAudioBackend):
        def is_playing(self) -> bool:
            raise RuntimeError("Gerät getrennt")

    clock = FakeClock()
    state = EmergencyStateService()
    monitor = DeckHealthMonitor(state, local_stall_seconds=0.1, clock=clock)
    backend = BrokenBackend()
    deck = DeckController("A", backend)
    deck.load(Track(1, "local.mp3", "Titel", "", "", 180), validate_file=False)
    deck.model.state = DeckState.PLAYING
    monitor.observe(deck)
    clock.advance(0.1)

    result = monitor.observe(deck)

    assert result.health == DeckHealth.FAILED
    assert "Gerät getrennt" in result.reason
    assert state.snapshot().system == EmergencySystemState.DEGRADED


def test_repeated_command_failures_escalate_but_single_failure_does_not() -> None:
    state = EmergencyStateService()
    monitor = DeckHealthMonitor(state, command_failure_threshold=3)

    monitor.report_command_result("A", "play", False, "Ausgabe fehlgeschlagen")
    assert state.snapshot().deck_a == DeckHealth.HEALTHY
    monitor.report_command_result("A", "play", False, "Ausgabe fehlgeschlagen")
    assert state.snapshot().deck_a == DeckHealth.SUSPECTED_STALL
    assert state.snapshot().system == EmergencySystemState.WARNING
    monitor.report_command_result("A", "play", False, "Ausgabe fehlgeschlagen")

    assert state.snapshot().deck_a == DeckHealth.FAILED
    assert state.snapshot().system == EmergencySystemState.DEGRADED


def test_success_resets_consecutive_command_failure_count() -> None:
    state = EmergencyStateService()
    monitor = DeckHealthMonitor(state, command_failure_threshold=2)

    monitor.report_command_result("A", "play", False)
    monitor.report_command_result("A", "play", True)
    monitor.report_command_result("A", "play", False)

    assert state.snapshot().deck_a == DeckHealth.HEALTHY


def test_bound_deck_reports_transport_failures() -> None:
    class BrokenBackend(FakeAudioBackend):
        def play(self) -> None:
            raise RuntimeError("Gerät antwortet nicht")

    state = EmergencyStateService()
    monitor = DeckHealthMonitor(state, command_failure_threshold=1)
    deck = DeckController("A", BrokenBackend())
    monitor.bind(deck)
    deck.load(Track(1, "local.mp3", "Titel", "", "", 180), validate_file=False)

    try:
        deck.play()
    except RuntimeError:
        pass

    assert state.snapshot().deck_a == DeckHealth.FAILED
    assert state.snapshot().system == EmergencySystemState.DEGRADED


def test_missing_explicit_output_device_fails_both_decks() -> None:
    state = EmergencyStateService()
    monitor = DeckHealthMonitor(state)

    available = monitor.report_output_device("usb-dac", {"speakers"})

    assert not available
    assert state.snapshot().deck_a == DeckHealth.FAILED
    assert state.snapshot().deck_b == DeckHealth.FAILED
    assert state.snapshot().system == EmergencySystemState.DEGRADED


def test_system_default_output_does_not_require_enumerated_id() -> None:
    state = EmergencyStateService()
    monitor = DeckHealthMonitor(state)

    assert monitor.report_output_device("", set())
    assert state.snapshot().system == EmergencySystemState.NORMAL


def test_confirmed_output_device_recovery_restores_previous_health() -> None:
    state = EmergencyStateService()
    monitor = DeckHealthMonitor(state)

    assert not monitor.report_output_device("usb-dac", {"speakers"})
    assert monitor.report_output_device("usb-dac", {"usb-dac"})
    # Enumeration alone is deliberately insufficient to clear the failure.
    assert state.snapshot().deck_a == DeckHealth.FAILED

    monitor.confirm_output_device_recovered()

    assert state.snapshot().deck_a == DeckHealth.HEALTHY
    assert state.snapshot().deck_b == DeckHealth.HEALTHY
    assert state.snapshot().system == EmergencySystemState.NORMAL


def test_output_device_recovery_does_not_hide_preexisting_deck_failure() -> None:
    state = EmergencyStateService()
    state.set_deck_health("A", DeckHealth.STALLED, "Vorheriger Fehler")
    monitor = DeckHealthMonitor(state)

    monitor.report_output_device("usb-dac", set())
    monitor.confirm_output_device_recovered()

    assert state.snapshot().deck_a == DeckHealth.STALLED
    assert state.snapshot().deck_b == DeckHealth.HEALTHY
    assert state.snapshot().system == EmergencySystemState.DEGRADED
