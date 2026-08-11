from threading import Event, Thread
from time import monotonic

from party_player.audio.fake_backend import FakeAudioBackend
from party_player.audio_recovery import AudioRecoveryPolicy, AudioRecoveryService
from party_player.recovery_escalation import GlobalRecoveryTrigger
from party_player.deck_controller import DeckController
from party_player.emergency_state import DeckHealth, EmergencyStateService, EmergencySystemState
from party_player.enums import DeckState
from party_player.models import Track


def make_service(
    *,
    factory_a=lambda: FakeAudioBackend(),
    factory_b=lambda: FakeAudioBackend(),
    independent_players: bool = True,
    preserves_shared_instance: bool = True,
    preserves_output_device: bool = True,
    emergency_track_provider=None,
    **service_kwargs,
):
    state = EmergencyStateService()
    deck_a = DeckController("A", FakeAudioBackend())
    deck_b = DeckController("B", FakeAudioBackend())
    events: list[tuple[str, dict[str, object]]] = []
    service = AudioRecoveryService(
        state,
        deck_a,
        deck_b,
        {"A": factory_a, "B": factory_b},
        independent_players=independent_players,
        preserves_shared_instance=preserves_shared_instance,
        preserves_output_device=preserves_output_device,
        audit=lambda code, details: events.append((code, details)),
        emergency_track_provider=emergency_track_provider,
        **service_kwargs,
    )
    return service, state, deck_a, deck_b, events


def test_isolated_recovery_replaces_only_requested_deck() -> None:
    service, state, deck_a, deck_b, events = make_service()
    old_a = deck_a.backend
    old_b = deck_b.backend
    state.set_deck_health("A", DeckHealth.STALLED)

    result = service.recover_deck("A")

    assert result.success
    assert result.state == "RECOVERED"
    assert deck_a.backend is not old_a
    assert deck_b.backend is old_b
    assert state.snapshot().system == EmergencySystemState.NORMAL
    assert state.snapshot().deck_a == DeckHealth.HEALTHY
    assert deck_a.transition_muted
    assert events[-1][1]["deck_id"] == "A"


def test_recovery_is_blocked_when_shared_resources_would_be_reinitialized() -> None:
    service, state, deck_a, deck_b, _events = make_service(preserves_shared_instance=False)
    old_a, old_b = deck_a.backend, deck_b.backend

    assessment = service.can_restart_deck_independently("A")
    result = service.recover_deck("A")

    assert not assessment.allowed
    assert assessment.error_code == "SHARED_INSTANCE_NOT_PRESERVED"
    assert result.state == "BLOCKED"
    assert deck_a.backend is old_a
    assert deck_b.backend is old_b
    assert state.snapshot().system == EmergencySystemState.NORMAL


def test_parallel_recovery_of_same_deck_is_rejected() -> None:
    entered, release = Event(), Event()

    def blocking_factory():
        entered.set()
        release.wait(timeout=2)
        return FakeAudioBackend()

    service, state, _deck_a, _deck_b, _events = make_service(factory_a=blocking_factory)
    state.set_deck_health("A", DeckHealth.STALLED)
    worker = Thread(target=lambda: service.recover_deck("A"))
    worker.start()
    assert entered.wait(timeout=1)
    assert service.recovery_active()

    busy = service.recover_deck("A")

    assert not busy.success
    assert busy.error_code == "DECK_RECOVERY_ACTIVE"
    release.set()
    worker.join(timeout=1)
    assert not service.recovery_active()


def test_different_decks_use_independent_locks_and_keep_recovering_state() -> None:
    entered_a, entered_b, release_a, release_b = Event(), Event(), Event(), Event()

    def factory_a():
        entered_a.set()
        release_a.wait(timeout=2)
        return FakeAudioBackend()

    def factory_b():
        entered_b.set()
        release_b.wait(timeout=2)
        return FakeAudioBackend()

    service, state, _deck_a, _deck_b, _events = make_service(
        factory_a=factory_a, factory_b=factory_b
    )
    workers = [
        Thread(target=lambda: service.recover_deck("A")),
        Thread(target=lambda: service.recover_deck("B")),
    ]
    for worker in workers:
        worker.start()
    assert entered_a.wait(timeout=1) and entered_b.wait(timeout=1)

    release_a.set()
    workers[0].join(timeout=1)
    assert state.snapshot().system == EmergencySystemState.RECOVERING
    assert service.recovery_active()

    release_b.set()
    workers[1].join(timeout=1)
    assert state.snapshot().system == EmergencySystemState.NORMAL
    assert not service.recovery_active()


