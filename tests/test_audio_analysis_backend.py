"""Contract tests for interchangeable offline PCM analysis backends."""

from collections.abc import Iterable, Sequence
from pathlib import Path
from threading import Event

import pytest

from party_player.analysis import (
    AnalysisSegment,
    AudioAnalysisBackend,
    AudioFileInfo,
    CancellationToken,
    PcmChunk,
    plan_edge_segments,
)


class FakeAnalysisBackend:
    @property
    def name(self) -> str:
        return "fake"

    def is_available(self) -> bool:
        return True

    def supported_extensions(self) -> frozenset[str]:
        return frozenset({".mp3", ".flac"})

    def probe(self, file_path: Path) -> AudioFileInfo:
        assert file_path.suffix in self.supported_extensions()
        return AudioFileInfo(120.0, 48_000, 2, "fake-pcm")

    def decode_segments(
        self,
        file_path: Path,
        segments: Sequence[AnalysisSegment],
        cancellation: CancellationToken,
    ) -> Iterable[PcmChunk]:
        del file_path
        for segment in segments:
            if cancellation.is_set():
                return
            yield PcmChunk(segment.start_seconds, 48_000, 2, (0.25, -0.25, 0.5, -0.5))


def test_structural_backend_contract_supports_probe_and_bounded_pcm_chunks() -> None:
    backend = FakeAnalysisBackend()
    contract: AudioAnalysisBackend = backend
    segments = (AnalysisSegment(0.0, 30.0), AnalysisSegment(90.0, 30.0))

    assert isinstance(backend, AudioAnalysisBackend)
    assert contract.probe(Path("party.mp3")) == AudioFileInfo(120.0, 48_000, 2, "fake-pcm")
    chunks = list(contract.decode_segments(Path("party.mp3"), segments, Event()))
    assert [chunk.start_seconds for chunk in chunks] == [0.0, 90.0]
    assert all(chunk.frame_count == 2 for chunk in chunks)


def test_backend_cancellation_stops_before_another_pcm_chunk() -> None:
    backend: AudioAnalysisBackend = FakeAnalysisBackend()
    cancellation = Event()
    cancellation.set()

    chunks = backend.decode_segments(
        Path("party.flac"),
        (AnalysisSegment(0.0, 30.0),),
        cancellation,
    )

    assert list(chunks) == []


def test_edge_segment_planner_bounds_long_files_and_merges_short_overlap() -> None:
    assert plan_edge_segments(300.0) == (
        AnalysisSegment(0.0, 45.0),
        AnalysisSegment(255.0, 45.0),
    )
    assert plan_edge_segments(70.0) == (AnalysisSegment(0.0, 70.0),)
    assert plan_edge_segments(150.0, 60.0) == (
        AnalysisSegment(0.0, 60.0),
        AnalysisSegment(90.0, 60.0),
    )


@pytest.mark.parametrize("window", [0.0, 61.0, float("nan")])
def test_edge_segment_planner_rejects_unbounded_windows(window: float) -> None:
    with pytest.raises(ValueError, match="zwischen 1 und 60"):
        plan_edge_segments(300.0, window)
