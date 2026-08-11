from pathlib import Path
from threading import Event

import pytest

from party_player.analysis.loudness_backend import LoudnessAnalysisResult
from party_player.analysis.loudness_service import OfflineLoudnessAnalysisService
from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.loudness import LoudnessRepository
from party_player.repositories.track_repository import TrackRepository


class ControlledBackend:
    name = "controlled-ebur128"

    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()
        self.error: Exception | None = None

    def is_available(self) -> bool:
        return True

    def analyze(self, _file_path: Path) -> LoudnessAnalysisResult:
        self.started.set()
        self.release.wait(2.0)
        if self.error is not None:
            raise self.error
        return LoudnessAnalysisResult(
            -14.0,
            5.0,
            -1.0,
            "EBU R128 / ITU-R BS.1770",
            self.name,
        )


def test_analysis_runs_in_background_and_persists_lifecycle_metadata(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "analysis.db")
    migrate(database)
    repository = LoudnessRepository(database)
    backend = ControlledBackend()
    service = OfflineLoudnessAnalysisService(backend, repository)
    track = TrackRepository(database).upsert_file(
        str(tmp_path / "track.flac"), "Track", "", "", 100.0
    )

    job = service.analyze(track)

    assert backend.started.wait(1.0)
    assert not job.future.done()
    assert repository.get(track.id).analysis_status == "NOT_ANALYSED"
    backend.release.set()
    assert job.future.result(timeout=2.0).integrated_loudness_lufs == -14.0

    stored = repository.get(track.id)
    assert stored.integrated_loudness_lufs == -14.0
    assert stored.loudness_range_lu == 5.0
    assert stored.true_peak_dbfs == -1.0
    assert stored.analysis_source == "EBU_R128"
    assert stored.analysis_version == "ebur128-v1"
    assert stored.analysis_method == "EBU R128 / ITU-R BS.1770"
    assert stored.analysis_status == "COMPLETE"
    assert stored.analysis_error is None
    assert stored.analysed_at is not None
    assert not service.needs_analysis(track.id)
    service.close()
    newer = OfflineLoudnessAnalysisService(
        backend,
        repository,
        analysis_version="ebur128-v2",
    )
    assert newer.needs_analysis(track.id)
    newer.close()


def test_analysis_failure_is_persisted_without_measurements(tmp_path: Path) -> None:
    database = Database(tmp_path / "failure.db")
    migrate(database)
    repository = LoudnessRepository(database)
    backend = ControlledBackend()
    backend.error = RuntimeError("decoder failed")
    backend.release.set()
    service = OfflineLoudnessAnalysisService(backend, repository)
    track = TrackRepository(database).upsert_file(
        str(tmp_path / "broken.mp3"), "Broken", "", "", 100.0
    )

    job = service.analyze(track)
    with pytest.raises(RuntimeError, match="decoder failed"):
        job.future.result(timeout=2.0)

    stored = repository.get(track.id)
    assert stored.analysis_status == "FAILED"
    assert stored.analysis_error == "decoder failed"
    assert stored.integrated_loudness_lufs is None
    assert service.needs_analysis(track.id)
    service.close()