def test_global_recovery_requires_both_deck_locks() -> None:
    entered, release = Event(), Event()

    def blocking_factory():
        entered.set()
        release.wait(timeout=2)
        return FakeAudioBackend()

    service, _state, _deck_a, _deck_b, _events = make_service(factory_a=blocking_factory)
    worker = Thread(target=lambda: service.recover_deck("A"))
    worker.start()
    assert entered.wait(timeout=1)

    result = service.recover_all_backends(GlobalRecoveryTrigger.OPERATOR_REQUEST)

    assert not result.success
    assert result.error_code == "DECK_RECOVERY_ACTIVE"
    release.set()
    worker.join(timeout=1)


def test_two_global_recoveries_cannot_run_together() -> None:
    entered, release = Event(), Event()

    def blocking_factory():
        entered.set()
        release.wait(timeout=2)
        return FakeAudioBackend()

    service, _state, _deck_a, _deck_b, _events = make_service(factory_a=blocking_factory)
    worker = Thread(
        target=lambda: service.recover_all_backends(GlobalRecoveryTrigger.OPERATOR_REQUEST)
    )
    worker.start()
    assert entered.wait(timeout=1)

    busy = service.recover_all_backends(GlobalRecoveryTrigger.OPERATOR_REQUEST)

    assert not busy.success
    assert busy.error_code == "GLOBAL_RECOVERY_ACTIVE"
    release.set()
    worker.join(timeout=1)


def test_failed_replacement_marks_only_requested_deck_failed() -> None:
    def fail():
        raise RuntimeError("Backend defekt")

    service, state, deck_a, deck_b, events = make_service(factory_a=fail)
    old_a, old_b = deck_a.backend, deck_b.backend

    result = service.recover_deck("A")

    assert not result.success
    assert result.error_code == "DECK_RESTART_FAILED"
    assert deck_a.backend is old_a
    assert deck_b.backend is old_b
    assert state.snapshot().deck_a == DeckHealth.FAILED
    assert state.snapshot().system == EmergencySystemState.RECOVERY_FAILED
    assert events[-1][1]["error_code"] == "DECK_RESTART_FAILED"


def test_recovery_restores_track_safe_position_and_loudness_while_muted() -> None:
    replacement = FakeAudioBackend()
    service, state, deck_a, deck_b, _events = make_service(factory_a=lambda: replacement)
    track = Track(17, "local.mp3", "Titel", "Interpret", "Album", 180)
    deck_a.load(track, validate_file=False)
    deck_a.model.cue_in = 12.0
    deck_a.model.cue_out = 170.0
    deck_a.model.cue_boundaries_ready = True
    deck_a.normalization_factor = 1.4
    deck_a.play()
    deck_a.model.position = 42.0
    old_b = deck_b.backend
    state.set_deck_health("A", DeckHealth.STALLED)

    result = service.recover_deck("A")

    assert result.success
    assert result.track_id == 17
    assert result.restored_position == 41.0
    assert result.playback_confirmed
    assert deck_a.model.loaded_track == track
    assert deck_a.model.position == 41.0
    assert deck_a.model.cue_in == 12.0
    assert deck_a.model.cue_out == 170.0
    assert deck_a.model.state == DeckState.PLAYING
    assert deck_a.normalization_factor == 1.4
    assert deck_a.transition_muted
    assert replacement.volume == 0.0
    assert replacement.is_playing()
    assert deck_b.backend is old_b


def test_failed_replacement_preparation_keeps_old_backend_and_context() -> None:
    class UnloadableBackend(FakeAudioBackend):
        def load(self, file_path):  # type: ignore[no-untyped-def]
            raise RuntimeError("Decoderstart fehlgeschlagen")

    service, state, deck_a, _deck_b, _events = make_service(factory_a=UnloadableBackend)
    track = Track(23, "local.mp3", "Titel", "", "", 180)
    deck_a.load(track, validate_file=False)
    deck_a.model.position = 25.0
    old_backend = deck_a.backend
    state.set_deck_health("A", DeckHealth.STALLED)

    result = service.recover_deck("A")

    assert not result.success
    assert deck_a.backend is old_backend
    assert deck_a.model.loaded_track == track
    assert deck_a.model.position == 25.0


def test_restart_track_policy_uses_cue_in_instead_of_previous_position() -> None:
    service, state, deck_a, _deck_b, events = make_service()
    track = Track(31, "local.mp3", "Titel", "", "", 180)
    deck_a.load(track, validate_file=False)
    deck_a.model.cue_in = 8.5
    deck_a.play()
    deck_a.model.position = 90.0
    state.set_deck_health("A", DeckHealth.STALLED)

    result = service.recover_deck("A", AudioRecoveryPolicy.RESTART_TRACK)

    assert result.success
    assert result.policy == AudioRecoveryPolicy.RESTART_TRACK
    assert result.restored_position == 8.5
    assert deck_a.model.position == 8.5
    assert events[-1][1]["policy"] == "RESTART_TRACK"


