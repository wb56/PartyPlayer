"""Public playback port tests for resolved loudness settings."""

import pytest

from party_player.audio.fake_backend import FakeAudioBackend
from party_player.deck_controller import DeckController
from party_player.loudness import ResolvedLoudnessSettings
from party_player.loudness_playback import (
    DeckResolvedLoudnessPlayback,
    ResolvedLoudnessPlayback,
)


def test_public_port_applies_pre_resolved_gain_to_requested_deck_only() -> None:
    deck_a = DeckController("A", FakeAudioBackend())
    deck_b = DeckController("B", FakeAudioBackend())
    playback = DeckResolvedLoudnessPlayback(deck_a, deck_b)
    contract: ResolvedLoudnessPlayback = playback
    settings = ResolvedLoudnessSettings(
        requested_gain_db=-6.0,
        effective_gain_db=-6.0,
        linear_gain_factor=10 ** (-6 / 20),
        source="REPLAYGAIN_TAG",
        peak_limited=False,
        normalization_mode="TRACK",
    )

    assert isinstance(playback, ResolvedLoudnessPlayback)
    contract.apply_resolved_loudness("A", settings)

    assert deck_a.normalization_factor == pytest.approx(10 ** (-6 / 20))
    assert deck_a.model.loudness_requested_gain_db == -6.0
    assert deck_a.model.loudness_effective_gain_db == -6.0
    assert deck_a.model.loudness_source == "REPLAYGAIN_TAG"
    assert not deck_a.model.loudness_peak_limited
    assert deck_b.normalization_factor == 1.0
    assert deck_b.model.loudness_source == "NONE"


def test_none_resets_gain_and_unknown_deck_is_rejected() -> None:
    deck_a = DeckController("A", FakeAudioBackend())
    deck_b = DeckController("B", FakeAudioBackend())
    playback = DeckResolvedLoudnessPlayback(deck_a, deck_b)
    deck_a.set_normalization_factor(0.5)

    playback.apply_resolved_loudness("A", None)

    assert deck_a.normalization_factor == 1.0
    assert deck_a.model.loudness_requested_gain_db == 0.0
    assert deck_a.model.loudness_effective_gain_db == 0.0
    assert deck_a.model.loudness_source == "NONE"
    assert not deck_a.model.loudness_peak_limited
    with pytest.raises(ValueError, match="Unbekanntes Deck"):
        playback.apply_resolved_loudness("C", None)
