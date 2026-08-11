"""Versioned automatic cue-analysis result model tests."""

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path

import pytest

from party_player.analysis import (
    CueAnalysisResult,
    DetectedCueBoundaries,
    PcmLevelWindow,
)


def measurements() -> tuple[PcmLevelWindow, ...]:
    return (
        PcmLevelWindow(0.0, 0.1, 0.0, -120.0, 0.0),
        PcmLevelWindow(2.0, 0.1, 0.25, -12.0, 0.8),
        PcmLevelWindow(117.0, 0.1, 0.1, -20.0, 0.4),
    )


def test_result_summarizes_levels_version_confidence_and_timestamp() -> None:
    timestamp = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)

    result = CueAnalysisResult.from_measurements(
        Path("song.mp3"),
        120.0,
        DetectedCueBoundaries(2.0, 118.0, 7.0),
        measurements(),
        confidence=0.85,
        analysis_version="silence-v1",
        backend_name="ffmpeg",
        analyzed_at=timestamp,
    )

    assert (result.minimum_level_dbfs, result.maximum_level_dbfs) == (-120.0, -12.0)
    assert result.peak == 0.8
    assert result.measured_window_count == 3
    assert result.confidence == 0.85
    assert result.analysis_version == "silence-v1"
    assert result.analyzed_at == timestamp
    with pytest.raises(FrozenInstanceError):
        result.confidence = 0.2  # type: ignore[misc]


def test_default_analysis_timestamp_is_timezone_aware() -> None:
    result = CueAnalysisResult.from_measurements(
        Path("song.flac"),
        120.0,
        DetectedCueBoundaries(2.0, 118.0, 7.0),
        measurements(),
        confidence=0.5,
        analysis_version="silence-v1",
        backend_name="ffmpeg",
    )

    assert result.analyzed_at.tzinfo is not None
    assert result.analyzed_at.utcoffset() is not None


@pytest.mark.parametrize("confidence", [-0.1, 1.1, float("nan")])
def test_result_rejects_invalid_confidence(confidence: float) -> None:
    with pytest.raises(ValueError):
        CueAnalysisResult.from_measurements(
            Path("song.mp3"),
            120.0,
            DetectedCueBoundaries(2.0, 118.0, 7.0),
            measurements(),
            confidence=confidence,
            analysis_version="silence-v1",
            backend_name="ffmpeg",
        )


def test_result_rejects_empty_measurements_and_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="Pegelfenster"):
        CueAnalysisResult.from_measurements(
            Path("song.mp3"),
            120.0,
            DetectedCueBoundaries(2.0, 118.0, 7.0),
            (),
            confidence=0.5,
            analysis_version="silence-v1",
            backend_name="ffmpeg",
        )
    with pytest.raises(ValueError, match="Zeitzone"):
        CueAnalysisResult.from_measurements(
            Path("song.mp3"),
            120.0,
            DetectedCueBoundaries(2.0, 118.0, 7.0),
            measurements(),
            confidence=0.5,
            analysis_version="silence-v1",
            backend_name="ffmpeg",
            analyzed_at=datetime(2026, 7, 25, 12, 0),
        )
