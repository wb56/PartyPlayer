"""Audio-profile regressions from PCM through robust cue estimation."""

import math

import pytest

from party_player.analysis import (
    CueBoundaryEstimator,
    PcmChunk,
    PcmLevelAnalyzer,
    SignalDetectionSettings,
    StreamingSignalDetector,
)


def estimate_from_amplitudes(
    amplitudes: list[float],
    *,
    sample_rate: int = 100,
    frames_per_value: int = 10,
) -> tuple[float, float]:
    analyzer = PcmLevelAnalyzer(window_seconds=frames_per_value / sample_rate)
    detector = StreamingSignalDetector(
        SignalDetectionSettings(
            signal_on_dbfs=-45.0,
            signal_off_dbfs=-50.0,
            minimum_signal_seconds=0.3,
            minimum_silence_seconds=0.3,
        )
    )
    samples = tuple(amplitude for amplitude in amplitudes for _frame in range(frames_per_value))
    levels = list(analyzer.consume(PcmChunk(0.0, sample_rate, 1, samples)))
    levels.extend(analyzer.finish())
    regions = [region for measured in levels for region in detector.consume(measured)]
    regions.extend(detector.finish())
    duration = len(samples) / sample_rate
    cues = CueBoundaryEstimator().estimate(duration, tuple(regions))
    return cues.cue_in, cues.cue_out


def test_leading_and_trailing_silence_produce_stable_boundaries() -> None:
    cue_in, cue_out = estimate_from_amplitudes([0.0] * 20 + [0.5] * 60 + [0.0] * 20)

    assert cue_in == pytest.approx(2.0)
    assert cue_out == pytest.approx(8.0)


def test_quiet_intro_below_threshold_is_not_mistaken_for_sustained_signal() -> None:
    quiet = 10 ** (-60 / 20)
    cue_in, _cue_out = estimate_from_amplitudes([0.0] * 5 + [quiet] * 15 + [0.4] * 70 + [0.0] * 10)

    assert cue_in == pytest.approx(2.0)


def test_fade_out_uses_confirmed_threshold_crossing_as_cue_out() -> None:
    fade = [10 ** (db / 20) for db in range(-10, -61, -5)]
    _cue_in, cue_out = estimate_from_amplitudes([0.5] * 60 + fade + [0.0] * 30)

    expected_first_below_off = (
        60 + next(i for i, db in enumerate(range(-10, -61, -5)) if db < -50)
    ) / 10
    assert cue_out == pytest.approx(expected_first_below_off)


def test_short_peak_and_low_environment_noise_do_not_shift_cue_in() -> None:
    noise = 10 ** (-65 / 20)
    cue_in, _cue_out = estimate_from_amplitudes(
        [noise] * 5 + [0.8] + [noise] * 14 + [0.4] * 60 + [0.0] * 20
    )

    assert math.isclose(cue_in, 2.0)
