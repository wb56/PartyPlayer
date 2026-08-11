"""Streaming PCM window level measurements."""

import math

import pytest

from party_player.analysis import PcmChunk, PcmLevelAnalyzer


def test_constant_pcm_has_expected_rms_dbfs_and_peak() -> None:
    analyzer = PcmLevelAnalyzer(window_seconds=0.1)
    chunk = PcmChunk(0.0, 100, 2, (0.5, -0.5) * 10)

    windows = analyzer.consume(chunk)

    assert len(windows) == 1
    assert windows[0].duration_seconds == 0.1
    assert windows[0].rms == 0.5
    assert windows[0].level_dbfs == pytest.approx(20 * math.log10(0.5))
    assert windows[0].peak == 0.5


def test_silence_uses_configured_finite_dbfs_floor() -> None:
    analyzer = PcmLevelAnalyzer(window_seconds=0.1, floor_dbfs=-100.0)

    windows = analyzer.consume(PcmChunk(2.0, 100, 1, (0.0,) * 10))

    assert windows[0].rms == 0.0
    assert windows[0].level_dbfs == -100.0


def test_window_continues_across_contiguous_pcm_chunks() -> None:
    analyzer = PcmLevelAnalyzer(window_seconds=0.1)

    assert analyzer.consume(PcmChunk(0.0, 100, 1, (0.25,) * 4)) == ()
    windows = analyzer.consume(PcmChunk(0.04, 100, 1, (0.25,) * 6))

    assert len(windows) == 1
    assert windows[0].start_seconds == 0.0
    assert windows[0].rms == 0.25


def test_start_and_end_segments_never_share_a_level_window() -> None:
    analyzer = PcmLevelAnalyzer(window_seconds=0.1)
    assert analyzer.consume(PcmChunk(0.0, 100, 1, (0.1,) * 5)) == ()

    windows = analyzer.consume(PcmChunk(255.0, 100, 1, (0.8,) * 10))

    assert len(windows) == 2
    assert windows[0].start_seconds == 0.0
    assert windows[0].duration_seconds == 0.05
    assert windows[0].rms == pytest.approx(0.1)
    assert windows[1].start_seconds == 255.0
    assert windows[1].rms == pytest.approx(0.8)


def test_finish_flushes_partial_window_and_rejects_nonfinite_pcm() -> None:
    analyzer = PcmLevelAnalyzer(window_seconds=0.1)
    analyzer.consume(PcmChunk(0.0, 100, 1, (0.2,) * 3))
    assert analyzer.finish()[0].duration_seconds == 0.03

    with pytest.raises(ValueError, match="endlichen Samplewert"):
        PcmLevelAnalyzer().consume(PcmChunk(0.0, 100, 1, (float("nan"),)))
