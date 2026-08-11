from pathlib import Path

import pytest

from party_player.audio.fake_backend import FakeAudioBackend
from party_player.crossfader_service import CrossfaderService
from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.deck_controller import DeckController
from party_player.loudness import LoudnessRepository, LoudnessService


def _repository(tmp_path: Path) -> LoudnessRepository:
    database = Database(tmp_path / "loudness.db")
    migrate(database)
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO tracks
               (id, file_path, title, artist, album, duration_seconds)
               VALUES (1, 'one.mp3', 'One', '', '', 100),
                      (2, 'two.mp3', 'Two', '', '', 100)"""
        )
    return LoudnessRepository(database)


def test_replaygain_is_persistent_and_converted_to_linear_factor(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.save_replaygain(1, -6.0, 0.9, None, None)

    resolved = LoudnessService(repository).resolve(1)

    assert resolved.source == "REPLAYGAIN_TAG"
    assert resolved.effective_gain_db == -6.0
    assert resolved.linear_gain_factor == pytest.approx(10 ** (-6 / 20))
    assert repository.get(1).replaygain_scanned_at is not None
    assert repository.get(1).metadata_status == "COMPLETE"


def test_missing_replaygain_tags_are_remembered_as_scanned(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    repository.save_replaygain(1, None, None, None, None)

    stored = repository.get(1)
    assert stored.replaygain_track_gain_db is None
    assert stored.replaygain_scanned_at is not None
    assert stored.metadata_status == "INCOMPLETE"


def test_analysis_fallback_uses_configured_target_and_true_peak(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.save_analysis(
        1,
        integrated_loudness_lufs=-20.0,
        loudness_range_lu=5.0,
        true_peak_dbfs=-3.0,
        source="EBU_R128",
        version="v1",
        method="EBU R128 / ITU-R BS.1770",
    )

    resolved = LoudnessService(
        repository,
        target_loudness_lufs=-14.0,
        maximum_output_peak_db=0.0,
        headroom_db=1.0,
    ).resolve(1)

    assert resolved.source == "ANALYSIS"
    assert resolved.requested_gain_db == 6.0
    assert resolved.effective_gain_db == pytest.approx(2.0)
    assert resolved.peak_limited


def test_replaygain_has_priority_over_analysis_fallback(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.save_analysis(
        1,
        integrated_loudness_lufs=-20.0,
        loudness_range_lu=5.0,
        true_peak_dbfs=-3.0,
        source="EBU_R128",
        version="v1",
        method="EBU R128 / ITU-R BS.1770",
    )
    repository.save_replaygain(1, -4.0, 0.8, None, None)

    resolved = LoudnessService(repository, target_loudness_lufs=-14.0).resolve(1)

    assert resolved.source == "REPLAYGAIN_TAG"
    assert resolved.requested_gain_db == -4.0


def test_invalid_refresh_values_do_not_replace_valid_replaygain(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.save_replaygain(1, -4.0, 0.8, -3.0, 0.9)

    repository.save_replaygain(1, None, None, 2.0, None)

    stored = repository.get(1)
    assert stored.replaygain_track_gain_db == -4.0
    assert stored.replaygain_track_peak == 0.8
    assert stored.replaygain_album_gain_db == 2.0
    assert stored.replaygain_album_peak == 0.9


def test_positive_gain_is_peak_limited(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.save_replaygain(1, 8.0, 0.8, None, None)

    resolved = LoudnessService(repository).resolve(1)

    safe_gain = -1.0 - 20.0 * __import__("math").log10(0.8)
    assert resolved.effective_gain_db == pytest.approx(safe_gain)
    assert resolved.peak_limited


def test_clip_protection_can_be_disabled_without_disabling_normalization(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    repository.save_replaygain(1, 6.0, 0.8, None, None)

    resolved = LoudnessService(
        repository,
        clip_protection_enabled=False,
    ).resolve(1)

    assert resolved.source == "REPLAYGAIN_TAG"
    assert resolved.effective_gain_db == 6.0
    assert not resolved.peak_limited


def test_peak_ceiling_and_headroom_are_applied_exactly_once(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.save_replaygain(1, 8.0, 0.5, None, None)

    resolved = LoudnessService(
        repository,
        maximum_output_peak_db=-0.5,
        headroom_db=1.5,
    ).resolve(1)

    # A 0.5 linear peak is about -6.0206 dBFS. The explicit safe ceiling is
    # -0.5 dBFS minus 1.5 dB headroom, hence roughly +4.0206 dB gain.
    assert resolved.effective_gain_db == pytest.approx(-2.0 - 20.0 * __import__("math").log10(0.5))


def test_missing_peak_uses_conservative_positive_limit(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.save_replaygain(1, 7.0, None, None, None)

    resolved = LoudnessService(repository).resolve(1)

    assert resolved.effective_gain_db == 3.0
    assert resolved.peak_limited
    assert repository.get(1).metadata_status == "INCOMPLETE"


def test_positive_gain_cannot_exceed_backend_volume_ceiling(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.save_replaygain(1, 12.0, 0.1, None, None)

    resolved = LoudnessService(
        repository,
        maximum_positive_gain_db=12.0,
        maximum_backend_volume_factor=2.0,
    ).resolve(1)

    assert resolved.effective_gain_db == pytest.approx(20.0 * __import__("math").log10(2.0))
    assert resolved.linear_gain_factor == pytest.approx(2.0)
    assert resolved.peak_limited


def test_manual_override_has_priority_and_can_be_reset(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.save_replaygain(1, -4.0, 0.8, None, None)
    service = LoudnessService(repository)
    service.save_manual_gain(1, 2.0)
    assert service.resolve(1).source == "MANUAL"
    assert service.resolve(1).requested_gain_db == 2.0

    service.save_manual_gain(1, None)
    assert service.resolve(1).requested_gain_db == -4.0


def test_album_mode_prefers_album_gain_and_its_peak(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.save_replaygain(1, -4.0, 0.9, -7.0, 0.7)

    resolved = LoudnessService(repository, mode="ALBUM").resolve(1)

    assert resolved.requested_gain_db == -7.0
    assert resolved.effective_gain_db == -7.0
    assert resolved.source == "REPLAYGAIN_TAG"
    assert resolved.normalization_mode == "ALBUM"


def test_album_mode_falls_back_to_track_gain_when_album_gain_is_missing(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    repository.save_replaygain(1, -4.0, 0.9, None, 0.7)

    resolved = LoudnessService(repository, mode="ALBUM").resolve(1)

    assert resolved.requested_gain_db == -4.0
    assert resolved.effective_gain_db == -4.0


def test_track_mode_does_not_use_album_gain_as_fallback(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.save_replaygain(1, None, None, -7.0, 0.7)

    resolved = LoudnessService(repository, mode="TRACK").resolve(1)

    assert resolved.source == "NONE"
    assert resolved.effective_gain_db == 0.0


def test_album_mode_uses_track_fallback_for_non_finite_album_gain(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    repository.save_replaygain(1, -3.0, 0.9, float("inf"), 0.7)

    resolved = LoudnessService(repository, mode="ALBUM").resolve(1)

    assert resolved.requested_gain_db == -3.0
    assert resolved.effective_gain_db == -3.0


def test_manual_gain_has_priority_in_album_mode(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.save_replaygain(1, -4.0, 0.9, -7.0, 0.7)
    service = LoudnessService(repository, mode="ALBUM")
    service.save_manual_gain(1, 2.0)

    resolved = service.resolve(1)

    assert resolved.requested_gain_db == 2.0
    assert resolved.source == "MANUAL"


def test_missing_data_uses_neutral_gain(tmp_path: Path) -> None:
    resolved = LoudnessService(_repository(tmp_path)).resolve(1)
    assert resolved.source == "NONE"
    assert resolved.effective_gain_db == 0.0
    assert resolved.linear_gain_factor == 1.0


def test_normalization_is_independent_per_deck_and_crossfader(tmp_path: Path) -> None:
    deck_a = DeckController("A", FakeAudioBackend())
    deck_b = DeckController("B", FakeAudioBackend())
    deck_a.set_normalization_factor(0.5)
    deck_b.set_normalization_factor(1.5)
    mixer = CrossfaderService(deck_a, deck_b, position=0.5, master_volume=0.8)

    factor = 2**-0.5
    assert mixer.effective_volumes() == pytest.approx((0.5 * factor * 0.8, 1.5 * factor * 0.8))
    assert deck_a.model.volume == 1.0
    assert deck_b.model.volume == 1.0
