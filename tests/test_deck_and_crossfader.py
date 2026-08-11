"""Tests for independent decks, fades and volume calculations."""

from pathlib import Path

import pytest

from party_player.audio.fake_backend import FakeAudioBackend
from party_player.crossfader_service import CrossfaderService
from party_player.deck_controller import DeckController
from party_player.enums import DeckState
from party_player.models import Track
from party_player.loudness import ResolvedLoudnessSettings


def track(track_id: int, path: str = "song.mp3") -> Track:
    return Track(track_id, path, f"Song {track_id}", "Artist", "Album", 180.0)


def loaded_deck(deck_id: str, track_id: int) -> tuple[DeckController, FakeAudioBackend]:
    backend = FakeAudioBackend()
    deck = DeckController(deck_id, backend)
    deck.load(track(track_id), validate_file=False)
    return deck, backend


def test_two_decks_operate_independently() -> None:
    deck_a, backend_a = loaded_deck("A", 1)
    deck_b, backend_b = loaded_deck("B", 2)
    deck_a.play()
    deck_b.play()
    deck_a.pause()
    assert deck_a.model.state == DeckState.PAUSED
    assert deck_b.model.state == DeckState.PLAYING
    assert backend_a.paused and backend_b.playing


def test_finished_track_can_restart_immediately() -> None:
    deck, backend = loaded_deck("A", 1)
    deck.play()
    backend.playing = False
    backend.finished = True
    backend.position = backend.duration
    deck.update_status()
    assert deck.model.state.value == "finished"

    deck.play()

    assert deck.model.state == DeckState.PLAYING
    assert backend.position == 0.0
    assert backend.playing


def test_missing_file_sets_error_state(tmp_path: Path) -> None:
    deck = DeckController("A", FakeAudioBackend())
    missing = track(1, str(tmp_path / "missing.mp3"))
    with pytest.raises(FileNotFoundError):
        deck.load(missing)
    assert deck.model.state == DeckState.ERROR
    assert "nicht gefunden" in deck.model.error_message


def test_crossfader_and_master_do_not_overwrite_deck_volume() -> None:
    deck_a, backend_a = loaded_deck("A", 1)
    deck_b, backend_b = loaded_deck("B", 2)
    deck_a.set_volume(0.6)
    deck_b.set_volume(0.4)
    crossfader = CrossfaderService(deck_a, deck_b, position=0.0, master_volume=0.5)
    assert backend_a.volume == pytest.approx(0.3)
    assert backend_b.volume == pytest.approx(0.0)
    crossfader.set_position(1.0)
    assert backend_a.volume == pytest.approx(0.0)
    assert backend_b.volume == pytest.approx(0.2)
    assert deck_a.model.volume == 0.6
    assert deck_b.model.volume == 0.4


def test_transition_gate_mutes_only_the_selected_deck() -> None:
    deck_a, backend_a = loaded_deck("A", 1)
    deck_b, backend_b = loaded_deck("B", 2)
    mixer = CrossfaderService(deck_a, deck_b, position=0.5, master_volume=1.0)

    deck_b.set_transition_muted(True)
    mixer.apply()

    assert backend_a.volume == pytest.approx(2**-0.5)
    assert backend_b.volume == 0.0
    assert deck_a.model.volume == 1.0
    assert deck_b.model.volume == 1.0


def test_emergency_mute_is_immediate_and_does_not_rewrite_other_deck() -> None:
    deck_a, backend_a = loaded_deck("A", 1)
    deck_b, backend_b = loaded_deck("B", 2)
    deck_a.play()
    deck_b.play()
    mixer = CrossfaderService(deck_a, deck_b, position=0.5, master_volume=1.0)
    other_volume = backend_b.volume
    other_writes = backend_b.volume_write_count
    visible_state = (deck_a.model.volume, mixer.position, mixer.master_volume)

    deck_a.set_emergency_muted(True)

    assert backend_a.volume == 0.0
    assert backend_b.volume == other_volume
    assert backend_b.volume_write_count == other_writes
    assert (deck_a.model.volume, mixer.position, mixer.master_volume) == visible_state
    assert deck_a.backend.is_playing()


