"""Structured, serialized SQLite checks and manual statistics maintenance."""

from contextlib import closing
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
import shutil
import sqlite3
from threading import Lock

from party_player.backup_service import (
    BackupOperationState,
    BackupPurpose,
    BackupService,
    validate_backup_archive,
)
from party_player.restore_safety import (
    RestoreSafetyBlocker,
    RestoreSafetyGate,
    RestoreSafetyReason,
    RestoreSafetyResult,
)


class DatabaseMaintenanceOperation(StrEnum):
    QUICK_CHECK = "QUICK_CHECK"
    INTEGRITY_CHECK = "INTEGRITY_CHECK"
    ANALYZE = "ANALYZE"
    VACUUM = "VACUUM"
    REINDEX = "REINDEX"


class DatabaseMaintenanceErrorCode(StrEnum):
    NONE = ""
    BUSY = "DATABASE_MAINTENANCE_BUSY"
    DATABASE_MISSING = "DATABASE_MAINTENANCE_DATABASE_MISSING"
    CHECK_FAILED = "DATABASE_MAINTENANCE_CHECK_FAILED"
    INTEGRITY_FINDINGS = "DATABASE_MAINTENANCE_INTEGRITY_FINDINGS"
    ANALYZE_FAILED = "DATABASE_MAINTENANCE_ANALYZE_FAILED"
    SAFETY_GATE_BLOCKED = "DATABASE_MAINTENANCE_SAFETY_GATE_BLOCKED"
    SAFETY_BACKUP_FAILED = "DATABASE_MAINTENANCE_SAFETY_BACKUP_FAILED"
    LIFECYCLE_FAILED = "DATABASE_MAINTENANCE_LIFECYCLE_FAILED"
    DESTRUCTIVE_OPERATION_FAILED = "DATABASE_MAINTENANCE_OPERATION_FAILED"
    RESUME_FAILED = "DATABASE_MAINTENANCE_RESUME_FAILED"
    INSUFFICIENT_SPACE = "DATABASE_MAINTENANCE_INSUFFICIENT_SPACE"


@dataclass(frozen=True, slots=True)
class DatabaseMaintenanceResult:
    success: bool
    state: BackupOperationState
    operation: DatabaseMaintenanceOperation
    error_code: DatabaseMaintenanceErrorCode
    message: str
    findings: tuple[str, ...] = ()
    safety_backup_path: Path | None = None


