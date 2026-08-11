"""Decision boundary for first-run setup and normal startup checks."""

from dataclasses import dataclass
from enum import StrEnum
from time import monotonic

from party_player.settings_service import SettingsService
from party_player.system_dependency_service import (
    SystemDependencyResolution,
    SystemDependencyService,
)
from party_player.system_dependencies import DependencyStatus
from party_player.performance_monitor import PerformanceMonitor


class FirstRunReason(StrEnum):
    READY = "READY"
    NEVER_COMPLETED = "NEVER_COMPLETED"
    VERSION_CHANGED = "VERSION_CHANGED"
    PLAYBACK_UNAVAILABLE = "PLAYBACK_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class StartupDependencyDecision:
    reason: FirstRunReason
    requires_setup: bool
    used_quick_check: bool
    resolution: SystemDependencyResolution


class FirstRunController:
    """Select full/quick checks and persist only confirmed setup completion."""

    def __init__(
        self,
        settings: SettingsService,
        dependencies: SystemDependencyService,
        application_version: str,
        performance_monitor: PerformanceMonitor | None = None,
    ) -> None:
        self._settings = settings
        self._dependencies = dependencies
        self._application_version = application_version.strip()
        self._performance = performance_monitor or PerformanceMonitor()

    def check_startup(self) -> StartupDependencyDecision:
        started = monotonic()
        pending_reason = self.pending_setup_reason()
        use_quick = pending_reason is None
        resolution = (
            self._dependencies.check_quick_configured(self._settings)
            if use_quick
            else self._dependencies.check_configured(self._settings)
        )
        if pending_reason is not None:
            reason = pending_reason
        elif not resolution.snapshot.capabilities.playback_available:
            reason = FirstRunReason.PLAYBACK_UNAVAILABLE
        else:
            reason = FirstRunReason.READY
        decision = StartupDependencyDecision(
            reason,
            reason != FirstRunReason.READY,
            use_quick,
            resolution,
        )
        self._performance.record(
            "first_run.total",
            max(0.0, (monotonic() - started) * 1000.0),
            15_000.0,
            {"reason": reason.value},
        )
        return decision

    def pending_setup_reason(self) -> FirstRunReason | None:
        """Return setup state without performing any dependency I/O."""
        if not self._settings.first_run_completed():
            return FirstRunReason.NEVER_COMPLETED
        if self._settings.system_check_completed_version() != self._application_version:
            return FirstRunReason.VERSION_CHANGED
        return None

    def check_quick_startup(self) -> StartupDependencyDecision:
        """Run only the bounded normal-start check after completed setup."""
        started = monotonic()
        resolution = self._dependencies.check_quick_configured(self._settings)
        available = resolution.snapshot.capabilities.playback_available
        decision = StartupDependencyDecision(
            FirstRunReason.READY if available else FirstRunReason.PLAYBACK_UNAVAILABLE,
            not available,
            True,
            resolution,
        )
        self._performance.record(
            "first_run.total",
            max(0.0, (monotonic() - started) * 1000.0),
            15_000.0,
            {"reason": decision.reason.value},
        )
        return decision

    def complete_setup(self, resolution: SystemDependencyResolution) -> None:
        if not resolution.snapshot.capabilities.playback_available:
            raise ValueError(
                "Der Erststart kann ohne funktionsfähiges VLC nicht abgeschlossen werden"
            )
        self._settings.set_first_run_completed(True)
        self._settings.set_system_check_completed_version(self._application_version)
        self._performance.increment_counter("first_run.completed")
        self._performance.set_gauge("first_run.setup_completed", True)

    def select_vlc_directory(self, directory: str) -> SystemDependencyResolution:
        proposal = self._dependencies.validate_vlc_directory(directory)
        if proposal.status != DependencyStatus.AVAILABLE:
            raise ValueError(proposal.message or "Das gewählte VLC-Verzeichnis ist ungültig")
        assert proposal.installation_directory is not None
        self._settings.set_vlc_installation_path(str(proposal.installation_directory))
        return self._dependencies.check_configured(self._settings)

    def select_ffmpeg_directory(self, directory: str) -> SystemDependencyResolution:
        proposal = self._dependencies.validate_ffmpeg_directory(directory)
        if not proposal.available:
            messages = [
                info.message
                for info in (proposal.ffmpeg, proposal.ffprobe)
                if info.status != DependencyStatus.AVAILABLE and info.message
            ]
            raise ValueError("; ".join(messages) or "Das gewählte FFmpeg-Verzeichnis ist ungültig")
        self._settings.set_ffmpeg_bin_path(str(proposal.installation_directory))
        return self._dependencies.check_configured(self._settings)
