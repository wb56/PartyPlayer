from pathlib import Path
import logging

from party_player.dependency_locator import (
    DependencyCandidate,
    DependencyCandidateSource,
)
from party_player.dependency_validator import FfmpegValidationResult
from party_player.system_dependencies import (
    DependencyInfo,
    DependencyStatus,
    VersionStatus,
    VlcDependencyInfo,
)
from party_player.system_dependency_service import SystemDependencyService
from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.repository import PartyPlayerRepository
from party_player.settings_service import SettingsService
from party_player.performance_monitor import PerformanceMonitor


def dep(name: str, status: DependencyStatus, source: str) -> DependencyInfo:
    return DependencyInfo(
        name,
        status,
        source=source,
        version="8.0" if status == DependencyStatus.AVAILABLE else None,
        version_status=(
            VersionStatus.SUPPORTED
            if status == DependencyStatus.AVAILABLE
            else VersionStatus.UNKNOWN
        ),
    )


def vlc(status: DependencyStatus, source: str) -> VlcDependencyInfo:
    return VlcDependencyInfo(
        status,
        source=source,
        version="3.0.21" if status == DependencyStatus.AVAILABLE else None,
        version_status=(
            VersionStatus.SUPPORTED
            if status == DependencyStatus.AVAILABLE
            else VersionStatus.UNKNOWN
        ),
        libvlc_loaded=status == DependencyStatus.AVAILABLE,
    )


def ffmpeg(directory: Path, status: DependencyStatus, source: str) -> FfmpegValidationResult:
    return FfmpegValidationResult(
        directory,
        dep("FFmpeg", status, source),
        dep("FFprobe", status, source),
    )


class StubLocator:
    def __init__(
        self,
        vlc_candidates: tuple[DependencyCandidate, ...],
        ffmpeg_candidates: tuple[DependencyCandidate, ...],
    ) -> None:
        self.vlc_candidates = vlc_candidates
        self.ffmpeg_candidates = ffmpeg_candidates

    def locate_vlc(self, _user=None, *, detected_directories=()):
        del detected_directories
        return self.vlc_candidates

    def locate_ffmpeg(self, _user=None, *, detected_directories=()):
        del detected_directories
        return self.ffmpeg_candidates


class StubValidator:
    def __init__(self, vlc_results, ffmpeg_results) -> None:
        self.vlc_results = vlc_results
        self.ffmpeg_results = ffmpeg_results

    def validate_vlc(self, candidate):
        return self.vlc_results[candidate.installation_directory]

    def validate_ffmpeg(self, candidate):
        return self.ffmpeg_results[candidate.installation_directory]


def candidate(path: Path, source: DependencyCandidateSource) -> DependencyCandidate:
    return DependencyCandidate("tool", path, source, 0, (), source.name == "USER")


def test_invalid_user_override_remains_visible_while_auto_fallback_is_selected(
    tmp_path: Path,
) -> None:
    user_vlc, auto_vlc = tmp_path / "user-vlc", tmp_path / "auto-vlc"
    user_ff, auto_ff = tmp_path / "user-ff", tmp_path / "auto-ff"
    locator = StubLocator(
        (
            candidate(user_vlc, DependencyCandidateSource.USER),
            candidate(auto_vlc, DependencyCandidateSource.STANDARD),
        ),
        (
            candidate(user_ff, DependencyCandidateSource.USER),
            candidate(auto_ff, DependencyCandidateSource.PATH),
        ),
    )
    validator = StubValidator(
        {
            user_vlc: vlc(DependencyStatus.INVALID, "user"),
            auto_vlc: vlc(DependencyStatus.AVAILABLE, "standard"),
        },
        {
            user_ff: ffmpeg(user_ff, DependencyStatus.NOT_FOUND, "user"),
            auto_ff: ffmpeg(auto_ff, DependencyStatus.AVAILABLE, "path"),
        },
    )

    result = SystemDependencyService(  # type: ignore[arg-type]
        locator, validator, application_version="1.0.0"
    ).check(vlc_user_directory=user_vlc, ffmpeg_user_bin_directory=user_ff)

    assert result.vlc.effective.source == "standard"
    assert result.vlc.attempts[0].source == "user"
    assert result.vlc.automatic_fallback_used
    assert not result.vlc.user_override_valid
    assert result.ffmpeg.effective.installation_directory == auto_ff
    assert result.ffmpeg.attempts[0].installation_directory == user_ff
    assert result.ffmpeg.automatic_fallback_used
    assert result.snapshot.capabilities.playback_available
    assert result.snapshot.capabilities.cue_analysis_available
    assert result.snapshot.application_version == "1.0.0"