def test_emergency_mute_cancels_ramps_and_survives_track_load() -> None:
    deck, backend = loaded_deck("A", 1)
    callbacks: list[object] = []

    def schedule(_delay: int, callback: object) -> object:
        callbacks.append(callback)
        return callback

    deck.start_fade(0.0, 2.0, schedule)
    deck.smooth_normalization_factor(0.5, 2.0, schedule)
    deck.set_emergency_muted(True)
    fade_before = deck.fade_level
    normalization_before = deck.normalization_factor
    for callback in callbacks:
        assert callable(callback)
        callback()
    deck.load(track(3), validate_file=False)

    assert deck.emergency_muted
    assert deck.fade_level == fade_before
    assert deck.normalization_factor == normalization_before
    mixer = CrossfaderService(deck, DeckController("B", FakeAudioBackend()), position=0.0)
    assert mixer.effective_volumes()[0] == 0.0
    assert backend.volume == 0.0


def test_clearing_emergency_mute_restores_calculated_level() -> None:
    deck_a, backend_a = loaded_deck("A", 1)
    deck_b, _backend_b = loaded_deck("B", 2)
    mixer = CrossfaderService(deck_a, deck_b, position=0.0, master_volume=0.6)
    deck_a.set_emergency_muted(True)
    assert backend_a.volume == 0.0

    deck_a.set_emergency_muted(False)
    mixer.apply()

    assert backend_a.volume == pytest.approx(0.6)


def test_ducking_during_crossfade_affects_both_decks_without_changing_state() -> None:
    deck_a, backend_a = loaded_deck("A", 1)
    deck_b, backend_b = loaded_deck("B", 2)
    deck_a.play()
    deck_b.play()
    mixer = CrossfaderService(deck_a, deck_b, position=0.5, master_volume=0.8)
    visible_before = (
        deck_a.model.volume,
        deck_b.model.volume,
        mixer.position,
        mixer.master_volume,
        deck_a.model.state,
        deck_b.model.state,
    )
    effective_before = (backend_a.volume, backend_b.volume)

    mixer.set_ducking_factor(0.5)

    assert backend_a.volume == pytest.approx(effective_before[0] * 0.5)
    assert backend_b.volume == pytest.approx(effective_before[1] * 0.5)
    assert (
        deck_a.model.volume,
        deck_b.model.volume,
        mixer.position,
        mixer.master_volume,
        deck_a.model.state,
        deck_b.model.state,
    ) == visible_before


def test_equal_power_crossfader_factors_and_range() -> None:
    deck_a, _ = loaded_deck("A", 1)
    deck_b, _ = loaded_deck("B", 2)
    crossfader = CrossfaderService(deck_a, deck_b, position=0.5, master_volume=1.0)

    factor_a, factor_b = crossfader.factors()
    assert factor_a == pytest.approx(2**-0.5)
    assert factor_b == pytest.approx(2**-0.5)

    crossfader.set_position(-10)
    assert crossfader.position == 0.0
    assert crossfader.factors() == pytest.approx((1.0, 0.0))
    crossfader.set_position(10)
    assert crossfader.position == 1.0
    assert crossfader.factors() == pytest.approx((0.0, 1.0), abs=1e-12)


def test_master_mute_is_separate_from_visible_master_volume() -> None:
    deck_a, _ = loaded_deck("A", 1)
    deck_b, _ = loaded_deck("B", 2)
    deck_a.set_volume(0.6)
    deck_b.set_volume(0.4)
    crossfader = CrossfaderService(deck_a, deck_b, position=0.25, master_volume=0.7)
    crossfader.mute()
    assert crossfader.master_volume == pytest.approx(0.7)
    assert crossfader.master_muted
    assert crossfader.effective_volumes() == (0.0, 0.0)
    crossfader.set_master_volume(0.45)
    assert crossfader.master_volume == pytest.approx(0.45)
    assert crossfader.effective_volumes() == (0.0, 0.0)
    assert crossfader.position == 0.25
    assert deck_a.model.volume == 0.6
    assert deck_b.model.volume == 0.4
    crossfader.unmute()
    assert crossfader.master_volume == pytest.approx(0.45)
    assert not crossfader.master_muted
    assert crossfader.position == 0.25
    assert deck_a.model.volume == 0.6
    assert deck_b.model.volume == 0.4


