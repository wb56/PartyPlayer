"""Programmatic single and serial batch operations for productive metadata analysis."""

from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from time import monotonic, sleep

from party_player.database.connection import Database
from party_player.metadata_analysis_contracts import (
    FileSnapshot,
    MetadataAnalysisBackendKind,
    MetadataAnalysisJob,
    MetadataAnalysisOutcome,
    MetadataAnalysisRequest,
    MetadataAnalysisResult,
)
from party_player.metadata_analysis_coordinator import (
    AnalysisOperatingState,
    MetadataAnalysisCoordinator,
)
from party_player.metadata_analysis_persistence import (
    SqliteAnalysisResultPersistencePort,
    SqliteAnalysisRunPersistencePort,
)
from party_player.metadata_analysis_profiles import (
    ALGORITHM_VERSION,
    HIGH_CONFIDENCE,
    MINIMUM_SUGGESTION_CONFIDENCE,
    PROFILE_CONFIGURATIONS,
    TEMPO_CHANGE_STABILITY,
    MetadataAnalysisProfile,
)
from party_player.metadata_analysis_supervisor import MetadataAnalysisProcessSupervisor
from party_player.repositories.track_repository import TrackRepository
from party_player.restore_lifecycle import PersistenceParticipant
from party_player.worker_diagnostics import WorkerRegistry


@dataclass(slots=True)
class MetadataAnalysisDiagnostics:
    started_runs: int = 0
    completed_runs: int = 0
    successful_runs: int = 0
    failed_runs: int = 0
    runs_without_bpm: int = 0
    high_confidence: int = 0
    medium_confidence: int = 0
    low_confidence: int = 0
    half_double_warnings: int = 0
    tempo_changes: int = 0
    snapshot_conflicts: int = 0
    timeouts: int = 0
    cancellations: int = 0
    worker_crashes: int = 0
    total_duration_seconds: float = 0.0
    maximum_duration_seconds: float = 0.0


class _OperatingStatePort:
    def __init__(self, provider: Callable[[], AnalysisOperatingState]) -> None:
        self._provider = provider

    def snapshot(self) -> AnalysisOperatingState:
        return self._provider()


class _ProgressPort:
    def __init__(self, publish: Callable[[str, str, str], None]) -> None:
        self._publish = publish

    def publish(self, event: str, job_id: str, detail: str = "") -> None:
        self._publish(event, job_id, detail[:120])


