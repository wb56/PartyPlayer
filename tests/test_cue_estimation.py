"""Automatic cue-boundary and safe fade suggestion tests."""

import pytest

from party_player.analysis import (
    CueBoundaryEstimator,
    CueBoundarySettings,
    SignalRegion,
)


def region(start: float, end: float) -> SignalRegion:
    return SignalRegion(start, end, 0.8, -3.0)


def test_estimator_uses_first_head_signal_and_last_tail_signal() -> None:
    estimator = CueBoundaryEstimator()

    cues = estimator.estimate(
        180.0,
        (region(2.4, 45.0), region(135.0, 177.5)),
    )

    assert cues.cue_in == 2.4
    assert cues.cue_out == 177.5
    assert cues.suggested_fade_duration == 7.0


def test_missing_edge_detection_falls_back_to_safe_file_boundary() -> None:
    estimator = CueBoundaryEstimator()

    no_regions = estimator.estimate(180.0, ())
    head_only = estimator.estimate(180.0, (region(3.0, 40.0),))
    tail_only = estimator.estimate(180.0, (region(140.0, 178.0),))

    assert (no_regions.cue_in, no_regions.cue_out) == (0.0, 180.0)
    assert (head_only.cue_in, head_only.cue_out) == (3.0, 180.0)
    assert (tail_only.cue_in, tail_only.cue_out) == (0.0, 178.0)


def test_short_track_fade_is_bounded_by_half_usable_duration() -> None:
    estimator = CueBoundaryEstimator(
        CueBoundarySettings(
            edge_window_seconds=45.0,
            preferred_fade_seconds=7.0,
            minimum_fade_seconds=0.5,
        )
    )

    cues = estimator.estimate(6.0, (region(1.0, 5.0),))

    assert cues.usable_duration == 4.0
    assert cues.suggested_fade_duration == 2.0


def test_very_short_track_never_receives_an_impossible_minimum_fade() -> None:
    cues = CueBoundaryEstimator().estimate(0.6, (region(0.1, 0.5),))

    assert cues.usable_duration == pytest.approx(0.4)
    assert cues.suggested_fade_duration == pytest.approx(0.2)
    assert cues.suggested_fade_duration < cues.usable_duration


def test_invalid_or_out_of_range_regions_cannot_create_unsafe_cues() -> None:
    cues = CueBoundaryEstimator().estimate(
        120.0,
        (
            region(-2.0, 5.0),
            region(119.0, 130.0),
            region(3.0, 40.0),
            region(80.0, 118.0),
        ),
    )

    assert (cues.cue_in, cues.cue_out) == (3.0, 118.0)
