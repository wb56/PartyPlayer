"""State-machine tests for automatic deck transitions."""

from collections.abc import Callable
from time import sleep

from party_player.audio.fake_backend import FakeAudioBackend
from party_player.crossfader_service import CrossfaderService
from party_player.deck_controller import DeckController
from party_player.models import Track
import party_player.transition_controller as transition_module
from party_player.transition_controller import TransitionController, TransitionState


def _loaded_deck(deck_id: str, track_id: int) -> DeckController:
    deck = DeckController(deck_id, FakeAudioBackend(duration=30))
    deck.load(Track(track_id, f"song-{track_id}.mp3", "Song", "", "", 30), validate_file=False)
    return deck


def test_transition_controller_has_no_queue_persistence_dependency() -> None:
    assert "QueueService" not in transition_module.__dict__
    assert "PartyPlayerRepository" not in transition_module.__dict__


def test_transition_waits_for_confirmed_playback_and_can_abort() -> None:
    deck_a = _loaded_deck("A", 1)
    deck_b = _loaded_deck("B", 2)
    deck_a.play()
    deck_b.model.state = deck_a.model.state
    scheduled: list[Callable[[], None]] = []
    transition = TransitionController(
        CrossfaderService(deck_a, deck_b),
        lambda _delay, callback: scheduled.append(callback),
        lambda: None,
        lambda _deck, _track_id, _queue_id: None,
    )

    transition.begin(deck_a, deck_b, 7)

    assert transition.state == TransitionState.WAIT_FOR_ACTUAL_PLAYBACK
    assert transition.is_transitioning
    assert scheduled
    transition.abort("Benutzereingriff")
    assert transition.state == TransitionState.ABORTED  # type: ignore[comparison-overlap]
    assert not transition.is_transitioning


def test_transition_enters_crossfade_after_actual_playback() -> None:
    deck_a = _loaded_deck("A", 1)
    deck_b = _loaded_deck("B", 2)
    deck_a.play()
    deck_b.play()
    deck_b.set_transition_muted(True)
    scheduled: list[Callable[[], None]] = []
    transition = TransitionController(
        CrossfaderService(deck_a, deck_b),
        lambda _delay, callback: scheduled.append(callback),
        lambda: None,
        lambda _deck, _track_id, _queue_id: None,
    )

    transition.begin(deck_a, deck_b, 7)

    assert transition.state == TransitionState.CROSSFADE
    assert not deck_b.transition_muted
    assert scheduled


def test_delayed_playback_check_accepts_forward_audio_progress() -> None:
    deck_a = _loaded_deck("A", 1)
    deck_b = _loaded_deck("B", 2)
    deck_a.play()
    deck_b.model.state = deck_a.model.state
    scheduled: list[Callable[[], None]] = []
    transition = TransitionController(
        CrossfaderService(deck_a, deck_b),
        lambda _delay, callback: scheduled.append(callback),
        lambda: None,
        lambda _deck, _track_id, _queue_id: None,
    )
    transition.begin(deck_a, deck_b, 7)
    deck_b.play()
    backend_b = deck_b.backend
    assert isinstance(backend_b, FakeAudioBackend)
    backend_b.position = 3.0

    scheduled.pop(0)()

    assert transition.state == TransitionState.CROSSFADE
    assert not deck_b.transition_muted


def test_unconfirmed_incoming_playback_reports_structured_transition_failure() -> None:
    deck_a = _loaded_deck("A", 1)
    deck_b = _loaded_deck("B", 2)
    deck_a.play()
    deck_b.model.state = deck_a.model.state
    scheduled: list[Callable[[], None]] = []
    failures: list[tuple[str, str, str]] = []
    transition = TransitionController(
        CrossfaderService(deck_a, deck_b),
        lambda _delay, callback: scheduled.append(callback),
        lambda: None,
        lambda _deck, _track_id, _queue_id: None,
        failure=lambda reason, outgoing, incoming: failures.append(
            (reason, outgoing.model.deck_id, incoming.model.deck_id)
        ),
    )
    transition.START_WAIT_STEPS = 0

    transition.begin(deck_a, deck_b, 7)

    assert transition.state == TransitionState.FAILED
    assert failures == [("INCOMING_PLAYBACK_NOT_CONFIRMED", "A", "B")]


