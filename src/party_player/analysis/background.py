"""Bounded background execution for offline PCM analysis."""

from collections.abc import Callable, Sequence
from concurrent.futures import Executor, Future
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock
from typing import cast
from uuid import uuid4

from party_player.analysis.base import (
    AnalysisSegment,
    AudioAnalysisBackend,
    PcmChunk,
    plan_edge_segments,
)
from party_player.bounded_executor import BoundedThreadPoolExecutor


@dataclass(frozen=True, slots=True)
class AudioAnalysisRunSummary:
    """Small completion value that never retains decoded PCM."""

    chunk_count: int
    frame_count: int
    cancelled: bool


class AudioAnalysisJob:
    """One submitted job with cooperative and queued-task cancellation."""

    def __init__(
        self,
        job_id: str,
        future: Future[AudioAnalysisRunSummary],
        cancellation: Event,
    ) -> None:
        self.job_id = job_id
        self.future = future
        self._cancellation = cancellation

    def cancel(self) -> bool:
        self._cancellation.set()
        return self.future.cancel()

    @property
    def cancellation_requested(self) -> bool:
        return self._cancellation.is_set()


class BackgroundAudioAnalysisRunner:
    """Submit every probe/decode operation to a dedicated bounded worker pool."""

    def __init__(
        self,
        backend: AudioAnalysisBackend,
        *,
        executor: Executor | None = None,
        maximum_pending: int = 8,
    ) -> None:
        self._backend = backend
        self._executor = executor or BoundedThreadPoolExecutor(
            max_workers=1,
            maximum_pending=maximum_pending,
            thread_name_prefix="audio-analysis",
        )
        self._owns_executor = executor is None
        self._jobs: dict[str, Event] = {}
        self._lock = Lock()
        self._closed = False

    def submit(
        self,
        file_path: Path,
        segments: Sequence[AnalysisSegment],
        consume_chunk: Callable[[PcmChunk], None],
    ) -> AudioAnalysisJob:
        """Return immediately after queueing all backend work."""
        return self._submit_with_segment_provider(
            file_path,
            lambda: tuple(segments),
            consume_chunk,
        )

    def submit_edges(
        self,
        file_path: Path,
        consume_chunk: Callable[[PcmChunk], None],
        *,
        edge_window_seconds: float = 45.0,
    ) -> AudioAnalysisJob:
        """Probe and decode only bounded start/end windows in the worker."""
        return self._submit_with_segment_provider(
            file_path,
            lambda: plan_edge_segments(
                self._backend.probe(file_path).duration_seconds,
                edge_window_seconds,
            ),
            consume_chunk,
        )

    def _submit_with_segment_provider(
        self,
        file_path: Path,
        segment_provider: Callable[[], Sequence[AnalysisSegment]],
        consume_chunk: Callable[[PcmChunk], None],
    ) -> AudioAnalysisJob:
        with self._lock:
            if self._closed:
                raise RuntimeError("Audioanalyse wurde bereits beendet")
            job_id = uuid4().hex
            cancellation = Event()
            self._jobs[job_id] = cancellation

        def run() -> AudioAnalysisRunSummary:
            chunk_count = 0
            frame_count = 0
            segments = segment_provider()
            for chunk in self._backend.decode_segments(file_path, segments, cancellation):
                if cancellation.is_set():
                    break
                consume_chunk(chunk)
                chunk_count += 1
                frame_count += chunk.frame_count
            return AudioAnalysisRunSummary(
                chunk_count,
                frame_count,
                cancellation.is_set(),
            )

        try:
            future = cast(Future[AudioAnalysisRunSummary], self._executor.submit(run))
        except Exception:
            with self._lock:
                self._jobs.pop(job_id, None)
            raise

        def forget(_future: Future[AudioAnalysisRunSummary]) -> None:
            with self._lock:
                self._jobs.pop(job_id, None)

        future.add_done_callback(forget)
        return AudioAnalysisJob(job_id, future, cancellation)

    def close(self, wait: bool = True) -> None:
        """Cancel active work and close the owned executor."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            cancellations = tuple(self._jobs.values())
        for cancellation in cancellations:
            cancellation.set()
        if self._owns_executor:
            self._executor.shutdown(wait=wait, cancel_futures=True)

    @property
    def active_job_count(self) -> int:
        with self._lock:
            return len(self._jobs)