def test_skip_track_policy_replaces_backend_without_reloading_track() -> None:
    service, state, deck_a, _deck_b, _events = make_service()
    deck_a.load(Track(32, "local.mp3", "Titel", "", "", 180), validate_file=False)
    deck_a.play()
    state.set_deck_health("A", DeckHealth.STALLED)

    result = service.recover_deck("A", AudioRecoveryPolicy.SKIP_TRACK)

    assert result.success
    assert result.track_id is None
    assert not result.playback_confirmed
    assert deck_a.model.loaded_track is None
    assert deck_a.model.state == DeckState.EMPTY


def test_load_emergency_policy_uses_only_configured_local_candidate() -> None:
    emergency = Track(99, "emergency.mp3", "Notfall", "", "", 120)
    service, state, deck_a, _deck_b, _events = make_service(
        emergency_track_provider=lambda: emergency
    )
    deck_a.load(Track(33, "broken.mp3", "Defekt", "", "", 180), validate_file=False)
    deck_a.play()
    state.set_deck_health("A", DeckHealth.STALLED)

    result = service.recover_deck("A", AudioRecoveryPolicy.LOAD_EMERGENCY)

    assert result.success
    assert result.track_id == 99
    assert result.playback_confirmed
    assert deck_a.model.loaded_track == emergency
    assert deck_a.model.position == 0.0
    assert deck_a.transition_muted


def test_load_emergency_policy_fails_closed_without_validated_candidate() -> None:
    service, state, deck_a, _deck_b, _events = make_service()
    old_backend = deck_a.backend
    state.set_deck_health("A", DeckHealth.STALLED)

    result = service.recover_deck("A", AudioRecoveryPolicy.LOAD_EMERGENCY)

    assert not result.success
    assert result.policy == AudioRecoveryPolicy.LOAD_EMERGENCY
    assert deck_a.backend is old_backend


def test_failed_recovery_attempts_are_bounded_without_extra_factory_call() -> None:
    calls = 0

    def fail():
        nonlocal calls
        calls += 1
        raise RuntimeError("Backend defekt")

    service, _state, _deck_a, _deck_b, events = make_service(factory_a=fail)

    results = [service.recover_deck("A") for _attempt in range(4)]

    assert calls == 3
    assert [result.attempt for result in results[:3]] == [1, 2, 3]
    assert results[2].attempts_remaining == 0
    assert results[3].error_code == "RECOVERY_ATTEMPTS_EXHAUSTED"
    assert events[-1][1]["attempts_remaining"] == 0


def test_successful_recovery_resets_consecutive_attempt_budget() -> None:
    calls = 0

    def alternating_factory():
        nonlocal calls
        calls += 1
        if calls in {1, 3}:
            raise RuntimeError("Kurzzeitfehler")
        return FakeAudioBackend()

    service, state, _deck_a, _deck_b, _events = make_service(factory_a=alternating_factory)

    first = service.recover_deck("A")
    second = service.recover_deck("A")
    state.set_deck_health("A", DeckHealth.STALLED)
    third = service.recover_deck("A")

    assert first.attempt == 1
    assert second.success and second.attempt == 2
    assert not third.success and third.attempt == 1


def test_player_creation_timeout_returns_stable_code_without_blocking_caller() -> None:
    release = Event()

    def blocking_factory():
        release.wait(timeout=2)
        return FakeAudioBackend()

    service, _state, deck_a, _deck_b, _events = make_service(
        factory_a=blocking_factory,
        player_creation_timeout_seconds=0.05,
    )
    old_backend = deck_a.backend
    started = monotonic()

    result = service.recover_deck("A")

    assert monotonic() - started < 0.5
    assert result.error_code == "PLAYER_CREATION_TIMEOUT"
    assert deck_a.backend is old_backend
    release.set()


def test_media_load_timeout_never_adopts_late_replacement() -> None:
    entered, release = Event(), Event()

    class SlowLoadBackend(FakeAudioBackend):
        def load(self, file_path):  # type: ignore[no-untyped-def]
            entered.set()
            release.wait(timeout=2)
            super().load(file_path)

    replacement = SlowLoadBackend()
    service, _state, deck_a, _deck_b, _events = make_service(
        factory_a=lambda: replacement,
        media_load_timeout_seconds=0.05,
    )
    deck_a.load(Track(71, "local.mp3", "Titel", "", "", 180), validate_file=False)
    old_backend = deck_a.backend

    result = service.recover_deck("A")

    assert entered.is_set()
    assert result.error_code == "MEDIA_LOAD_TIMEOUT"
    assert deck_a.backend is old_backend
    release.set()


