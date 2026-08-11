from pathlib import Path

import pytest

from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.first_run_controller import FirstRunController, FirstRunReason
from party_player.repository import PartyPlayerRepository
from party_player.settings_service import SettingsService
from party_player.system_dependencies import (
    DependencyInfo,
    DependencyStatus,
    RuntimeCapabilities,
    SystemDiagnosticSnapshot,
    VlcDependencyInfo,
)
from party_player.system_dependency_service import (
    FfmpegDependencyResolution,
    SystemDependencyResolution,
    VlcDependencyResolution,
)
from party_player.dependency_validator import FfmpegValidationResult
from party_player.performance_monitor import PerformanceMonitor


def resolution(*, playback: bool) -> SystemDependencyResolution:
    vlc_status = DependencyStatus.AVAILABLE if playback else DependencyStatus.NOT_FOUND
    vlc = VlcDependencyInfo(vlc_status, libvlc_loaded=playback)
    missing_ffmpeg = DependencyInfo("FFmpeg", DependencyStatus.NOT_FOUND)
    missing_ffprobe = DependencyInfo("FFprobe", DependencyStatus.NOT_FOUND)
    ffmpeg = FfmpegValidationResult(Path(), missing_ffmpeg, missing_ffprobe)
    snapshot = SystemDiagnosticSnapshot(
        "now",
        vlc,
        missing_ffmpeg,
        missing_ffprobe,
        RuntimeCapabilities.from_dependencies(vlc, missing_ffmpeg, missing_ffprobe),
    )
    return SystemDependencyResolution(
        snapshot,
        VlcDependencyResolution(vlc, (vlc,), False, False, False),
        FfmpegDependencyResolution(ffmpeg, (ffmpeg,), False, False, False),
    )


class StubDependencyService:
    def __init__(self, result: SystemDependencyResolution) -> None:
        self.result = result
        self.full_checks = 0
        self.quick_checks = 0

    def check_configured(self, _settings: SettingsService) -> SystemDependencyResolution:
        self.full_checks += 1
        return self.result

    def check_quick_configured(self, _settings: SettingsService) -> SystemDependencyResolution:
        self.quick_checks += 1
        return self.result

    def validate_vlc_directory(self, _directory: str) -> VlcDependencyInfo:
        return self.result.snapshot.vlc

    def validate_ffmpeg_directory(self, _directory: str) -> FfmpegValidationResult:
        return self.result.ffmpeg.effective


def settings_at(tmp_path: Path) -> SettingsService:
    database = Database(tmp_path / "first-run.db")
    migrate(database)
    return SettingsService(PartyPlayerRepository(database))


def test_never_completed_uses_full_check_and_requires_setup(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    dependencies = StubDependencyService(resolution(playback=True))
    controller = FirstRunController(settings, dependencies, "1.0.0")  # type: ignore[arg-type]

    decision = controller.check_startup()

    assert decision.reason == FirstRunReason.NEVER_COMPLETED
    assert decision.requires_setup
    assert not decision.used_quick_check
    assert dependencies.full_checks == 1


def test_pending_setup_reason_performs_no_dependency_check(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    dependencies = StubDependencyService(resolution(playback=True))
    controller = FirstRunController(settings, dependencies, "1.0.0")  # type: ignore[arg-type]

    reason = controller.pending_setup_reason()

    assert reason == FirstRunReason.NEVER_COMPLETED
    assert dependencies.full_checks == 0
    assert dependencies.quick_checks == 0


def test_matching_completed_version_uses_quick_check(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    settings.set_first_run_completed(True)
    settings.set_system_check_completed_version("1.0.0")
    dependencies = StubDependencyService(resolution(playback=True))
    controller = FirstRunController(settings, dependencies, "1.0.0")  # type: ignore[arg-type]

    decision = controller.check_startup()

    assert decision.reason == FirstRunReason.READY
    assert decision.used_quick_check
    assert dependencies.quick_checks == 1


def test_version_change_uses_full_check_and_requires_setup(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    settings.set_first_run_completed(True)
    settings.set_system_check_completed_version("0.9.0")
    dependencies = StubDependencyService(resolution(playback=True))

    decision = FirstRunController(  # type: ignore[arg-type]
        settings, dependencies, "1.0.0"
    ).check_startup()

    assert decision.reason == FirstRunReason.VERSION_CHANGED
    assert not decision.used_quick_check
    assert dependencies.full_checks == 1


def test_setup_completion_requires_playback_and_persists_version(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    dependencies = StubDependencyService(resolution(playback=False))
    controller = FirstRunController(settings, dependencies, "1.0.0")  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        controller.complete_setup(dependencies.result)
    assert not settings.first_run_completed()

    controller.complete_setup(resolution(playback=True))
    assert settings.first_run_completed()
    assert settings.system_check_completed_version() == "1.0.0"


def test_setup_completion_records_counter_and_current_state(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    dependencies = StubDependencyService(resolution(playback=True))
    performance = PerformanceMonitor()
    controller = FirstRunController(
        settings,
        dependencies,  # type: ignore[arg-type]
        "1.0.0",
        performance,
    )

    controller.complete_setup(dependencies.result)

    assert performance.counters() == {"first_run.completed": 1}
    assert performance.gauges() == {"first_run.setup_completed": 1.0}


def test_invalid_path_proposal_does_not_replace_persisted_override(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    settings.set_vlc_installation_path("C:/working-vlc")
    dependencies = StubDependencyService(resolution(playback=False))
    controller = FirstRunController(settings, dependencies, "1.0.0")  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        controller.select_vlc_directory("C:/broken-vlc")

    assert settings.vlc_installation_path() == "C:/working-vlc"


def test_valid_path_proposals_are_saved_only_after_validation(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    selected = tmp_path / "selected-vlc"
    available = resolution(playback=True)
    vlc = available.snapshot.vlc
    object.__setattr__(vlc, "installation_directory", selected)
    dependencies = StubDependencyService(available)
    controller = FirstRunController(settings, dependencies, "1.0.0")  # type: ignore[arg-type]

    result = controller.select_vlc_directory(str(selected))

    assert result is available
    assert settings.vlc_installation_path() == str(selected)
