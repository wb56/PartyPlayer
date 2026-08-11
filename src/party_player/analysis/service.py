"""Application service connecting PCM analysis to cue persistence."""

from concurrent.futures import CancelledError, Future
import logging
from pathlib import Path

from party_player.analysis.background import (
    AudioAnalysisJob,
    AudioAnalysisRunSummary,
    BackgroundAudioAnalysisRunner,
)
from party_player.analysis.base import AudioAnalysisBackend, PcmChunk
from party_player.analysis.cue_estimation import CueBoundaryEstimator
from party_player.analysis.levels import PcmLevelAnalyzer, PcmLevelWindow
from party_player.analysis.result import CueAnalysisResult
from party_player.analysis.signal_detection import (
    SignalDetectionSettings,
    SignalRegion,
    StreamingSignalDetector,
)
from party_player.cue_points import CuePointService
from party_player.models import Track


class CueAnalysisServiceJob:
    """Public job handle exposing the final persisted analysis result."""

    def __init__(
        self,
        worker_job: AudioAnalysisJob,
        future: Future[CueAnalysisResult],
    ) -> None:
        self.job_id = worker_job.job_id
        self.future = future
        self._worker_job = worker_job

    def cancel(self) -> bool:
        return self._worker_job.cancel()

    @property
    def cancellation_requested(self) -> bool:
        return self._worker_job.cancellation_requested


class CueAnalysisService:
    """Run the complete automatic-cue pipeline outside the caller thread."""

    def __init__(
        self,
        backend: AudioAnalysisBackend,
        cue_points: CuePointService,
        *,
        runner: BackgroundAudioAnalysisRunner | None = None,
        signal_settings: SignalDetectionSettings | None = None,
        estimator: CueBoundaryEstimator | None = None,
        level_window_seconds: float = 0.1,
        analysis_version: str = "silence-v1",
    ) -> None:
        if not analysis_version.strip():
            raise ValueError("Analyseversion darf nicht leer sein")
        self._backend = backend
        self._cue_points = cue_points
        self._runner = runner or BackgroundAudioAnalysisRunner(backend)
        self._owns_runner = runner is None
        self._signal_settings = signal_settings or SignalDetectionSettings()
        self._estimator = estimator or CueBoundaryEstimator()
        self._level_window_seconds = level_window_seconds
        self._analysis_version = analysis_version.strip()
        self._logger = logging.getLogger(__name__)

    def analyze(self, track: Track) -> CueAnalysisServiceJob:
        """Queue one complete analysis and return before file access begins."""
        if not track.file_path.strip():
            raise ValueError("Titel besitzt keinen analysierbaren Dateipfad")
        if track.duration_seconds is None or track.duration_seconds <= 0:
            raise ValueError("Titel besitzt keine gültige Dauer für die Cue-Analyse")
        track_duration = float(track.duration_seconds)
        analyzer = PcmLevelAnalyzer(window_seconds=self._level_window_seconds)
        detector = StreamingSignalDetector(self._signal_settings)
        levels: list[PcmLevelWindow] = []
        regions: list[SignalRegion] = []

        def consume_chunk(chunk: PcmChunk) -> None:
            for measured in analyzer.consume(chunk):
                levels.append(measured)
                regions.extend(detector.consume(measured))

        worker_job = self._runner.submit_edges(
            Path(track.file_path),
            consume_chunk,
            edge_window_seconds=self._estimator.settings.edge_window_seconds,
        )
        result_future: Future[CueAnalysisResult] = Future()

        def finalize(worker_future: Future[AudioAnalysisRunSummary]) -> None:
            try:
                summary = worker_future.result()
                if summary.cancelled:
                    raise CancelledError()
                for measured in analyzer.finish():
                    levels.append(measured)
                    regions.extend(detector.consume(measured))
                regions.extend(detector.finish())
                boundaries = self._estimator.estimate(
                    track_duration,
                    tuple(regions),
                )
                result = CueAnalysisResult.from_measurements(
                    Path(track.file_path),
                    track_duration,
                    boundaries,
                    levels,
                    confidence=self._confidence(track_duration, tuple(regions), levels),
                    analysis_version=self._analysis_version,
                    backend_name=self._backend.name,
                )
                self._cue_points.save_automatic(track, result)
                result_future.set_result(result)
            except BaseException as exc:
                if isinstance(exc, CancelledError):
                    result_future.cancel()
                else:
                    self._logger.error(
                        "Cue-Analyse für Titel %s (%s) fehlgeschlagen: %s",
                        track.id,
                        track.file_path,
                        exc,
                    )
                    result_future.set_exception(exc)

        worker_job.future.add_done_callback(finalize)
        return CueAnalysisServiceJob(worker_job, result_future)

    def is_available(self) -> bool:
        return self._backend.is_available()

    @property
    def analysis_version(self) -> str:
        return self._analysis_version

    def needs_analysis(self, track_id: int) -> bool:
        stored = self._cue_points.get(track_id)
        return (
            stored.automatic_cue_in is None
            or stored.automatic_cue_out is None
            or stored.automatic_fade_duration is None
            or stored.analysis_version != self._analysis_version
        )

    @property
    def active_job_count(self) -> int:
        return self._runner.active_job_count

    def close(self, wait: bool = True) -> None:
        if self._owns_runner:
            self._runner.close(wait=wait)

    def _confidence(
        self,
        duration: float,
        regions: tuple[SignalRegion, ...],
        levels: list[PcmLevelWindow],
    ) -> float:
        edge = self._estimator.settings.edge_window_seconds
        head_confirmed = any(region.start_seconds < min(duration, edge) for region in regions)
        tail_confirmed = any(region.end_seconds > max(0.0, duration - edge) for region in regions)
        dynamic_range = max(level.level_dbfs for level in levels) - min(
            level.level_dbfs for level in levels
        )
        return min(
            1.0,
            (0.4 if head_confirmed else 0.0)
            + (0.4 if tail_confirmed else 0.0)
            + min(0.2, max(0.0, dynamic_range) / 200.0),
        )
