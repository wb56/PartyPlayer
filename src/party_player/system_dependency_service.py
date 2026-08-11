"""Orchestrate dependency discovery, validation, fallback and capabilities."""

from dataclasses import dataclass
from datetime import datetime
import logging
import platform
from pathlib import Path
from time import monotonic

from party_player.dependency_locator import DependencyLocator
from party_player.dependency_validator import (
    DependencyValidator,
    FfmpegValidationResult,
)
from party_player.system_dependencies import (
    DependencyInfo,
    DependencyStatus,
    RuntimeCapabilities,
    SystemDiagnosticSnapshot,
    VlcDependencyInfo,
    DependencySelectionMode,
)
from party_player.settings_service import SettingsService
from party_player.performance_monitor import PerformanceMonitor


@dataclass(frozen=True, slots=True)
class VlcDependencyResolution:
    effective: VlcDependencyInfo
    attempts: tuple[VlcDependencyInfo, ...]
    user_override_configured: bool
    user_override_valid: bool
    automatic_fallback_used: bool


@dataclass(frozen=True, slots=True)
class FfmpegDependencyResolution:
    effective: FfmpegValidationResult
    attempts: tuple[FfmpegValidationResult, ...]
    user_override_configured: bool
    user_override_valid: bool
    automatic_fallback_used: bool


@dataclass(frozen=True, slots=True)
class SystemDependencyResolution:
    snapshot: SystemDiagnosticSnapshot
    vlc: VlcDependencyResolution
    ffmpeg: FfmpegDependencyResolution


