"""Configurable sustained-signal detection and hysteresis tests."""

import pytest

from party_player.analysis import (
    PcmLevelWindow,
    SignalDetectionSettings,
    StreamingSignalDetector,
)


def level(start: float, dbfs: float, duration: float = 0.1) -> PcmLevelWindow:
    return PcmLevelWindow(start, duration, 0.1, dbfs, 0.2)


def test_signal_is_accepted_only_after_configured_minimum_duration() -> None:
    detector = StreamingSignalDetector(SignalDetectionSettings(minimum_signal_seconds=0.3))
    for index in range(2):
        assert detector.consume(level(index / 10, -30.0)) == ()
    assert detector.finish() == ()

    detector = StreamingSignalDetector(SignalDetectionSettings(minimum_signal_seconds=0.3))
    for index in range(3):
        detector.consume(level(index / 10, -30.0))
    regions = detector.finish()

    assert len(regions) == 1
    assert regions[0].start_seconds == 0.0
    assert regions[0].end_seconds == pytest.approx(0.3)


def test_hysteresis_keeps_active_signal_between_on_and_off_thresholds() -> None:
    detector = StreamingSignalDetector(
        SignalDetectionSettings(
            signal_on_dbfs=-40.0,
            signal_off_dbfs=-50.0,
            minimum_signal_seconds=0.1,
            minimum_silence_seconds=0.1,
        )
    )

    assert detector.consume(level(0.0, -35.0)) == ()
    assert detector.consume(level(0.1, -45.0)) == ()
    completed = detector.consume(level(0.2, -55.0))

    assert len(completed) == 1
    assert completed[0].start_seconds == 0.0
    assert completed[0].end_seconds == 0.2


def test_level_below_release_threshold_discards_unconfirmed_candidate() -> None:
    detector = StreamingSignalDetector(SignalDetectionSettings(minimum_signal_seconds=0.3))
    detector.consume(level(0.0, -30.0))
    detector.consume(level(0.1, -60.0))
    for index in range(2, 4):
        detector.consume(level(index / 10, -30.0))

    assert detector.finish() == ()


def test_time_gap_finishes_region_instead_of_joining_edge_segments() -> None:
    detector = StreamingSignalDetector(SignalDetectionSettings(minimum_signal_seconds=0.1))
    detector.consume(level(0.0, -30.0))

    completed = detector.consume(level(255.0, -30.0))
    final = detector.finish()

    assert completed[0].end_seconds == 0.1
    assert final[0].start_seconds == 255.0


def test_isolated_short_peak_never_becomes_a_signal_region() -> None:
    detector = StreamingSignalDetector(SignalDetectionSettings(minimum_signal_seconds=0.3))
    levels = (-80.0, -20.0, -80.0, -80.0)

    completed = tuple(
        region
        for index, value in enumerate(levels)
        for region in detector.consume(level(index / 10, value))
    )

    assert completed + detector.finish() == ()


def test_short_level_drop_does_not_split_sustained_signal() -> None:
    detector = StreamingSignalDetector(
        SignalDetectionSettings(
            minimum_signal_seconds=0.1,
            minimum_silence_seconds=0.2,
        )
    )
    for index, value in enumerate((-30.0, -60.0, -30.0, -60.0, -60.0)):
        completed = detector.consume(level(index / 10, value))

    assert len(completed) == 1
    assert completed[0].start_seconds == 0.0
    assert completed[0].end_seconds == pytest.approx(0.3)


@pytest.mark.parametrize(
    "settings",
    [
        SignalDetectionSettings(signal_on_dbfs=-45.0, signal_off_dbfs=-50.0),
        SignalDetectionSettings(signal_on_dbfs=-35.0, signal_off_dbfs=-60.0),
    ],
)
def test_thresholds_are_explicitly_configurable(settings: SignalDetectionSettings) -> None:
    assert StreamingSignalDetector(settings).settings == settings


def test_invalid_hysteresis_and_duration_are_rejected() -> None:
    with pytest.raises(ValueError, match="Einschaltschwelle"):
        SignalDetectionSettings(signal_on_dbfs=-50.0, signal_off_dbfs=-45.0)
    with pytest.raises(ValueError, match="Mindest-Signaldauer"):
        SignalDetectionSettings(minimum_signal_seconds=0.0)
    with pytest.raises(ValueError, match="Mindest-Stilledauer"):
        SignalDetectionSettings(minimum_silence_seconds=0.0)