class MetadataAnalysisService:
    """Own the productive coordinator without starting work implicitly."""

    def __init__(
        self,
        database: Database,
        tracks: TrackRepository,
        *,
        ffmpeg: Path | None,
        ffprobe: Path | None,
        operating_state: Callable[[], AnalysisOperatingState],
        publish_progress: Callable[[str, str, str], None] = lambda *_args: None,
        worker_registry: WorkerRegistry | None = None,
    ) -> None:
        self._database = database
        self._tracks = tracks
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe
        self._runs = SqliteAnalysisRunPersistencePort(database)
        self._supervisor = MetadataAnalysisProcessSupervisor(worker_registry)
        self._coordinator = MetadataAnalysisCoordinator(
            self._supervisor,
            self._runs,
            SqliteAnalysisResultPersistencePort(database),
            _OperatingStatePort(operating_state),
            _ProgressPort(publish_progress),
        )
        self._closed = False
        self._accepting = True
        self._started: dict[str, float] = {}
        self.diagnostics = MetadataAnalysisDiagnostics()
        self.interrupted_on_start = self._coordinator.recover_interrupted_runs()

    @property
    def available(self) -> bool:
        return bool(
            self._ffmpeg is not None
            and self._ffprobe is not None
            and self._ffmpeg.is_file()
            and self._ffprobe.is_file()
        )

    @property
    def active_job_count(self) -> int:
        return int(self._coordinator.current_job is not None)

    def analyze_track(
        self,
        track_id: int,
        profile: MetadataAnalysisProfile = MetadataAnalysisProfile.TEMPO,
        *,
        batch: bool = False,
    ) -> MetadataAnalysisJob:
        if self._closed or not self._accepting:
            raise RuntimeError("Metadatenanalyse ist geschlossen")
        track = self._tracks.get_active(track_id)
        if track is None:
            raise KeyError(f"Katalogtitel {track_id} wurde nicht gefunden")
        path = Path(track.file_path).resolve()
        snapshot = FileSnapshot.capture(str(path))
        configuration = PROFILE_CONFIGURATIONS[profile]
        request = MetadataAnalysisRequest(
            track_id,
            snapshot,
            profile.value,
            ALGORITHM_VERSION,
            configuration.requested_kinds,
            timeout_seconds=configuration.timeout_seconds,
            backend=MetadataAnalysisBackendKind.FFMPEG_TEMPO,
            technical_options=self._technical_options(configuration.segment_strategy),
        )
        job = self._runs.create_job(request)
        if not self.available:
            result = self._unavailable_result(job, "FFmpeg oder FFprobe ist nicht verfügbar.")
            self._runs.finish(result)
            self._record_result(result)
            return job
        self._coordinator.enqueue(job, batch=batch)
        return job

    def analyze_selected(
        self,
        track_ids: Iterable[int],
        profile: MetadataAnalysisProfile = MetadataAnalysisProfile.TEMPO,
    ) -> tuple[MetadataAnalysisJob, ...]:
        return tuple(self.analyze_track(track_id, profile, batch=True) for track_id in track_ids)

    def analyze_without_current_bpm_suggestion(
        self,
        *,
        limit: int = 1000,
        profile: MetadataAnalysisProfile = MetadataAnalysisProfile.TEMPO,
    ) -> tuple[MetadataAnalysisJob, ...]:
        ids = self._candidate_ids(
            """NOT EXISTS (
                   SELECT 1 FROM track_metadata_suggestions s
                   WHERE s.track_id=t.id AND s.field_key='bpm' AND s.status='PENDING'
               )""",
            limit,
        )
        return self.analyze_selected(ids, profile)

    def analyze_outdated_version(
        self,
        *,
        limit: int = 1000,
        profile: MetadataAnalysisProfile = MetadataAnalysisProfile.TEMPO,
    ) -> tuple[MetadataAnalysisJob, ...]:
        ids = self._candidate_ids(
            """EXISTS (
                   SELECT 1 FROM metadata_analysis_runs r
                   WHERE r.track_id=t.id AND r.analysis_profile=?
                     AND r.analysis_version<>?
               ) AND NOT EXISTS (
                   SELECT 1 FROM metadata_analysis_runs current
                   WHERE current.track_id=t.id AND current.analysis_profile=?
                     AND current.analysis_version=? AND current.status='COMPLETED'
               )""",
            limit,
            (profile.value, ALGORITHM_VERSION, profile.value, ALGORITHM_VERSION),
        )
        return self.analyze_selected(ids, profile)

    def tick(self) -> MetadataAnalysisResult | None:
        before = self._coordinator.current_job
        result = self._coordinator.tick()
        current = self._coordinator.current_job
        if before is None and current is not None:
            self._started[current.job_id] = monotonic()
            self.diagnostics.started_runs += 1
        if result is not None:
            self._record_result(result)
        return result

    def pause(self) -> None:
        self._coordinator.pause()

    def resume_persistent_pending(self, *, limit: int = 1000) -> int:
        """Explicitly resume retained PENDING runs; never called automatically at startup."""
        if not self.available or self._ffmpeg is None or self._ffprobe is None:
            return 0
        jobs = self._runs.pending_jobs(self._ffmpeg, self._ffprobe, limit=limit)
        for job in jobs:
            self._coordinator.enqueue(job, batch=True)
        return len(jobs)

    def resume(self) -> None:
        self._coordinator.resume()

    def cancel_current(self) -> MetadataAnalysisResult | None:
        result = self._coordinator.cancel_current()
        if result is not None:
            self._record_result(result)
        return result

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._accepting = False
        self._coordinator.close()

    def restore_participant(self) -> PersistenceParticipant:
        def block() -> bool:
            self._accepting = False
            self.pause()
            return True

        def drain(timeout: float) -> bool:
            deadline = monotonic() + max(0.0, timeout)
            while self._coordinator.current_job is not None and monotonic() < deadline:
                self.tick()
                sleep(0.02)
            return self._coordinator.current_job is None

        def resume() -> bool:
            if self._closed:
                return False
            self._accepting = True
            self.resume()
            return True

        return PersistenceParticipant(
            "metadata-analysis",
            block,
            drain,
            lambda: True,
            resume,
        )

    def support_snapshot(self) -> dict[str, object]:
        """Return bounded diagnostics without file paths or worker PID."""
        values = asdict(self.diagnostics)
        values["average_duration_seconds"] = (
            self.diagnostics.total_duration_seconds / self.diagnostics.completed_runs
            if self.diagnostics.completed_runs
            else 0.0
        )
        with self._database.connect() as connection:
            waiting = connection.execute(
                "SELECT COUNT(*) FROM metadata_analysis_runs WHERE status='PENDING'"
            ).fetchone()[0]
        values["waiting_runs"] = int(waiting)
        values["active_profile"] = (
            self._coordinator.current_job.analysis_profile
            if self._coordinator.current_job is not None
            else ""
        )
        values["backend"] = "ffmpeg-onset-autocorrelation"
        values["algorithm_version"] = ALGORITHM_VERSION
        return values

    def _technical_options(self, strategy: str) -> tuple[tuple[str, str], ...]:
        return (
            ("ffmpeg", str(self._ffmpeg) if self._ffmpeg is not None else "ffmpeg"),
            ("ffprobe", str(self._ffprobe) if self._ffprobe is not None else "ffprobe"),
            ("segment_strategy", strategy),
        )

    def _candidate_ids(
        self, condition: str, limit: int, parameters: tuple[object, ...] = ()
    ) -> tuple[int, ...]:
        bounded = max(1, min(int(limit), 10_000))
        with self._database.connect() as connection:
            rows = connection.execute(
                f"""SELECT t.id FROM tracks t
                    WHERE t.catalog_visible=1 AND {condition}
                    ORDER BY t.id LIMIT ?""",
                (*parameters, bounded),
            ).fetchall()
        return tuple(int(row["id"]) for row in rows)

    def _record_result(self, result: MetadataAnalysisResult) -> None:
        self.diagnostics.completed_runs += 1
        duration = max(0.0, monotonic() - self._started.pop(result.job_id, monotonic()))
        self.diagnostics.total_duration_seconds += duration
        self.diagnostics.maximum_duration_seconds = max(
            self.diagnostics.maximum_duration_seconds, duration
        )
        if result.outcome in {
            MetadataAnalysisOutcome.SUCCESS,
            MetadataAnalysisOutcome.PARTIAL_SUCCESS,
        }:
            self.diagnostics.successful_runs += 1
        else:
            self.diagnostics.failed_runs += 1
        bpm = next((item for item in result.suggestions if item.field_key == "bpm"), None)
        if bpm is None:
            self.diagnostics.runs_without_bpm += 1
            self.diagnostics.low_confidence += 1
        elif bpm.confidence >= HIGH_CONFIDENCE:
            self.diagnostics.high_confidence += 1
        elif bpm.confidence >= MINIMUM_SUGGESTION_CONFIDENCE:
            self.diagnostics.medium_confidence += 1
        else:
            self.diagnostics.low_confidence += 1
        if result.rhythm_stability < TEMPO_CHANGE_STABILITY:
            self.diagnostics.tempo_changes += 1
        self.diagnostics.half_double_warnings += sum(
            "Halb-/Doppeltempo" in warning for warning in result.warnings
        )
        self.diagnostics.timeouts += int(result.outcome is MetadataAnalysisOutcome.TIMEOUT)
        self.diagnostics.cancellations += int(result.outcome is MetadataAnalysisOutcome.CANCELLED)
        self.diagnostics.worker_crashes += int(
            result.outcome is MetadataAnalysisOutcome.WORKER_CRASHED
        )
        self.diagnostics.snapshot_conflicts = self._coordinator.snapshot_conflicts

    @staticmethod
    def _unavailable_result(job: MetadataAnalysisJob, text: str) -> MetadataAnalysisResult:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        return MetadataAnalysisResult(
            job.job_id,
            job.run_id,
            job.track_id,
            job.input_snapshot,
            job.analysis_profile,
            job.analysis_version,
            now,
            now,
            MetadataAnalysisOutcome.BACKEND_UNAVAILABLE,
            error_code="BACKEND_UNAVAILABLE",
            error_text=text,
            backend_name="ffmpeg-onset-autocorrelation",
            backend_version=ALGORITHM_VERSION,
        )