def test_panic_mute_overrides_all_audio_factors_without_destroying_them() -> None:
    deck_a, backend_a = loaded_deck("A", 1)
    deck_b, backend_b = loaded_deck("B", 2)
    deck_a.fade_level = 0.4
    deck_a.normalization_factor = 1.3
    deck_a.set_volume(0.7)
    deck_b.fade_level = 0.8
    deck_b.normalization_factor = 0.6
    deck_b.set_volume(0.9)
    mixer = CrossfaderService(deck_a, deck_b, position=0.35, master_volume=0.65)
    state_before = (
        deck_a.fade_level,
        deck_a.normalization_factor,
        deck_a.model.volume,
        deck_b.fade_level,
        deck_b.normalization_factor,
        deck_b.model.volume,
        mixer.position,
        mixer.master_volume,
    )
    levels_before = mixer.effective_volumes()

    mixer.set_panic_muted(True)

    assert mixer.panic_muted
    assert mixer.effective_volumes() == (0.0, 0.0)
    assert backend_a.volume == 0.0
    assert backend_b.volume == 0.0
    assert state_before == (
        deck_a.fade_level,
        deck_a.normalization_factor,
        deck_a.model.volume,
        deck_b.fade_level,
        deck_b.normalization_factor,
        deck_b.model.volume,
        mixer.position,
        mixer.master_volume,
    )

    mixer.set_panic_muted(False)

    assert mixer.effective_volumes() == pytest.approx(levels_before)


def test_unchanged_mixer_poll_does_not_rewrite_backend_volumes() -> None:
    deck_a, backend_a = loaded_deck("A", 1)
    deck_b, backend_b = loaded_deck("B", 2)
    crossfader = CrossfaderService(deck_a, deck_b)
    writes = (backend_a.volume_write_count, backend_b.volume_write_count)

    for _ in range(20):
        crossfader.apply()

    assert (backend_a.volume_write_count, backend_b.volume_write_count) == writes


def test_fade_is_scheduled_and_cancellable() -> None:
    deck, _ = loaded_deck("A", 1)
    callbacks: list[object] = []

    def schedule(_delay: int, callback: object) -> object:
        callbacks.append(callback)
        return callback

    deck.fade_level = 0.0
    deck.start_fade(1.0, 1.0, schedule)
    first = callbacks.pop(0)
    assert callable(first)
    first()
    assert 0 < deck.fade_level < 1
    deck.cancel_fade()
    pending = callbacks.pop(0)
    assert callable(pending)
    pending()
    assert deck.fade_level < 1


def test_fade_out_reaches_zero_and_optionally_stops_deck() -> None:
    deck, _backend = loaded_deck("A", 1)
    deck.play()
    callbacks: list[object] = []

    def schedule(_delay: int, callback: object) -> object:
        callbacks.append(callback)
        return callback

    deck.start_fade(0.0, 0.1, schedule, stop_after=True)
    while callbacks:
        callback = callbacks.pop(0)
        assert callable(callback)
        callback()

    assert deck.fade_level == pytest.approx(0.0)
    assert deck.model.state == DeckState.STOPPED


def test_fade_does_not_modify_master_or_crossfader() -> None:
    deck_a, _ = loaded_deck("A", 1)
    deck_b, _ = loaded_deck("B", 2)
    mixer = CrossfaderService(deck_a, deck_b, position=0.3, master_volume=0.65)
    callbacks: list[object] = []

    def schedule(_delay: int, callback: object) -> object:
        callbacks.append(callback)
        return callback

    deck_a.start_fade(0.0, 1.0, schedule)
    callback = callbacks.pop(0)
    assert callable(callback)
    callback()

    assert mixer.master_volume == 0.65
    assert mixer.position == 0.3


def _start_gain_ramp(
    deck: DeckController,
) -> list[object]:
    callbacks: list[object] = []
    settings = ResolvedLoudnessSettings(
        requested_gain_db=-6.0,
        effective_gain_db=-6.0,
        linear_gain_factor=10 ** (-6 / 20),
        source="REPLAYGAIN_TAG",
        peak_limited=False,
        normalization_mode="TRACK",
    )
    deck.smooth_resolved_loudness(
        settings,
        1.0,
        lambda _delay, callback: callbacks.append(callback),
    )
    first = callbacks.pop(0)
    assert callable(first)
    first()
    assert 10 ** (-6 / 20) < deck.normalization_factor < 1.0
    return callbacks


def _run_callbacks(callbacks: list[object]) -> None:
    while callbacks:
        callback = callbacks.pop(0)
        assert callable(callback)
        callback()