class DatabaseMaintenanceService:
    """Execute one explicit SQLite maintenance operation at a time."""

    def __init__(
        self,
        database_path: Path,
        *,
        safety_gate: RestoreSafetyGate | None = None,
        backup_service: BackupService | None = None,
        safety_backup_directory: Path | None = None,
        quiesce: Callable[[], bool] | None = None,
        resume: Callable[[], bool] | None = None,
        free_space: Callable[[Path], int] | None = None,
    ) -> None:
        self._database_path = database_path.resolve()
        self._lock = Lock()
        self._safety_gate = safety_gate
        self._backup_service = backup_service
        self._safety_backup_directory = safety_backup_directory
        self._quiesce = quiesce
        self._resume = resume
        self._free_space = free_space or (lambda path: shutil.disk_usage(path).free)

    def quick_check(self) -> DatabaseMaintenanceResult:
        return self._check(DatabaseMaintenanceOperation.QUICK_CHECK, "quick_check")

    def integrity_check(self) -> DatabaseMaintenanceResult:
        return self._check(DatabaseMaintenanceOperation.INTEGRITY_CHECK, "integrity_check")

    def analyze(self) -> DatabaseMaintenanceResult:
        operation = DatabaseMaintenanceOperation.ANALYZE
        if not self._lock.acquire(blocking=False):
            return self._busy(operation)
        try:
            if not self._database_path.is_file():
                return self._missing(operation)
            try:
                with closing(sqlite3.connect(self._database_path)) as connection:
                    connection.execute("ANALYZE")
                    connection.commit()
            except sqlite3.Error as exc:
                return DatabaseMaintenanceResult(
                    False,
                    BackupOperationState.FAILED,
                    operation,
                    DatabaseMaintenanceErrorCode.ANALYZE_FAILED,
                    f"SQLite-Statistiken konnten nicht aktualisiert werden ({type(exc).__name__}).",
                )
            return DatabaseMaintenanceResult(
                True,
                BackupOperationState.COMPLETED,
                operation,
                DatabaseMaintenanceErrorCode.NONE,
                "SQLite-Statistiken wurden erfolgreich aktualisiert.",
            )
        finally:
            self._lock.release()

    def vacuum(self) -> DatabaseMaintenanceResult:
        return self._destructive(DatabaseMaintenanceOperation.VACUUM, "VACUUM")

    def reindex(self) -> DatabaseMaintenanceResult:
        return self._destructive(DatabaseMaintenanceOperation.REINDEX, "REINDEX")

    def destructive_safety(self) -> RestoreSafetyResult:
        if (
            self._safety_gate is None
            or self._backup_service is None
            or self._safety_backup_directory is None
            or self._quiesce is None
            or self._resume is None
        ):
            reason = RestoreSafetyReason(
                RestoreSafetyBlocker.STATE_UNAVAILABLE,
                "Destruktive Wartung ist nicht vollständig abgesichert.",
            )
            return RestoreSafetyResult(False, (reason,))
        return self._safety_gate.evaluate()

    def _destructive(
        self, operation: DatabaseMaintenanceOperation, statement: str
    ) -> DatabaseMaintenanceResult:
        if not self._lock.acquire(blocking=False):
            return self._busy(operation)
        quiesced = False
        safety_path: Path | None = None
        try:
            if not self._database_path.is_file():
                return self._missing(operation)
            if operation is DatabaseMaintenanceOperation.VACUUM:
                required = max(10 * 1024 * 1024, self._database_path.stat().st_size * 2)
                try:
                    available = self._free_space(self._database_path.parent)
                except OSError:
                    available = -1
                if available < required:
                    return DatabaseMaintenanceResult(
                        False,
                        BackupOperationState.FAILED,
                        operation,
                        DatabaseMaintenanceErrorCode.INSUFFICIENT_SPACE,
                        "Für VACUUM steht neben der Datenbank nicht genügend freier Speicherplatz zur Verfügung.",
                    )
            if (
                self._safety_gate is None
                or self._backup_service is None
                or self._safety_backup_directory is None
                or self._quiesce is None
                or self._resume is None
            ):
                return DatabaseMaintenanceResult(
                    False,
                    BackupOperationState.FAILED,
                    operation,
                    DatabaseMaintenanceErrorCode.LIFECYCLE_FAILED,
                    "Destruktive Datenbankwartung ist nicht vollständig abgesichert.",
                )
            first_gate = self._safety_gate.evaluate()
            if not first_gate.allowed:
                return self._blocked(operation, first_gate.reasons)
            safety = self._backup_service.create_backup(
                self._safety_backup_directory, purpose=BackupPurpose.SAFETY
            )
            if (
                not safety.success
                or safety.backup_path is None
                or not validate_backup_archive(safety.backup_path).valid
            ):
                return DatabaseMaintenanceResult(
                    False,
                    BackupOperationState.FAILED,
                    operation,
                    DatabaseMaintenanceErrorCode.SAFETY_BACKUP_FAILED,
                    "Das obligatorische Safety-Backup ist fehlgeschlagen.",
                )
            safety_path = safety.backup_path
            second_gate = self._safety_gate.evaluate()
            if not second_gate.allowed:
                blocked = self._blocked(operation, second_gate.reasons)
                return DatabaseMaintenanceResult(
                    blocked.success,
                    blocked.state,
                    blocked.operation,
                    blocked.error_code,
                    blocked.message,
                    blocked.findings,
                    safety_path,
                )
            if not self._quiesce():
                return DatabaseMaintenanceResult(
                    False,
                    BackupOperationState.FAILED,
                    operation,
                    DatabaseMaintenanceErrorCode.LIFECYCLE_FAILED,
                    "Persistenz konnte für die Wartung nicht geleert werden.",
                    safety_backup_path=safety_path,
                )
            quiesced = True
            try:
                with closing(sqlite3.connect(self._database_path)) as connection:
                    connection.execute(statement)
                    connection.commit()
            except sqlite3.Error as exc:
                result = DatabaseMaintenanceResult(
                    False,
                    BackupOperationState.FAILED,
                    operation,
                    DatabaseMaintenanceErrorCode.DESTRUCTIVE_OPERATION_FAILED,
                    f"Datenbankwartung ist fehlgeschlagen ({type(exc).__name__}).",
                    safety_backup_path=safety_path,
                )
            else:
                result = DatabaseMaintenanceResult(
                    True,
                    BackupOperationState.COMPLETED,
                    operation,
                    DatabaseMaintenanceErrorCode.NONE,
                    f"{operation.value} wurde erfolgreich abgeschlossen.",
                    safety_backup_path=safety_path,
                )
            assert self._resume is not None
            resume_ok = self._resume()
            quiesced = False
            if not resume_ok:
                return DatabaseMaintenanceResult(
                    False,
                    BackupOperationState.FAILED,
                    operation,
                    DatabaseMaintenanceErrorCode.RESUME_FAILED,
                    "Persistenz konnte nach der Wartung nicht fortgesetzt werden.",
                    safety_backup_path=safety_path,
                )
            return result
        finally:
            if quiesced and self._resume is not None:
                self._resume()
            self._lock.release()

    @staticmethod
    def _blocked(
        operation: DatabaseMaintenanceOperation,
        reasons: tuple[RestoreSafetyReason, ...],
    ) -> DatabaseMaintenanceResult:
        messages = tuple(reason.message for reason in reasons)
        return DatabaseMaintenanceResult(
            False,
            BackupOperationState.FAILED,
            operation,
            DatabaseMaintenanceErrorCode.SAFETY_GATE_BLOCKED,
            "Wartung ist aus Sicherheitsgründen blockiert: " + " ".join(messages),
            messages,
        )

    def _check(
        self, operation: DatabaseMaintenanceOperation, pragma: str
    ) -> DatabaseMaintenanceResult:
        if not self._lock.acquire(blocking=False):
            return self._busy(operation)
        try:
            if not self._database_path.is_file():
                return self._missing(operation)
            try:
                uri = f"{self._database_path.as_uri()}?mode=ro"
                with closing(sqlite3.connect(uri, uri=True)) as connection:
                    findings = tuple(str(row[0]) for row in connection.execute(f"PRAGMA {pragma}"))
            except sqlite3.Error as exc:
                return DatabaseMaintenanceResult(
                    False,
                    BackupOperationState.FAILED,
                    operation,
                    DatabaseMaintenanceErrorCode.CHECK_FAILED,
                    f"SQLite-Prüfung konnte nicht ausgeführt werden ({type(exc).__name__}).",
                )
            if findings != ("ok",):
                return DatabaseMaintenanceResult(
                    False,
                    BackupOperationState.FAILED,
                    operation,
                    DatabaseMaintenanceErrorCode.INTEGRITY_FINDINGS,
                    f"SQLite-Prüfung meldet {len(findings)} Befund(e).",
                    findings,
                )
            return DatabaseMaintenanceResult(
                True,
                BackupOperationState.COMPLETED,
                operation,
                DatabaseMaintenanceErrorCode.NONE,
                "SQLite-Prüfung wurde ohne Befund abgeschlossen.",
                findings,
            )
        finally:
            self._lock.release()

    @staticmethod
    def _busy(operation: DatabaseMaintenanceOperation) -> DatabaseMaintenanceResult:
        return DatabaseMaintenanceResult(
            False,
            BackupOperationState.FAILED,
            operation,
            DatabaseMaintenanceErrorCode.BUSY,
            "Eine Datenbankwartung läuft bereits.",
        )

    @staticmethod
    def _missing(operation: DatabaseMaintenanceOperation) -> DatabaseMaintenanceResult:
        return DatabaseMaintenanceResult(
            False,
            BackupOperationState.FAILED,
            operation,
            DatabaseMaintenanceErrorCode.DATABASE_MISSING,
            "Die DeckRelay-Datenbank fehlt.",
        )
