from pathlib import Path

from party_player.database.connection import Database
from party_player.database.migrations import LATEST_SCHEMA_VERSION, migrate
from party_player.dependency_validator import FfmpegValidationResult
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
from party_player.system_diagnostic_service import (
    AudioDeviceProbe,
    DiagnosticStatus,
    SystemDiagnosticService,
)
from party_player.performance_monitor import PerformanceMonitor
from party_player.network_source_check import NetworkSourceProbeResult
from party_player.repository import PartyPlayerRepository
from party_player.settings_service import SettingsService


def dependency_resolution() -> SystemDependencyResolution:
    vlc = VlcDependencyInfo(DependencyStatus.AVAILABLE, libvlc_loaded=True)
    ffmpeg_info = DependencyInfo("FFmpeg", DependencyStatus.NOT_FOUND)
    ffprobe_info = DependencyInfo("FFprobe", DependencyStatus.NOT_FOUND)
    ffmpeg = FfmpegValidationResult(Path(), ffmpeg_info, ffprobe_info)
    snapshot = SystemDiagnosticSnapshot(
        "dependency-time",
        vlc,
        ffmpeg_info,
        ffprobe_info,
        RuntimeCapabilities.from_dependencies(vlc, ffmpeg_info, ffprobe_info),
    )
    return SystemDependencyResolution(
        snapshot,
        VlcDependencyResolution(vlc, (vlc,), False, False, False),
        FfmpegDependencyResolution(ffmpeg, (ffmpeg,), False, False, False),
    )


def test_quick_diagnostic_reads_schema_without_integrity_check(tmp_path: Path) -> None:
    database = Database(tmp_path / "diagnostic.db")
    migrate(database)

    report = SystemDiagnosticService(database, application_version="1.0.0").check(
        dependency_resolution()
    )

    assert report.application_version == "1.0.0"
    assert report.database.status == DiagnosticStatus.AVAILABLE
    assert report.database.schema_version == LATEST_SCHEMA_VERSION
    assert report.database.integrity_result is None
    assert report.audio.status == DiagnosticStatus.NOT_CHECKED
    assert not report.full_check


def test_full_diagnostic_runs_sqlite_quick_check_and_audio_probe(tmp_path: Path) -> None:
    database = Database(tmp_path / "full-diagnostic.db")
    migrate(database)
    devices = AudioDeviceProbe((("default", "Standardlautsprecher"),), "default")

    report = SystemDiagnosticService(
        database,
        application_version="1.0.0",
        audio_device_provider=lambda: devices,
    ).check(dependency_resolution(), full=True)

    assert report.database.integrity_result == "ok"
    assert report.audio.status == DiagnosticStatus.AVAILABLE
    assert report.audio.device_count == 1
    assert report.audio.default_device_id == "default"
    assert report.full_check


def test_audio_probe_failure_becomes_stable_diagnostic(tmp_path: Path) -> None:
    database = Database(tmp_path / "audio-failure.db")
    migrate(database)

    def fail() -> AudioDeviceProbe:
        raise OSError("Geräte-API nicht erreichbar")

    report = SystemDiagnosticService(
        database,
        application_version="1.0.0",
        audio_device_provider=fail,
    ).check(dependency_resolution())

    assert report.audio.status == DiagnosticStatus.ERROR
    assert report.audio.error_code == "DEP_AUDIO_NO_DEVICE"


def test_diagnostic_probes_record_duration_metrics(tmp_path: Path) -> None:
    database = Database(tmp_path / "metrics.db")
    migrate(database)
    performance = PerformanceMonitor()
    service = SystemDiagnosticService(
        database,
        application_version="1.0.0",
        audio_device_provider=lambda: AudioDeviceProbe((("id", "Gerät"),)),
        network_source_provider=lambda: (r"\\server\music",),
        network_source_probe=lambda source: NetworkSourceProbeResult(source, True),
        performance_monitor=performance,
    )

    service.check(dependency_resolution(), full=True)

    statistics = performance.statistics()
    assert statistics["dependencies.database_check"].count == 1
    assert statistics["dependencies.audio_devices"].count == 1
    assert statistics["dependencies.network_source_check"].count == 1


def test_unknown_default_audio_device_is_explained(tmp_path: Path) -> None:
    database = Database(tmp_path / "unknown-default.db")
    migrate(database)

    report = SystemDiagnosticService(
        database,
        application_version="1.0.0",
        audio_device_provider=lambda: AudioDeviceProbe((("id", "Lautsprecher"),)),
    ).check(dependency_resolution())

    assert report.audio.status == DiagnosticStatus.AVAILABLE
    assert report.audio.default_device_id is None
    assert "nicht zuverlässig bestimmbar" in report.audio.message


def test_full_diagnostic_does_not_mutate_database_settings_or_dependencies(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "read-only.db")
    migrate(database)
    settings = SettingsService(PartyPlayerRepository(database))
    settings.set_vlc_installation_path(r"C:\Configured\VLC")
    dependencies = dependency_resolution()
    dependency_snapshot = repr(dependencies)
    before_settings = settings.dependency_settings()
    with database.connect() as connection:
        before_dump = tuple(connection.iterdump())

    SystemDiagnosticService(
        database,
        application_version="1.0.0",
        audio_device_provider=lambda: AudioDeviceProbe((("id", "Lautsprecher"),)),
        network_source_provider=lambda: (r"\\server\share",),
        network_source_probe=lambda source: NetworkSourceProbeResult(source, True),
    ).check(dependencies, full=True)

    with database.connect() as connection:
        after_dump = tuple(connection.iterdump())
    assert before_dump == after_dump
    assert settings.dependency_settings() == before_settings
    assert repr(dependencies) == dependency_snapshot