def test_unconfirmed_incoming_playback_is_retried_once_before_failure() -> None:
    deck_a = _loaded_deck("A", 1)
    deck_b = _loaded_deck("B", 2)
    deck_a.play()
    deck_b.model.state = deck_a.model.state
    scheduled: list[Callable[[], None]] = []
    transition = TransitionController(
        CrossfaderService(deck_a, deck_b),
        lambda _delay, callback: scheduled.append(callback),
        lambda: None,
        lambda _deck, _track_id, _queue_id: None,
    )
    transition.START_RETRY_STEP = 1

    transition.begin(deck_a, deck_b, 7)
    scheduled.pop(0)()

    assert transition.state == TransitionState.WAIT_FOR_ACTUAL_PLAYBACK
    assert deck_b.backend.is_playing()
    scheduled.pop(0)()
    assert transition.state == TransitionState.CROSSFADE


def test_audio_fade_frequency_is_independent_from_visible_render_frequency() -> None:
    assert TransitionController.FADE_INTERVAL_MS == 16
    assert TransitionController.RENDER_INTERVAL_MS >= 100
    assert (
        TransitionController.START_WAIT_STEPS * TransitionController.START_WAIT_INTERVAL_MS
        >= 8000
    )


def test_crossfade_records_bounded_actual_gain_samples() -> None:
    deck_a = _loaded_deck("A", 1)
    deck_b = _loaded_deck("B", 2)
    deck_a.play()
    deck_b.play()
    deck_a.normalization_factor = 0.5
    deck_b.normalization_factor = 1.25
    scheduled: list[Callable[[], None]] = []
    transition = TransitionController(
        CrossfaderService(deck_a, deck_b),
        lambda _delay, callback: scheduled.append(callback),
        lambda: None,
        lambda _deck, _track_id, _queue_id: None,
        fade_duration=0.25,
    )

    transition.begin(deck_a, deck_b, 7)
    sleep(0.1)

    samples = transition.level_samples()
    assert samples
    assert samples[-1].elapsed_ms >= samples[0].elapsed_ms
    assert samples[-1].position > samples[0].position
    assert samples[-1].normalization_a == 0.5
    assert samples[-1].normalization_b == 1.25
    assert samples[-1].backend_volume_a == deck_a.effective_volume
    assert samples[-1].backend_volume_b == deck_b.effective_volume
    diagnostic = transition.level_diagnostic()
    assert diagnostic.direction == "A_TO_B"
    assert diagnostic.position_monotonic
    assert diagnostic.maximum_sample_gap_ms < 100.0
    transition.abort("Test beendet")


def test_crossfade_level_diagnosis_covers_b_to_a_with_normalization() -> None:
    deck_a = _loaded_deck("A", 1)
    deck_b = _loaded_deck("B", 2)
    deck_a.play()
    deck_b.play()
    deck_a.normalization_factor = 1.4
    deck_b.normalization_factor = 0.65
    mixer = CrossfaderService(deck_a, deck_b, position=1.0)
    scheduled: list[Callable[[], None]] = []
    transition = TransitionController(
        mixer,
        lambda _delay, callback: scheduled.append(callback),
        lambda: None,
        lambda _deck, _track_id, _queue_id: None,
        fade_duration=0.25,
    )

    transition.begin(deck_b, deck_a, 8)
    sleep(0.35)

    samples = transition.level_samples()
    diagnostic = transition.level_diagnostic()
    assert len(samples) >= 2
    assert diagnostic.direction == "B_TO_A"
    assert diagnostic.position_monotonic
    assert diagnostic.reached_target
    assert diagnostic.audio_ramp_complete
    assert samples[-1].position == 0.0
    assert samples[-1].backend_volume_a > samples[0].backend_volume_a
    assert samples[-1].backend_volume_b < samples[0].backend_volume_b
    assert samples[-1].normalization_a == 1.4
    assert samples[-1].normalization_b == 0.65