def test_track_change_cancels_old_gain_ramp_and_resets_loudness() -> None:
    deck, _backend = loaded_deck("A", 1)
    callbacks = _start_gain_ramp(deck)

    deck.load(track(2, "next.mp3"), validate_file=False)
    _run_callbacks(callbacks)

    assert deck.model.loaded_track == track(2, "next.mp3")
    assert deck.normalization_factor == 1.0
    assert deck.model.loudness_source == "NONE"


def test_eject_cancels_old_gain_ramp() -> None:
    deck, _backend = loaded_deck("A", 1)
    callbacks = _start_gain_ramp(deck)

    deck.eject()
    _run_callbacks(callbacks)

    assert deck.model.loaded_track is None
    assert deck.normalization_factor == 1.0
    assert deck.model.loudness_source == "NONE"


def test_stop_cancels_gain_ramp_at_resolved_target() -> None:
    deck, _backend = loaded_deck("A", 1)
    callbacks = _start_gain_ramp(deck)

    deck.stop()
    settled = deck.normalization_factor
    _run_callbacks(callbacks)

    assert deck.model.state == DeckState.STOPPED
    assert settled == pytest.approx(10 ** (-6 / 20))
    assert deck.normalization_factor == settled


def test_error_cancels_gain_ramp_at_resolved_target(tmp_path: Path) -> None:
    deck, _backend = loaded_deck("A", 1)
    callbacks = _start_gain_ramp(deck)

    with pytest.raises(FileNotFoundError):
        deck.load(track(2, str(tmp_path / "missing.mp3")))
    settled = deck.normalization_factor
    _run_callbacks(callbacks)

    assert deck.model.state == DeckState.ERROR
    assert settled == pytest.approx(10 ** (-6 / 20))
    assert deck.normalization_factor == settled


def test_gain_smoothing_is_deterministic_and_keeps_mixer_controls_unchanged() -> None:
    deck_a, backend_a = loaded_deck("A", 1)
    deck_b, _backend_b = loaded_deck("B", 2)
    deck_a.set_volume(0.7)
    deck_b.set_volume(0.4)
    mixer = CrossfaderService(deck_a, deck_b, position=0.25, master_volume=0.6)
    deck_a.set_volume_changed_callback(mixer.apply)
    deck_b.set_volume_changed_callback(mixer.apply)
    callbacks: list[tuple[int, object]] = []
    factors = [deck_a.normalization_factor]
    target = 10 ** (-6 / 20)
    settings = ResolvedLoudnessSettings(
        requested_gain_db=-6.0,
        effective_gain_db=-6.0,
        linear_gain_factor=target,
        source="REPLAYGAIN_TAG",
        peak_limited=False,
        normalization_mode="TRACK",
    )

    def schedule(delay_ms: int, callback: object) -> object:
        callbacks.append((delay_ms, callback))
        return callback

    deck_a.smooth_resolved_loudness(settings, 0.1, schedule)
    while callbacks:
        delay_ms, callback = callbacks.pop(0)
        assert delay_ms == 20
        assert callable(callback)
        callback()
        factors.append(deck_a.normalization_factor)

    assert factors == sorted(factors, reverse=True)
    assert deck_a.normalization_factor == pytest.approx(target)
    assert backend_a.volume == pytest.approx(
        0.7 * mixer.factors()[0] * mixer.master_volume * target
    )
    assert deck_a.model.volume == 0.7
    assert deck_b.model.volume == 0.4
    assert mixer.position == 0.25
    assert mixer.master_volume == 0.6


def test_resolved_loudness_configures_optional_runtime_clip_protection() -> None:
    backend = FakeAudioBackend()
    backend.runtime_clip_protection_supported = True
    deck = DeckController("A", backend)
    settings = ResolvedLoudnessSettings(
        requested_gain_db=3.0,
        effective_gain_db=2.0,
        linear_gain_factor=10 ** (2 / 20),
        source="ANALYSIS",
        peak_limited=True,
        normalization_mode="TRACK",
        runtime_clip_protection_enabled=True,
        output_peak_ceiling_dbfs=-1.5,
    )

    deck.set_resolved_loudness(settings)

    assert backend.runtime_clip_protection == (True, -1.5)


def test_clearing_loudness_disables_supported_runtime_clip_protection() -> None:
    backend = FakeAudioBackend()
    backend.runtime_clip_protection_supported = True
    deck = DeckController("A", backend)

    deck.set_resolved_loudness(None)

    assert backend.runtime_clip_protection == (False, 0.0)
