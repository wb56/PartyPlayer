"""Bounded background orchestration for complete-file loudness analysis."""

from concurrent.futures import Executor, Future
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock
from typing import cast
from uuid import uuid4

from party_player.analysis.loudness_backend import (
    LoudnessAnalysisBackend,
    LoudnessAnalysisResult,
)
from party_player.bounded_executor import BoundedThreadPoolExecutor
from party_player.loudness import LoudnessRepository
from party_player.models import Track
from party_player.persistence_participant import single_worker_participant
from party_player.restore_lifecycle import PersistenceParticipant


@dataclass(frozen=True, slots=True)
class LoudnessAnalysisJob:
    """One cancellable background analysis."""

    job_id: str
    future: Future[LoudnessAnalysisResult]
    _cancellation: Event

    def cancel(self) -> bool:
        self._cancellation.set()
        return self.future.cancel()

    @property
    def cancellation_requested(self) -> bool:
        return self._cancellation.is_set()


class OfflineLoudnessAnalysisService:
    """Measure and persist loudness without blocking callers or editing files."""

    def __init__(
        self,
        backend: LoudnessAnalysisBackend,
        repository: LoudnessRepository,
        *,
        analysis_version: str = "ebur128-v1",
        executor: Executor | None = None,
        maximum_pending: int = 8,
    ) -> None:
        if not analysis_version.strip():
            raise ValueError("Analyseversion darf nicht leer sein")
        self._backend = backend
        self._repository = repository
        self._analysis_version = analysis_version.strip()
        self._executor = executor or BoundedThreadPoolExecutor(
            max_workers=1,
            maximum_pending=maximum_pending,
            thread_name_prefix="loudness-analysis",
        )
        self._owns_executor = executor is None
        self._jobs: dict[str, Event] = {}
        self._lock = Lock()
        self._closed = False

    def analyze(self, track: Track) -> LoudnessAnalysisJob:
        """Queue complete-file analysis and return before backend work starts."""
        if not track.file_path.strip():
            raise ValueError("Titel besitzt keinen analysierbaren Dateipfad")
        with self._lock:
            if self._closed:
                raise RuntimeError("Lautheitsanalyse wurde bereits beendet")
            job_id = uuid4().hex
            cancellation = Event()
            self._jobs[job_id] = cancellation

        def run() -> LoudnessAnalysisResult:
            try:
                if cancellation.is_set():
                    raise RuntimeError("Lautheitsanalyse wurde abgebrochen")
                result = self._backend.analyze(Path(track.file_path))
                if cancellation.is_set():
                    raise RuntimeError("Lautheitsanalyse wurde abgebrochen")
                self._repository.save_analysis(
                    track.id,
                    integrated_loudness_lufs=result.integrated_loudness_lufs,
                    loudness_range_lu=result.loudness_range_lu,
                    true_peak_dbfs=result.true_peak_dbfs,
                    source="EBU_R128",
                    version=self._analysis_version,
                    method=result.method,
                )
                return result
            except Exception as exc:
                if not cancellation.is_set():
                    self._repository.mark_analysis_failed(track.id, str(exc))
                raise

        try:
            future = cast(Future[LoudnessAnalysisResult], self._executor.submit(run))
        except Exception:
            with self._lock:
                self._jobs.pop(job_id, None)
            raise

        def forget(_future: Future[LoudnessAnalysisResult]) -> None:
            with self._lock:
                self._jobs.pop(job_id, None)

        future.add_done_callback(forget)
        return LoudnessAnalysisJob(job_id, future, cancellation)

    def needs_analysis(self, track_id: int) -> bool:
        stored = self._repository.get(track_id)
        return (
            stored.analysis_status != "COMPLETE"
            or stored.analysis_version != self._analysis_version
        )

    @property
    def active_job_count(self) -> int:
        with self._lock:
            return len(self._jobs)

    def restore_participant(self) -> PersistenceParticipant | None:
        if not isinstance(self._executor, BoundedThreadPoolExecutor):
            return None
        return single_worker_participant("loudness-analysis", self._executor)

    def close(self, wait: bool = True) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            cancellations = tuple(self._jobs.values())
        for cancellation in cancellations:
            cancellation.set()
        if self._owns_executor:
            self._executor.shutdown(wait=wait, cancel_futures=True)
