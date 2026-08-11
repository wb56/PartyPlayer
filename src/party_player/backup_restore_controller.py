"""Asynchronous application boundary for manual backup and restore operations."""

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import logging
from pathlib import Path
from threading import Lock
from time import monotonic

from party_player.backup_service import BackupResult, BackupService
from party_player.restore_pipeline import AtomicRestorePipeline, RestorePipelineResult
from party_player.database_maintenance import (
    DatabaseMaintenanceOperation,
    DatabaseMaintenanceResult,
    DatabaseMaintenanceService,
)
from party_player.performance_monitor import PerformanceMonitor
from party_player.database.migrations import LATEST_SCHEMA_VERSION
from party_player.equalizer_transfer import (
    EqualizerConflictStrategy,
    EqualizerImportPreview,
    EqualizerTransferResult,
    EqualizerTransferService,
)
from party_player.playlist_transfer import (
    PlaylistConflictStrategy,
    PlaylistImportPreview,
    PlaylistTransferFormat,
    PlaylistTransferResult,
    PlaylistTransferService,
)
from party_player.media_path_remap import (
    MediaPathRemapPreview,
    MediaPathRemapResult,
    MediaPathRemapService,
)
from party_player.overlay_transfer import (
    OverlayConflictStrategy,
    OverlayImportPreview,
    OverlayTransferResult,
    OverlayTransferService,
)
from party_player.restore_safety import (
    RestoreSafetyBlocker,
    RestoreSafetyReason,
    RestoreSafetyResult,
)


class BackupRestoreOperation(StrEnum):
    BACKUP = "BACKUP"
    RESTORE = "RESTORE"
    MAINTENANCE = "MAINTENANCE"
    PLAYLIST_EXPORT = "PLAYLIST_EXPORT"
    PLAYLIST_IMPORT_PREVIEW = "PLAYLIST_IMPORT_PREVIEW"
    PLAYLIST_IMPORT = "PLAYLIST_IMPORT"
    MEDIA_PATH_REMAP_PREVIEW = "MEDIA_PATH_REMAP_PREVIEW"
    MEDIA_PATH_REMAP = "MEDIA_PATH_REMAP"
    EQUALIZER_EXPORT = "EQUALIZER_EXPORT"
    EQUALIZER_IMPORT_PREVIEW = "EQUALIZER_IMPORT_PREVIEW"
    EQUALIZER_IMPORT = "EQUALIZER_IMPORT"
    OVERLAY_EXPORT = "OVERLAY_EXPORT"
    OVERLAY_IMPORT_PREVIEW = "OVERLAY_IMPORT_PREVIEW"
    OVERLAY_IMPORT = "OVERLAY_IMPORT"