class SystemDependencyService:
    """Resolve effective dependencies without mutating persisted user choices."""

    def __init__(
        self,
        locator: DependencyLocator,
        validator: DependencyValidator,
        *,
        application_version: str = "",
        performance_monitor: PerformanceMonitor | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._locator = locator
        self._validator = validator
        self._application_version = application_version
        self._performance = performance_monitor or PerformanceMonitor()
        self._logger = logger or logging.getLogger(__name__)
        self._last_state: tuple[object, ...] | None = None
        self._candidate_states: dict[tuple[str, int, str | None], tuple[object, ...]] = {}

    def check(
        self,
        *,
        vlc_user_directory: str | Path | None = None,
        ffmpeg_user_bin_directory: str | Path | None = None,
        detected_vlc_directories: tuple[str | Path, ...] = (),
        detected_ffmpeg_directories: tuple[str | Path, ...] = (),
    ) -> SystemDependencyResolution:
        return self._check(
            vlc_user_directory=vlc_user_directory,
            ffmpeg_user_bin_directory=ffmpeg_user_bin_directory,
            detected_vlc_directories=detected_vlc_directories,
            detected_ffmpeg_directories=detected_ffmpeg_directories,
            quick=False,
        )

    def check_quick(
        self,
        *,
        vlc_user_directory: str | Path | None = None,
        ffmpeg_user_bin_directory: str | Path | None = None,
        detected_vlc_directories: tuple[str | Path, ...] = (),
        detected_ffmpeg_directories: tuple[str | Path, ...] = (),
    ) -> SystemDependencyResolution:
        return self._check(
            vlc_user_directory=vlc_user_directory,
            ffmpeg_user_bin_directory=ffmpeg_user_bin_directory,
            detected_vlc_directories=detected_vlc_directories,
            detected_ffmpeg_directories=detected_ffmpeg_directories,
            quick=True,
        )

    def _check(
        self,
        *,
        vlc_user_directory: str | Path | None,
        ffmpeg_user_bin_directory: str | Path | None,
        detected_vlc_directories: tuple[str | Path, ...],
        detected_ffmpeg_directories: tuple[str | Path, ...],
        quick: bool,
    ) -> SystemDependencyResolution:
        operation = "dependencies.quick_check" if quick else "dependencies.full_check"
        started = monotonic()
        self._logger.info(
            "Dependency-Prüfung gestartet",
            extra={
                "event_code": "DEPENDENCY_CHECK_STARTED",
                "check_mode": "quick" if quick else "full",
            },
        )
        vlc_candidates = self._locator.locate_vlc(
            vlc_user_directory,
            detected_directories=detected_vlc_directories,
        )
        ffmpeg_candidates = self._locator.locate_ffmpeg(
            ffmpeg_user_bin_directory,
            detected_directories=detected_ffmpeg_directories,
        )
        validate_vlc = self._validator.validate_vlc_quick if quick else self._validator.validate_vlc
        validate_ffmpeg = (
            self._validator.validate_ffmpeg_quick if quick else self._validator.validate_ffmpeg
        )
        vlc_attempts = []
        for candidate in vlc_candidates:
            with self._performance.measure(
                "dependencies.vlc_probe",
                warning_threshold_ms=5_000.0,
                context={"source": candidate.source.value},
            ):
                vlc_attempts.append(validate_vlc(candidate))
        vlc = self._resolve_vlc(
            tuple(vlc_attempts),
            bool(vlc_user_directory and str(vlc_user_directory).strip()),
        )
        ffmpeg_attempts = []
        for candidate in ffmpeg_candidates:
            with self._performance.measure(
                "dependencies.ffmpeg_probe",
                warning_threshold_ms=5_000.0,
                context={"source": candidate.source.value},
            ):
                ffmpeg_attempts.append(validate_ffmpeg(candidate))
        ffmpeg = self._resolve_ffmpeg(
            tuple(ffmpeg_attempts),
            bool(ffmpeg_user_bin_directory and str(ffmpeg_user_bin_directory).strip()),
        )
        capabilities = RuntimeCapabilities.from_dependencies(
            vlc.effective,
            ffmpeg.effective.ffmpeg,
            ffmpeg.effective.ffprobe,
        )
        snapshot = SystemDiagnosticSnapshot(
            datetime.now().astimezone().isoformat(),
            vlc.effective,
            ffmpeg.effective.ffmpeg,
            ffmpeg.effective.ffprobe,
            capabilities,
            f"{platform.system()} {platform.release()} ({platform.machine()})".strip(),
            self._application_version,
        )
        resolution = SystemDependencyResolution(snapshot, vlc, ffmpeg)
        self._log_candidate_states(vlc, ffmpeg)
        check_succeeded = snapshot.capabilities.playback_available
        self._performance.increment_counter(
            "dependencies.check.success" if check_succeeded else "dependencies.check.failed"
        )
        timeout_count = sum(
            1
            for error_code in (
                *(attempt.error_code for attempt in vlc.attempts),
                *(
                    info.error_code
                    for attempt in ffmpeg.attempts
                    for info in (attempt.ffmpeg, attempt.ffprobe)
                ),
            )
            if error_code and "TIMEOUT" in error_code
        )
        if timeout_count:
            self._performance.increment_counter("dependencies.check.timeout", timeout_count)
        for capability_name, available in (
            (
                "playback_available",
                snapshot.capabilities.playback_available,
            ),
            (
                "cue_analysis_available",
                snapshot.capabilities.cue_analysis_available,
            ),
            (
                "loudness_analysis_available",
                snapshot.capabilities.loudness_analysis_available,
            ),
            (
                "ffprobe_available",
                snapshot.capabilities.ffprobe_available,
            ),
        ):
            self._performance.set_gauge(f"dependencies.capability.{capability_name}", available)
        elapsed_ms = max(0.0, (monotonic() - started) * 1000.0)
        self._performance.record(
            operation, elapsed_ms, 10_000.0, {"mode": "quick" if quick else "full"}
        )
        state = (
            snapshot.vlc.status,
            snapshot.vlc.source,
            snapshot.vlc.version,
            snapshot.ffmpeg.status,
            snapshot.ffmpeg.source,
            snapshot.ffmpeg.version,
            snapshot.ffprobe.status,
            snapshot.capabilities,
        )
        changed = state != self._last_state
        self._last_state = state
        self._logger.info(
            "Dependency-Prüfung beendet",
            extra={
                "event_code": "DEPENDENCY_CHECK_COMPLETED",
                "check_mode": "quick" if quick else "full",
                "elapsed_ms": round(elapsed_ms, 1),
                "state_changed": changed,
                "vlc_status": snapshot.vlc.status.value,
                "vlc_source": snapshot.vlc.source,
                "vlc_version": snapshot.vlc.version,
                "vlc_error_code": snapshot.vlc.error_code,
                "ffmpeg_status": snapshot.ffmpeg.status.value,
                "ffmpeg_source": snapshot.ffmpeg.source,
                "ffmpeg_version": snapshot.ffmpeg.version,
                "ffmpeg_error_code": snapshot.ffmpeg.error_code,
                "playback_available": snapshot.capabilities.playback_available,
                "analysis_available": snapshot.capabilities.cue_analysis_available,
            },
        )
        if changed:
            self._logger.info(
                "Dependency-Zustand geändert",
                extra={
                    "event_code": "DEPENDENCY_STATE_CHANGED",
                    "vlc_status": snapshot.vlc.status.value,
                    "ffmpeg_status": snapshot.ffmpeg.status.value,
                },
            )
        return resolution

    def _log_candidate_states(
        self,
        vlc: VlcDependencyResolution,
        ffmpeg: FfmpegDependencyResolution,
    ) -> None:
        current_keys: set[tuple[str, int, str | None]] = set()
        for rank, vlc_attempt in enumerate(vlc.attempts, start=1):
            key = ("vlc", rank, vlc_attempt.source)
            current_keys.add(key)
            vlc_state = (
                vlc_attempt.status,
                vlc_attempt.version,
                vlc_attempt.error_code,
                vlc_attempt == vlc.effective,
            )
            if self._candidate_states.get(key) == vlc_state:
                continue
            self._candidate_states[key] = vlc_state
            self._logger.info(
                "VLC-Kandidat geprüft",
                extra={
                    "event_code": "DEPENDENCY_CANDIDATE_CHECKED",
                    "dependency": "vlc",
                    "candidate_rank": rank,
                    "candidate_source": vlc_attempt.source,
                    "status": vlc_attempt.status.value,
                    "version": vlc_attempt.version,
                    "error_code": vlc_attempt.error_code,
                    "selected": vlc_attempt == vlc.effective,
                },
            )
        for rank, ffmpeg_attempt in enumerate(ffmpeg.attempts, start=1):
            source = ffmpeg_attempt.ffmpeg.source or ffmpeg_attempt.ffprobe.source
            key = ("ffmpeg", rank, source)
            current_keys.add(key)
            ffmpeg_state = (
                ffmpeg_attempt.ffmpeg.status,
                ffmpeg_attempt.ffprobe.status,
                ffmpeg_attempt.ffmpeg.version,
                ffmpeg_attempt.ffprobe.version,
                ffmpeg_attempt.ffmpeg.error_code,
                ffmpeg_attempt.ffprobe.error_code,
                ffmpeg_attempt == ffmpeg.effective,
            )
            if self._candidate_states.get(key) == ffmpeg_state:
                continue
            self._candidate_states[key] = ffmpeg_state
            self._logger.info(
                "FFmpeg-Kandidat geprüft",
                extra={
                    "event_code": "DEPENDENCY_CANDIDATE_CHECKED",
                    "dependency": "ffmpeg_pair",
                    "candidate_rank": rank,
                    "candidate_source": source,
                    "ffmpeg_status": ffmpeg_attempt.ffmpeg.status.value,
                    "ffprobe_status": ffmpeg_attempt.ffprobe.status.value,
                    "ffmpeg_version": ffmpeg_attempt.ffmpeg.version,
                    "ffprobe_version": ffmpeg_attempt.ffprobe.version,
                    "ffmpeg_error_code": ffmpeg_attempt.ffmpeg.error_code,
                    "ffprobe_error_code": ffmpeg_attempt.ffprobe.error_code,
                    "selected": ffmpeg_attempt == ffmpeg.effective,
                },
            )
        for stale_key in self._candidate_states.keys() - current_keys:
            del self._candidate_states[stale_key]

    def check_configured(
        self,
        settings: SettingsService,
        *,
        detected_vlc_directories: tuple[str | Path, ...] = (),
        detected_ffmpeg_directories: tuple[str | Path, ...] = (),
    ) -> SystemDependencyResolution:
        """Resolve dependencies from persisted modes without changing settings."""
        configured = settings.dependency_settings()
        vlc_path = (
            configured.vlc_installation_path
            if configured.vlc_selection_mode == DependencySelectionMode.USER
            else None
        )
        ffmpeg_path = (
            configured.ffmpeg_bin_path
            if configured.ffmpeg_selection_mode == DependencySelectionMode.USER
            else None
        )
        return self.check(
            vlc_user_directory=vlc_path,
            ffmpeg_user_bin_directory=ffmpeg_path,
            detected_vlc_directories=detected_vlc_directories,
            detected_ffmpeg_directories=detected_ffmpeg_directories,
        )

    def check_quick_configured(
        self,
        settings: SettingsService,
        *,
        detected_vlc_directories: tuple[str | Path, ...] = (),
        detected_ffmpeg_directories: tuple[str | Path, ...] = (),
    ) -> SystemDependencyResolution:
        configured = settings.dependency_settings()
        return self.check_quick(
            vlc_user_directory=(
                configured.vlc_installation_path
                if configured.vlc_selection_mode == DependencySelectionMode.USER
                else None
            ),
            ffmpeg_user_bin_directory=(
                configured.ffmpeg_bin_path
                if configured.ffmpeg_selection_mode == DependencySelectionMode.USER
                else None
            ),
            detected_vlc_directories=detected_vlc_directories,
            detected_ffmpeg_directories=detected_ffmpeg_directories,
        )

    def validate_vlc_directory(self, directory: str | Path) -> VlcDependencyInfo:
        candidates = self._locator.locate_vlc(directory)
        if not candidates:
            raise ValueError("Kein VLC-Verzeichnis angegeben")
        return self._validator.validate_vlc(candidates[0])

    def validate_ffmpeg_directory(self, directory: str | Path) -> FfmpegValidationResult:
        candidates = self._locator.locate_ffmpeg(directory)
        if not candidates:
            raise ValueError("Kein FFmpeg-bin-Verzeichnis angegeben")
        return self._validator.validate_ffmpeg(candidates[0])

    @staticmethod
    def _resolve_vlc(
        attempts: tuple[VlcDependencyInfo, ...], user_configured: bool
    ) -> VlcDependencyResolution:
        effective_index = next(
            (
                index
                for index, result in enumerate(attempts)
                if result.status == DependencyStatus.AVAILABLE
            ),
            0,
        )
        effective = (
            attempts[effective_index]
            if attempts
            else VlcDependencyInfo(
                DependencyStatus.NOT_FOUND,
                message="Keine VLC-Kandidaten gefunden",
                error_code="DEP_VLC_NOT_FOUND",
            )
        )
        user_valid = bool(
            user_configured
            and attempts
            and attempts[0].source == "user"
            and attempts[0].status == DependencyStatus.AVAILABLE
        )
        fallback = bool(user_configured and not user_valid and effective_index > 0)
        return VlcDependencyResolution(
            effective,
            attempts,
            user_configured,
            user_valid,
            fallback,
        )

    @staticmethod
    def _resolve_ffmpeg(
        attempts: tuple[FfmpegValidationResult, ...], user_configured: bool
    ) -> FfmpegDependencyResolution:
        effective_index = next(
            (index for index, result in enumerate(attempts) if result.available),
            0,
        )
        if attempts:
            effective = attempts[effective_index]
        else:
            missing_ffmpeg = DependencyInfo(
                "FFmpeg",
                DependencyStatus.NOT_FOUND,
                message="Keine FFmpeg-Kandidaten gefunden",
                error_code="DEP_FFMPEG_NOT_FOUND",
            )
            missing_ffprobe = DependencyInfo(
                "FFprobe",
                DependencyStatus.NOT_FOUND,
                message="Keine FFprobe-Kandidaten gefunden",
                error_code="DEP_FFPROBE_NOT_FOUND",
            )
            effective = FfmpegValidationResult(Path(), missing_ffmpeg, missing_ffprobe)
        user_valid = bool(
            user_configured
            and attempts
            and attempts[0].ffmpeg.source == "user"
            and attempts[0].available
        )
        fallback = bool(user_configured and not user_valid and effective_index > 0)
        return FfmpegDependencyResolution(
            effective,
            attempts,
            user_configured,
            user_valid,
            fallback,
        )
