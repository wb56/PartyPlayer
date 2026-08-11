"""Equalizer curve resolution and deck-local application."""

import pytest

from party_player.audio.fake_backend import FakeAudioBackend
from party_player.crossfader_service import CrossfaderService
from party_player.deck_controller import DeckController
from party_player.equalizer import (
    BUILTIN_EQUALIZER_PRESETS,
    EqualizerPreset,
    EqualizerService,
    ResolvedEqualizerPreset,
)
from party_player.models import Track


def _track(track_id: int = 1) -> Track:
    return Track(track_id, "song.mp3", "Song", "Artist", "Album", 180.0)


def test_disabled_equalizer_has_no_backend_bands() -> None:
    resolved = EqualizerService().builtin(None, (60.0, 1000.0), source="GLOBAL")

    assert not resolved.enabled
    assert resolved.name == "Aus"
    assert resolved.band_gains_db == ()


def test_builtin_curve_is_resolved_to_dynamic_backend_frequencies() -> None:
    frequencies = (80.0, 250.0, 1000.0, 4000.0, 12000.0)
    resolved = EqualizerService().builtin("rock", frequencies)

    assert resolved.band_frequencies_hz == frequencies
    assert len(resolved.band_gains_db) == len(frequencies)
    assert resolved.preamp_db <= -max(0.0, *resolved.band_gains_db)
    assert all(-3.0 <= gain <= 3.0 for gain in resolved.band_gains_db)


def test_unsafe_preamp_is_reduced_to_cover_largest_boost() -> None:
    preset = EqualizerPreset("test", "Test", 0.0, ((60.0, 4.0), (1000.0, 2.0)))

    resolved = EqualizerService().resolve(preset, (60.0, 1000.0))

    assert resolved.preamp_db == -4.0


def test_invalid_values_are_rejected() -> None:
    preset = EqualizerPreset("bad", "Bad", 0.0, ((60.0, 21.0),))

    with pytest.raises(ValueError, match="sicheren VLC-Bereich"):
        EqualizerService().resolve(preset, (60.0,))


def test_fake_backend_skips_identical_snapshot() -> None:
    backend = FakeAudioBackend()
    deck = DeckController("A", backend)
    resolved = EqualizerService().builtin("pop", deck.equalizer_band_frequencies())

    assert deck.apply_equalizer(resolved)
    assert not deck.apply_equalizer(resolved)
    assert backend.equalizer_apply_count == 1
    assert backend.equalizer_skip_count == 1
    assert deck.model.equalizer_preset_name == "Pop"
    assert deck.model.equalizer_applied


def test_decks_keep_independent_equalizer_snapshots() -> None:
    backend_a = FakeAudioBackend()
    backend_b = FakeAudioBackend()
    deck_a = DeckController("A", backend_a)
    deck_b = DeckController("B", backend_b)
    service = EqualizerService()

    deck_a.apply_equalizer(service.builtin("bluesrock", deck_a.equalizer_band_frequencies()))
    deck_b.apply_equalizer(service.builtin("dance", deck_b.equalizer_band_frequencies()))

    assert backend_a.equalizer is not None
    assert backend_b.equalizer is not None
    assert backend_a.equalizer.preset_id == "bluesrock"
    assert backend_b.equalizer.preset_id == "dance"


def test_every_builtin_preset_passes_resolved_safety_validation() -> None:
    service = EqualizerService()
    frequencies = FakeAudioBackend().equalizer_band_frequencies()

    for preset_id in BUILTIN_EQUALIZER_PRESETS:
        service.validate_resolved(service.builtin(preset_id, frequencies))


def test_resolved_snapshot_rejects_band_length_mismatch() -> None:
    resolved = ResolvedEqualizerPreset("bad", "Bad", -1.0, (60.0,), (), "GLOBAL")

    with pytest.raises(ValueError, match="unterschiedliche Länge"):
        EqualizerService().validate_resolved(resolved)


def test_eject_resets_visible_equalizer_state_and_reload_accepts_new_preset() -> None:
    backend = FakeAudioBackend()
    deck = DeckController("A", backend)
    service = EqualizerService()
    deck.load(_track(), validate_file=False)
    deck.apply_equalizer(service.builtin("rock", deck.equalizer_band_frequencies()))

    deck.eject()

    assert deck.model.equalizer_preset_name == "Aus"
    assert deck.model.equalizer_source == "DISABLED"
    assert not deck.model.equalizer_applied

    deck.load(_track(2), validate_file=False)
    deck.apply_equalizer(service.builtin("pop", deck.equalizer_band_frequencies()))
    assert deck.model.equalizer_preset_name == "Pop"
    assert backend.equalizer is not None
    assert backend.equalizer.preset_id == "pop"


def test_backend_error_disables_only_equalizer_and_keeps_loaded_track() -> None:
    class FailingEqualizerBackend(FakeAudioBackend):
        def apply_equalizer(self, preset: ResolvedEqualizerPreset) -> bool:
            if preset.enabled:
                raise RuntimeError("EQ nicht verfügbar")
            return super().apply_equalizer(preset)

    backend = FailingEqualizerBackend()
    deck = DeckController("A", backend)
    track = _track()
    deck.load(track, validate_file=False)

    changed = deck.apply_equalizer(
        EqualizerService().builtin("dance", deck.equalizer_band_frequencies())
    )

    assert not changed
    assert deck.model.loaded_track == track
    assert deck.model.state.value == "loaded"
    assert deck.model.equalizer_source == "ERROR"
    assert deck.model.equalizer_error == "EQ nicht verfügbar"
    assert backend.equalizer is not None
    assert not backend.equalizer.enabled


def test_crossfader_volume_updates_never_reapply_equalizer() -> None:
    backend_a = FakeAudioBackend()
    backend_b = FakeAudioBackend()
    deck_a = DeckController("A", backend_a)
    deck_b = DeckController("B", backend_b)
    service = EqualizerService()
    deck_a.apply_equalizer(service.builtin("rock", deck_a.equalizer_band_frequencies()))
    deck_b.apply_equalizer(service.builtin("pop", deck_b.equalizer_band_frequencies()))
    mixer = CrossfaderService(deck_a, deck_b)
    apply_counts = (backend_a.equalizer_apply_count, backend_b.equalizer_apply_count)

    for position in (0.0, 0.2, 0.5, 0.8, 1.0):
        mixer.set_position(position)
        mixer.apply()

    assert (backend_a.equalizer_apply_count, backend_b.equalizer_apply_count) == apply_counts