def test_valid_user_paths_win_without_testing_later_results(tmp_path: Path) -> None:
    user = tmp_path / "user"
    later = tmp_path / "later"
    locator = StubLocator(
        (
            candidate(user, DependencyCandidateSource.USER),
            candidate(later, DependencyCandidateSource.STANDARD),
        ),
        (candidate(user, DependencyCandidateSource.USER),),
    )
    validator = StubValidator(
        {
            user: vlc(DependencyStatus.AVAILABLE, "user"),
            later: vlc(DependencyStatus.AVAILABLE, "standard"),
        },
        {user: ffmpeg(user, DependencyStatus.AVAILABLE, "user")},
    )

    result = SystemDependencyService(locator, validator).check(  # type: ignore[arg-type]
        vlc_user_directory=user,
        ffmpeg_user_bin_directory=user,
    )

    assert result.vlc.effective.source == "user"
    assert result.vlc.user_override_valid
    assert not result.vlc.automatic_fallback_used
    assert result.ffmpeg.user_override_valid


def test_missing_vlc_blocks_playback_but_not_available_analysis(tmp_path: Path) -> None:
    directory = tmp_path / "tools"
    locator = StubLocator(
        (candidate(directory, DependencyCandidateSource.STANDARD),),
        (candidate(directory, DependencyCandidateSource.STANDARD),),
    )
    validator = StubValidator(
        {directory: vlc(DependencyStatus.NOT_FOUND, "standard")},
        {directory: ffmpeg(directory, DependencyStatus.AVAILABLE, "standard")},
    )

    result = SystemDependencyService(locator, validator).check()  # type: ignore[arg-type]

    assert not result.snapshot.capabilities.playback_available
    assert result.snapshot.capabilities.cue_analysis_available
    assert result.snapshot.vlc.status == DependencyStatus.NOT_FOUND
    assert result.snapshot.checked_at


def test_configured_check_uses_only_active_user_overrides(tmp_path: Path) -> None:
    user = tmp_path / "user"
    automatic = tmp_path / "automatic"
    locator = StubLocator(
        (candidate(automatic, DependencyCandidateSource.STANDARD),),
        (candidate(user, DependencyCandidateSource.USER),),
    )
    validator = StubValidator(
        {automatic: vlc(DependencyStatus.AVAILABLE, "standard")},
        {user: ffmpeg(user, DependencyStatus.AVAILABLE, "user")},
    )
    database = Database(tmp_path / "configured.db")
    migrate(database)
    settings = SettingsService(PartyPlayerRepository(database))
    settings.set_vlc_installation_path(str(user))
    settings.reset_vlc_installation_path()
    settings.set_ffmpeg_bin_path(str(user))

    result = SystemDependencyService(locator, validator).check_configured(  # type: ignore[arg-type]
        settings
    )

    assert not result.vlc.user_override_configured
    assert result.vlc.effective.source == "standard"
    assert result.ffmpeg.user_override_configured
    assert result.ffmpeg.user_override_valid


def test_dependency_checks_emit_path_free_events_and_probe_metrics(tmp_path: Path, caplog) -> None:
    directory = tmp_path / "private-profile" / "tools"
    locator = StubLocator(
        (candidate(directory, DependencyCandidateSource.STANDARD),),
        (candidate(directory, DependencyCandidateSource.PATH),),
    )
    validator = StubValidator(
        {directory: vlc(DependencyStatus.AVAILABLE, "standard")},
        {directory: ffmpeg(directory, DependencyStatus.AVAILABLE, "path")},
    )
    performance = PerformanceMonitor()
    service = SystemDependencyService(
        locator,  # type: ignore[arg-type]
        validator,  # type: ignore[arg-type]
        performance_monitor=performance,
    )

    with caplog.at_level(logging.INFO):
        service.check()
        service.check()

    statistics = performance.statistics()
    assert statistics["dependencies.full_check"].count == 2
    assert statistics["dependencies.vlc_probe"].count == 2
    assert statistics["dependencies.ffmpeg_probe"].count == 2
    completed = [
        record
        for record in caplog.records
        if getattr(record, "event_code", None) == "DEPENDENCY_CHECK_COMPLETED"
    ]
    changed = [
        record
        for record in caplog.records
        if getattr(record, "event_code", None) == "DEPENDENCY_STATE_CHANGED"
    ]
    assert [record.state_changed for record in completed] == [True, False]
    assert len(changed) == 1
    candidates = [
        record
        for record in caplog.records
        if getattr(record, "event_code", None) == "DEPENDENCY_CANDIDATE_CHECKED"
    ]
    assert len(candidates) == 2
    assert {record.dependency for record in candidates} == {"vlc", "ffmpeg_pair"}
    assert all(record.candidate_rank == 1 for record in candidates)
    assert all(record.selected for record in candidates)
    assert all("path" not in record.__dict__ for record in candidates)
    assert str(directory) not in caplog.text
    assert performance.counters() == {"dependencies.check.success": 2}
    assert performance.gauges() == {
        "dependencies.capability.playback_available": 1.0,
        "dependencies.capability.cue_analysis_available": 1.0,
        "dependencies.capability.loudness_analysis_available": 1.0,
        "dependencies.capability.ffprobe_available": 1.0,
    }