class BackupRestoreUiState(StrEnum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BUSY = "BUSY"
    RESTART_REQUIRED = "RESTART_REQUIRED"


@dataclass(frozen=True, slots=True)
class BackupRestoreUiResult:
    operation: BackupRestoreOperation
    state: BackupRestoreUiState
    message: str
    path: Path | None = None
    error_code: str = ""
    created_at: str = ""
    findings: tuple[str, ...] = ()
    operation_detail: str = ""
    backup_target: str = ""
    schema_version: int | None = None
    playlist_preview: PlaylistImportPreview | None = None
    media_path_preview: MediaPathRemapPreview | None = None
    equalizer_preview: EqualizerImportPreview | None = None
    overlay_preview: OverlayImportPreview | None = None


class BackupRestoreController:
    """Run database operations away from Tk and serialize UI requests."""

    def __init__(
        self,
        backup_service: BackupService,
        restore_pipeline: AtomicRestorePipeline | None,
        schedule: Callable[[int, Callable[[], None]], object],
        completed: Callable[[BackupRestoreUiResult], None],
        *,
        restore_unavailable_reason: str = "Restore ist nicht verfügbar.",
        maintenance_service: DatabaseMaintenanceService | None = None,
        manual_backup_recorded: Callable[[str, str], None] | None = None,
        last_manual_backup: tuple[str, str] | None = None,
        performance_monitor: PerformanceMonitor | None = None,
        logger: logging.Logger | None = None,
        now: Callable[[], datetime] | None = None,
        playlist_transfer_service: PlaylistTransferService | None = None,
        media_path_remap_service: MediaPathRemapService | None = None,
        equalizer_transfer_service: EqualizerTransferService | None = None,
        overlay_transfer_service: OverlayTransferService | None = None,
    ) -> None:
        self._backup = backup_service
        self._restore = restore_pipeline
        self._schedule = schedule
        self._completed = completed
        self._restore_unavailable_reason = restore_unavailable_reason
        self._maintenance = maintenance_service
        self._manual_backup_recorded = manual_backup_recorded
        self._last_manual_backup = last_manual_backup
        self._last_result: BackupRestoreUiResult | None = None
        self._performance = performance_monitor or PerformanceMonitor()
        self._logger = logger or logging.getLogger(__name__)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._playlist_transfer = playlist_transfer_service
        self._media_path_remap = media_path_remap_service
        self._equalizer_transfer = equalizer_transfer_service
        self._overlay_transfer = overlay_transfer_service
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="backup-restore")
        self._lock = Lock()
        self._active = False
        self._closed = False

    def start_backup(self, destination: Path) -> bool:
        return self._submit(
            BackupRestoreOperation.BACKUP,
            lambda: self._backup_result(self._backup.create_backup(destination)),
            "backup.create.total",
            operation_detail="CREATE",
            backup_target=str(destination),
        )

    def last_manual_backup(self) -> tuple[str, str] | None:
        with self._lock:
            return self._last_manual_backup

    def diagnostic_status(self) -> tuple[str, ...]:
        with self._lock:
            result = self._last_result
            backup = self._last_manual_backup
        return (
            f"last_manual_backup_at: {backup[0] if backup else 'none'}",
            f"last_manual_backup_path: {backup[1] if backup else 'none'}",
            f"last_data_operation: {result.operation.value if result else 'none'}",
            f"last_data_operation_state: {result.state.value if result else 'none'}",
            f"last_data_operation_error: {result.error_code if result and result.error_code else 'none'}",
            *(
                f"last_maintenance_finding: {item}"
                for item in (result.findings[:10] if result else ())
            ),
        )

    def start_restore(self, archive: Path, safety_backup_directory: Path) -> bool:
        restore = self._restore
        if restore is None:
            self._publish(
                BackupRestoreUiResult(
                    BackupRestoreOperation.RESTORE,
                    BackupRestoreUiState.FAILED,
                    self._restore_unavailable_reason,
                    error_code="RESTORE_RUNTIME_UNAVAILABLE",
                )
            )
            return False
        return self._submit(
            BackupRestoreOperation.RESTORE,
            lambda: self._restore_result(restore.execute(archive, safety_backup_directory)),
            "restore.total",
            operation_detail="RESTORE",
            backup_target=str(safety_backup_directory),
        )

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)

    def start_quick_check(self) -> bool:
        return self._start_maintenance(DatabaseMaintenanceOperation.QUICK_CHECK)

    def start_integrity_check(self) -> bool:
        return self._start_maintenance(DatabaseMaintenanceOperation.INTEGRITY_CHECK)

    def start_analyze(self) -> bool:
        return self._start_maintenance(DatabaseMaintenanceOperation.ANALYZE)

    def start_vacuum(self) -> bool:
        return self._start_maintenance(DatabaseMaintenanceOperation.VACUUM)

    def start_reindex(self) -> bool:
        return self._start_maintenance(DatabaseMaintenanceOperation.REINDEX)

    def start_playlist_export(
        self,
        saved_queue_id: int,
        destination: Path,
        format: PlaylistTransferFormat,
    ) -> bool:
        transfer = self._playlist_transfer
        if transfer is None:
            return False
        return self._submit(
            BackupRestoreOperation.PLAYLIST_EXPORT,
            lambda: self._playlist_transfer_result(
                BackupRestoreOperation.PLAYLIST_EXPORT,
                transfer.export(saved_queue_id, destination, format),
                format,
            ),
            "playlist.export.total",
            operation_detail=format.value,
            backup_target=str(destination.parent),
        )

    def start_playlist_import_preview(self, source: Path, format: PlaylistTransferFormat) -> bool:
        transfer = self._playlist_transfer
        if transfer is None:
            return False
        return self._submit(
            BackupRestoreOperation.PLAYLIST_IMPORT_PREVIEW,
            lambda: self._playlist_preview_result(transfer.preview_import(source, format)),
            "playlist.import.preview",
            operation_detail=format.value,
        )

    def start_playlist_import(
        self,
        preview: PlaylistImportPreview,
        conflict: PlaylistConflictStrategy,
    ) -> bool:
        transfer = self._playlist_transfer
        if transfer is None:
            return False
        return self._submit(
            BackupRestoreOperation.PLAYLIST_IMPORT,
            lambda: self._playlist_transfer_result(
                BackupRestoreOperation.PLAYLIST_IMPORT,
                transfer.import_preview(preview, conflict),
                preview.format,
            ),
            "playlist.import.total",
            operation_detail=f"{preview.format.value}:{conflict.value}",
        )

    def start_media_path_remap_preview(self, old_base_path: str, new_base_path: str) -> bool:
        remap = self._media_path_remap
        if remap is None:
            return False
        return self._submit(
            BackupRestoreOperation.MEDIA_PATH_REMAP_PREVIEW,
            lambda: self._media_path_preview_result(remap.preview(old_base_path, new_base_path)),
            "media_path.remap.preview",
            operation_detail="PREVIEW",
        )

    def start_media_path_remap(self, preview: MediaPathRemapPreview) -> bool:
        remap = self._media_path_remap
        if remap is None:
            return False
        return self._submit(
            BackupRestoreOperation.MEDIA_PATH_REMAP,
            lambda: self._media_path_remap_result(remap.commit(preview)),
            "media_path.remap.total",
            operation_detail="COMMIT",
        )

    def start_equalizer_export(self, preset_key: str, destination: Path) -> bool:
        transfer = self._equalizer_transfer
        if transfer is None:
            return False
        return self._submit(
            BackupRestoreOperation.EQUALIZER_EXPORT,
            lambda: self._equalizer_transfer_result(
                BackupRestoreOperation.EQUALIZER_EXPORT,
                transfer.export(preset_key, destination),
                "JSON",
            ),
            "equalizer.export.total",
            operation_detail="JSON",
            backup_target=str(destination.parent),
        )

    def start_equalizer_import_preview(self, source: Path) -> bool:
        transfer = self._equalizer_transfer
        if transfer is None:
            return False
        return self._submit(
            BackupRestoreOperation.EQUALIZER_IMPORT_PREVIEW,
            lambda: self._equalizer_preview_result(transfer.preview_import(source)),
            "equalizer.import.preview",
            operation_detail="JSON",
        )

    def start_equalizer_import(
        self,
        preview: EqualizerImportPreview,
        strategy: EqualizerConflictStrategy,
    ) -> bool:
        transfer = self._equalizer_transfer
        if transfer is None:
            return False
        return self._submit(
            BackupRestoreOperation.EQUALIZER_IMPORT,
            lambda: self._equalizer_transfer_result(
                BackupRestoreOperation.EQUALIZER_IMPORT,
                transfer.import_preview(preview, strategy),
                strategy.value,
            ),
            "equalizer.import.total",
            operation_detail=strategy.value,
        )

    def start_overlay_export(self, destination: Path) -> bool:
        transfer = self._overlay_transfer
        if transfer is None:
            return False
        return self._submit(
            BackupRestoreOperation.OVERLAY_EXPORT,
            lambda: self._overlay_transfer_result(
                BackupRestoreOperation.OVERLAY_EXPORT, transfer.export(destination), "JSON"
            ),
            "overlay.export.total",
            operation_detail="JSON",
            backup_target=str(destination.parent),
        )

    def start_overlay_import_preview(self, source: Path) -> bool:
        transfer = self._overlay_transfer
        if transfer is None:
            return False
        return self._submit(
            BackupRestoreOperation.OVERLAY_IMPORT_PREVIEW,
            lambda: self._overlay_preview_result(transfer.preview_import(source)),
            "overlay.import.preview",
            operation_detail="JSON",
        )

    def start_overlay_import(
        self, preview: OverlayImportPreview, strategy: OverlayConflictStrategy
    ) -> bool:
        transfer = self._overlay_transfer
        if transfer is None:
            return False
        return self._submit(
            BackupRestoreOperation.OVERLAY_IMPORT,
            lambda: self._overlay_transfer_result(
                BackupRestoreOperation.OVERLAY_IMPORT,
                transfer.import_preview(preview, strategy),
                strategy.value,
            ),
            "overlay.import.total",
            operation_detail=strategy.value,
        )

    def destructive_maintenance_safety(self) -> RestoreSafetyResult:
        if self._maintenance is None:
            reason = RestoreSafetyReason(
                RestoreSafetyBlocker.STATE_UNAVAILABLE,
                "Datenbankwartung ist nicht eingerichtet.",
            )
            return RestoreSafetyResult(False, (reason,))
        return self._maintenance.destructive_safety()

    def _start_maintenance(self, operation: DatabaseMaintenanceOperation) -> bool:
        if self._maintenance is None:
            return False
        actions = {
            DatabaseMaintenanceOperation.QUICK_CHECK: self._maintenance.quick_check,
            DatabaseMaintenanceOperation.INTEGRITY_CHECK: self._maintenance.integrity_check,
            DatabaseMaintenanceOperation.ANALYZE: self._maintenance.analyze,
            DatabaseMaintenanceOperation.VACUUM: self._maintenance.vacuum,
            DatabaseMaintenanceOperation.REINDEX: self._maintenance.reindex,
        }
        return self._submit(
            BackupRestoreOperation.MAINTENANCE,
            lambda: self._maintenance_result(actions[operation]()),
            {
                DatabaseMaintenanceOperation.QUICK_CHECK: "database.quick_check",
                DatabaseMaintenanceOperation.INTEGRITY_CHECK: "database.integrity_check",
                DatabaseMaintenanceOperation.ANALYZE: "database.analyze",
                DatabaseMaintenanceOperation.VACUUM: "database.vacuum",
                DatabaseMaintenanceOperation.REINDEX: "database.reindex",
            }[operation],
            operation_detail=operation.value,
        )

    def _submit(
        self,
        operation: BackupRestoreOperation,
        work: Callable[[], BackupRestoreUiResult],
        metric: str,
        *,
        operation_detail: str,
        backup_target: str = "",
    ) -> bool:
        with self._lock:
            if self._closed:
                return False
            if self._active:
                self._publish(
                    BackupRestoreUiResult(
                        operation,
                        BackupRestoreUiState.BUSY,
                        "Eine Backup- oder Restore-Operation läuft bereits.",
                        error_code="BACKUP_RESTORE_BUSY",
                    )
                )
                return False
            self._active = True
        future = self._executor.submit(
            self._run_measured,
            operation,
            work,
            metric,
            operation_detail,
            backup_target,
        )
        future.add_done_callback(lambda done: self._finished(operation, done))
        return True

    def _finished(
        self, operation: BackupRestoreOperation, future: Future[BackupRestoreUiResult]
    ) -> None:
        try:
            result = future.result()
        except Exception as exc:
            result = BackupRestoreUiResult(
                operation,
                BackupRestoreUiState.FAILED,
                f"Die Operation ist unerwartet fehlgeschlagen: {exc}",
                error_code="BACKUP_RESTORE_UNEXPECTED_ERROR",
            )
        with self._lock:
            self._active = False
            self._last_result = result
            closed = self._closed
        if not closed:
            self._publish(result)

    def _run_measured(
        self,
        operation: BackupRestoreOperation,
        work: Callable[[], BackupRestoreUiResult],
        metric: str,
        operation_detail: str,
        backup_target: str,
    ) -> BackupRestoreUiResult:
        started_at = self._now().astimezone(timezone.utc)
        started = monotonic()
        self._logger.info(
            "Datenoperation gestartet",
            extra={
                "event": "data_operation_started",
                "operation_type": operation.value,
                "operation_detail": operation_detail,
                "started_at": started_at.isoformat(),
                "backup_target": backup_target or "none",
            },
        )
        try:
            result = work()
        except Exception as exc:
            result = BackupRestoreUiResult(
                operation,
                BackupRestoreUiState.FAILED,
                f"Die Operation ist unerwartet fehlgeschlagen: {exc}",
                error_code="BACKUP_RESTORE_UNEXPECTED_ERROR",
            )
        duration_ms = max(0.0, (monotonic() - started) * 1000.0)
        self._performance.record(metric, duration_ms, 1000.0)
        successful = result.state in {
            BackupRestoreUiState.COMPLETED,
            BackupRestoreUiState.RESTART_REQUIRED,
        }
        counter = {
            (BackupRestoreOperation.BACKUP, True): "backup_created_total",
            (BackupRestoreOperation.BACKUP, False): "backup_failed_total",
            (BackupRestoreOperation.RESTORE, True): "restore_completed_total",
            (BackupRestoreOperation.RESTORE, False): "restore_failed_total",
            (BackupRestoreOperation.MAINTENANCE, False): "database_maintenance_failed_total",
        }.get((result.operation, successful))
        if counter is not None:
            self._performance.increment_counter(counter)
        finished_at = self._now().astimezone(timezone.utc)
        self._logger.info(
            "Datenoperation abgeschlossen",
            extra={
                "event": "data_operation_completed",
                "operation_type": result.operation.value,
                "operation_detail": result.operation_detail or operation_detail,
                "started_at": started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "duration_ms": round(duration_ms, 3),
                "operation_result": result.state.value,
                "error_code": result.error_code or "none",
                "backup_target": result.backup_target or backup_target or "none",
                "schema_version": (
                    result.schema_version if result.schema_version is not None else "unknown"
                ),
            },
        )
        return result

    def _publish(self, result: BackupRestoreUiResult) -> None:
        self._schedule(0, lambda: self._completed(result))

    def _backup_result(self, result: BackupResult) -> BackupRestoreUiResult:
        created_at = result.manifest.created_at if result.manifest is not None else ""
        if result.success and result.backup_path is not None and created_at:
            if self._manual_backup_recorded is not None:
                self._manual_backup_recorded(created_at, str(result.backup_path))
            with self._lock:
                self._last_manual_backup = (created_at, str(result.backup_path))
        return BackupRestoreUiResult(
            BackupRestoreOperation.BACKUP,
            BackupRestoreUiState.COMPLETED if result.success else BackupRestoreUiState.FAILED,
            result.message,
            result.backup_path,
            result.error_code.value,
            created_at,
            operation_detail="CREATE",
            backup_target=str(result.backup_path.parent) if result.backup_path else "",
            schema_version=(
                result.manifest.database_schema_version if result.manifest is not None else None
            ),
        )

    @staticmethod
    def _restore_result(result: RestorePipelineResult) -> BackupRestoreUiResult:
        return BackupRestoreUiResult(
            BackupRestoreOperation.RESTORE,
            (
                BackupRestoreUiState.RESTART_REQUIRED
                if result.success
                else BackupRestoreUiState.FAILED
            ),
            result.message,
            result.safety_backup_path,
            result.error_code.value,
            operation_detail="RESTORE",
            schema_version=result.database_schema_version,
        )

    @staticmethod
    def _maintenance_result(result: DatabaseMaintenanceResult) -> BackupRestoreUiResult:
        details = ""
        if result.findings and result.findings != ("ok",):
            details = "\n\n" + "\n".join(result.findings[:10])
        return BackupRestoreUiResult(
            BackupRestoreOperation.MAINTENANCE,
            BackupRestoreUiState.COMPLETED if result.success else BackupRestoreUiState.FAILED,
            result.message + details,
            error_code=result.error_code.value,
            findings=result.findings,
            operation_detail=result.operation.value,
            schema_version=LATEST_SCHEMA_VERSION,
        )

    @staticmethod
    def _playlist_preview_result(preview: PlaylistImportPreview) -> BackupRestoreUiResult:
        findings = (
            f"Einträge: {preview.entry_count}",
            f"Duplikate: {preview.duplicate_count}",
            f"Unbekannte Pfade: {preview.unknown_path_count}",
            f"Namenskonflikt: {'ja' if preview.name_conflict else 'nein'}",
        )
        return BackupRestoreUiResult(
            BackupRestoreOperation.PLAYLIST_IMPORT_PREVIEW,
            BackupRestoreUiState.COMPLETED if preview.valid else BackupRestoreUiState.FAILED,
            preview.message,
            error_code=preview.error_code.value,
            findings=findings,
            operation_detail=preview.format.value,
            schema_version=LATEST_SCHEMA_VERSION,
            playlist_preview=preview,
        )

    @staticmethod
    def _playlist_transfer_result(
        operation: BackupRestoreOperation,
        result: PlaylistTransferResult,
        format: PlaylistTransferFormat,
    ) -> BackupRestoreUiResult:
        return BackupRestoreUiResult(
            operation,
            BackupRestoreUiState.COMPLETED if result.success else BackupRestoreUiState.FAILED,
            result.message,
            result.path,
            result.error_code.value,
            operation_detail=format.value,
            schema_version=LATEST_SCHEMA_VERSION,
        )

    @staticmethod
    def _media_path_preview_result(
        preview: MediaPathRemapPreview,
    ) -> BackupRestoreUiResult:
        findings = (
            f"Katalogtitel: {preview.track_count}",
            f"Overlays: {preview.overlay_count}",
            f"Notfallhistorie: {preview.emergency_history_count}",
            f"Kollisionen: {len(preview.collisions)}",
        )
        return BackupRestoreUiResult(
            BackupRestoreOperation.MEDIA_PATH_REMAP_PREVIEW,
            BackupRestoreUiState.COMPLETED if preview.valid else BackupRestoreUiState.FAILED,
            preview.message,
            error_code=preview.error_code.value,
            findings=findings,
            operation_detail="PREVIEW",
            schema_version=LATEST_SCHEMA_VERSION,
            media_path_preview=preview,
        )

    @staticmethod
    def _media_path_remap_result(result: MediaPathRemapResult) -> BackupRestoreUiResult:
        return BackupRestoreUiResult(
            BackupRestoreOperation.MEDIA_PATH_REMAP,
            (
                BackupRestoreUiState.RESTART_REQUIRED
                if result.success
                else BackupRestoreUiState.FAILED
            ),
            result.message,
            error_code=result.error_code.value,
            findings=(f"Geänderte Pfade: {result.affected_count}",),
            operation_detail="COMMIT",
            schema_version=LATEST_SCHEMA_VERSION,
        )

    @staticmethod
    def _equalizer_preview_result(preview: EqualizerImportPreview) -> BackupRestoreUiResult:
        preset = preview.preset
        findings = (
            f"Preset: {preset.name if preset is not None else 'unbekannt'}",
            f"Bänder: {len(preset.curve) if preset is not None else 0}",
            f"Konflikte: {len(preview.conflicts)}",
            f"Eingebauter Konflikt: {'ja' if preview.builtin_conflict else 'nein'}",
        )
        return BackupRestoreUiResult(
            BackupRestoreOperation.EQUALIZER_IMPORT_PREVIEW,
            BackupRestoreUiState.COMPLETED if preview.valid else BackupRestoreUiState.FAILED,
            preview.message,
            error_code=preview.error_code.value,
            findings=findings,
            operation_detail="JSON",
            schema_version=LATEST_SCHEMA_VERSION,
            equalizer_preview=preview,
        )

    @staticmethod
    def _equalizer_transfer_result(
        operation: BackupRestoreOperation,
        result: EqualizerTransferResult,
        detail: str,
    ) -> BackupRestoreUiResult:
        return BackupRestoreUiResult(
            operation,
            BackupRestoreUiState.COMPLETED if result.success else BackupRestoreUiState.FAILED,
            result.message,
            result.path,
            result.error_code.value,
            operation_detail=detail,
            schema_version=LATEST_SCHEMA_VERSION,
        )

    @staticmethod
    def _overlay_preview_result(preview: OverlayImportPreview) -> BackupRestoreUiResult:
        favorites = sum(record.favorite_position is not None for record in preview.records)
        return BackupRestoreUiResult(
            BackupRestoreOperation.OVERLAY_IMPORT_PREVIEW,
            BackupRestoreUiState.COMPLETED if preview.valid else BackupRestoreUiState.FAILED,
            preview.message,
            error_code=preview.error_code.value,
            findings=(
                f"Definitionen: {len(preview.records)}",
                f"Favoriten: {favorites}",
                f"Konflikte: {len(preview.conflicts)}",
            ),
            operation_detail="JSON",
            schema_version=LATEST_SCHEMA_VERSION,
            overlay_preview=preview,
        )

    @staticmethod
    def _overlay_transfer_result(
        operation: BackupRestoreOperation,
        result: OverlayTransferResult,
        detail: str,
    ) -> BackupRestoreUiResult:
        return BackupRestoreUiResult(
            operation,
            BackupRestoreUiState.COMPLETED if result.success else BackupRestoreUiState.FAILED,
            result.message,
            result.path,
            result.error_code.value,
            findings=(f"Importierte Definitionen: {result.imported_count}",),
            operation_detail=detail,
            schema_version=LATEST_SCHEMA_VERSION,
        )