def test_backend_release_timeout_keeps_valid_replacement_active() -> None:
    release = Event()

    class SlowCloseBackend(FakeAudioBackend):
        def close(self) -> None:
            release.wait(timeout=2)
            super().close()

    state = EmergencyStateService()
    old_backend = SlowCloseBackend()
    deck_a = DeckController("A", old_backend)
    deck_b = DeckController("B", FakeAudioBackend())
    replacement = FakeAudioBackend()
    service = AudioRecoveryService(
        state,
        deck_a,
        deck_b,
        {"A": lambda: replacement, "B": FakeAudioBackend},
        independent_players=True,
        preserves_shared_instance=True,
        preserves_output_device=True,
        backend_release_timeout_seconds=0.05,
    )

    result = service.recover_deck("A")

    assert result.success
    assert result.state == "RECOVERED_CLEANUP_PENDING"
    assert result.error_code == "BACKEND_RELEASE_TIMEOUT"
    assert deck_a.backend is replacement
    assert state.snapshot().deck_a == DeckHealth.HEALTHY
    release.set()


def test_explicit_global_recovery_replaces_both_backends_atomically_and_muted() -> None:
    replacement_a = FakeAudioBackend()
    replacement_b = FakeAudioBackend()
    service, state, deck_a, deck_b, events = make_service(
        factory_a=lambda: replacement_a,
        factory_b=lambda: replacement_b,
    )
    track_a = Track(81, "a.mp3", "A", "", "", 180)
    track_b = Track(82, "b.mp3", "B", "", "", 180)
    deck_a.load(track_a, validate_file=False)
    deck_b.load(track_b, validate_file=False)
    deck_a.play()
    deck_a.model.position = 30.0
    old_a, old_b = deck_a.backend, deck_b.backend

    result = service.recover_all_backends(GlobalRecoveryTrigger.OPERATOR_REQUEST)

    assert result.success
    assert result.state == "RECOVERED_MUTED"
    assert result.recovered_decks == ("A", "B")
    assert deck_a.backend is replacement_a and deck_b.backend is replacement_b
    assert deck_a.backend is not old_a and deck_b.backend is not old_b
    assert deck_a.emergency_muted and deck_b.emergency_muted
    assert replacement_a.volume == 0.0 and replacement_b.volume == 0.0
    assert replacement_a.is_playing()
    assert not replacement_b.is_playing()
    assert deck_a.model.position == 29.0
    assert state.snapshot().system == EmergencySystemState.NORMAL
    assert events[-1][0] == "AUDIO_GLOBAL_RECOVERY"


def test_global_preparation_failure_keeps_both_old_backends() -> None:
    replacement_a = FakeAudioBackend()

    def fail_b():
        raise RuntimeError("VLC-Instanz defekt")

    service, state, deck_a, deck_b, _events = make_service(
        factory_a=lambda: replacement_a,
        factory_b=fail_b,
    )
    old_a, old_b = deck_a.backend, deck_b.backend

    result = service.recover_all_backends(GlobalRecoveryTrigger.OPERATOR_REQUEST)

    assert not result.success
    assert result.error_code == "GLOBAL_RECOVERY_FAILED"
    assert deck_a.backend is old_a and deck_b.backend is old_b
    assert not deck_a.emergency_muted and not deck_b.emergency_muted
    assert state.snapshot().system == EmergencySystemState.RECOVERY_FAILED


def test_global_recovery_attempts_are_bounded() -> None:
    calls = 0

    def fail_a():
        nonlocal calls
        calls += 1
        raise RuntimeError("Backend defekt")

    service, _state, _deck_a, _deck_b, events = make_service(
        factory_a=fail_a,
        maximum_global_attempts=2,
    )

    results = [
        service.recover_all_backends(GlobalRecoveryTrigger.OPERATOR_REQUEST)
        for _attempt in range(3)
    ]

    assert calls == 2
    assert [result.attempt for result in results[:2]] == [1, 2]
    assert results[2].state == "BLOCKED"
    assert results[2].error_code == "GLOBAL_RECOVERY_ATTEMPTS_EXHAUSTED"
    assert events[-1][1]["attempts_remaining"] == 0


def test_global_player_creation_timeout_has_stable_code_and_keeps_old_backends() -> None:
    release = Event()

    def blocking_factory():
        release.wait(timeout=2)
        return FakeAudioBackend()

    service, _state, deck_a, deck_b, _events = make_service(
        factory_a=blocking_factory,
        player_creation_timeout_seconds=0.05,
    )
    old_a, old_b = deck_a.backend, deck_b.backend
    started = monotonic()

    result = service.recover_all_backends(GlobalRecoveryTrigger.OPERATOR_REQUEST)

    assert monotonic() - started < 0.5
    assert result.error_code == "GLOBAL_PLAYER_CREATION_TIMEOUT"
    assert deck_a.backend is old_a and deck_b.backend is old_b
    release.set()
