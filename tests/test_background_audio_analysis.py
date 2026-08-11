"""Thread-boundary and cancellation tests for offline audio analysis."""

from collections.abc import Iterable, Sequence
from pathlib import Path
from threading import Event, get_ident

import pytest

from party_player.analysis import (
    AnalysisSegment,
    AudioFileInfo,
    BackgroundAudioAnalysisRunner,
    CancellationToken,
    PcmChunk,
)


class ThreadRecordingBackend:
    name = "thread-recording"

    def __init__(self) -> None:
        self.thread_ids: list[int] = []
        self.probe_thread_ids: list[int] = []
        self.segments: tuple[AnalysisSegment, ...] = ()
        self.started = Event()
        self.release = Event()

    def is_available(self) -> bool:
        return True

    def supported_extensions(self) -> frozenset[str]:
        return frozenset({".wav"})

    def probe(self, file_path: Path) -> AudioFileInfo:
        del file_path
        self.probe_thread_ids.append(get_ident())
        return AudioFileInfo(300.0, 100, 1)

    def decode_segments(
        self,
        file_path: Path,
        segments: Sequence[AnalysisSegment],
        cancellation: CancellationToken,
    ) -> Iterable[PcmChunk]:
        del file_path
        self.segments = tuple(segments)
        self.thread_ids.append(get_ident())
        self.started.set()
        self.release.wait(timeout=2)
        if not cancellation.is_set():
            yield PcmChunk(0.0, 100, 1, (0.1, 0.2))


def test_analysis_and_pcm_consumer_run_outside_calling_thread() -> None:
    backend = ThreadRecordingBackend()
    runner = BackgroundAudioAnalysisRunner(backend)
    caller_thread = get_ident()
    consumer_threads: list[int] = []

    job = runner.submit(
        Path("song.wav"),
        (AnalysisSegment(0.0, 5.0),),
        lambda _chunk: consumer_threads.append(get_ident()),
    )
    assert backend.started.wait(timeout=1)
    assert not job.future.done()
    backend.release.set()

    summary = job.future.result(timeout=2)
    runner.close()

    assert backend.thread_ids == consumer_threads
    assert backend.thread_ids[0] != caller_thread
    assert (summary.chunk_count, summary.frame_count, summary.cancelled) == (1, 2, False)


def test_running_analysis_job_can_be_cancelled_cooperatively() -> None:
    backend = ThreadRecordingBackend()
    runner = BackgroundAudioAnalysisRunner(backend)
    job = runner.submit(
        Path("song.wav"),
        (AnalysisSegment(0.0, 5.0),),
        lambda _chunk: pytest.fail("Nach Abbruch darf kein PCM-Block verarbeitet werden"),
    )
    assert backend.started.wait(timeout=1)

    job.cancel()
    backend.release.set()
    summary = job.future.result(timeout=2)
    runner.close()

    assert job.cancellation_requested
    assert summary.cancelled
    assert summary.chunk_count == 0


def test_closed_runner_rejects_new_jobs() -> None:
    runner = BackgroundAudioAnalysisRunner(ThreadRecordingBackend())
    runner.close()

    with pytest.raises(RuntimeError, match="beendet"):
        runner.submit(Path("song.wav"), (), lambda _chunk: None)


def test_edge_probe_and_bounded_segment_planning_stay_in_worker() -> None:
    backend = ThreadRecordingBackend()
    runner = BackgroundAudioAnalysisRunner(backend)
    caller_thread = get_ident()

    job = runner.submit_edges(Path("song.wav"), lambda _chunk: None)
    assert backend.started.wait(timeout=1)
    backend.release.set()
    job.future.result(timeout=2)
    runner.close()

    assert backend.probe_thread_ids[0] != caller_thread
    assert backend.segments == (
        AnalysisSegment(0.0, 45.0),
        AnalysisSegment(255.0, 45.0),
    )
