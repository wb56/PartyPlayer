from pathlib import Path
import sqlite3

import pytest
from _pytest.monkeypatch import MonkeyPatch

from party_player.database.connection import Database
from party_player.database_maintenance import (
    DatabaseMaintenanceErrorCode,
    DatabaseMaintenanceOperation,
    DatabaseMaintenanceService,
)
from party_player.backup_service import BackupService, validate_backup_archive
from party_player.restore_safety import RestoreSafetyGate, RestoreSafetySnapshot


def test_quick_and_full_integrity_checks_return_all_ok_findings(
    temporary_database: Database,
) -> None:
    service = DatabaseMaintenanceService(temporary_database.path)

    quick = service.quick_check()
    full = service.integrity_check()

    assert quick.success and quick.findings == ("ok",)
    assert quick.operation is DatabaseMaintenanceOperation.QUICK_CHECK
    assert full.success and full.findings == ("ok",)
    assert full.operation is DatabaseMaintenanceOperation.INTEGRITY_CHECK


def test_manual_analyze_updates_sqlite_statistics(temporary_database: Database) -> None:
    with temporary_database.connect() as connection:
        connection.execute("CREATE TABLE maintenance_probe (value TEXT)")
        connection.execute("CREATE INDEX maintenance_probe_value ON maintenance_probe(value)")
        connection.executemany(
            "INSERT INTO maintenance_probe VALUES (?)", ((str(value),) for value in range(20))
        )

    result = DatabaseMaintenanceService(temporary_database.path).analyze()

    assert result.success
    assert result.operation is DatabaseMaintenanceOperation.ANALYZE
    with sqlite3.connect(temporary_database.path) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_stat1 WHERE tbl = 'maintenance_probe'"
        ).fetchone() == (1,)


def test_missing_database_has_stable_maintenance_error(tmp_path: Path) -> None:
    service = DatabaseMaintenanceService(tmp_path / "missing.db")

    for result in (service.quick_check(), service.integrity_check(), service.analyze()):
        assert not result.success
        assert result.error_code is DatabaseMaintenanceErrorCode.DATABASE_MISSING


def test_integrity_check_preserves_every_sqlite_finding(
    temporary_database: Database, monkeypatch: MonkeyPatch
) -> None:
    class FindingConnection:
        def execute(self, _statement: str) -> tuple[tuple[str], ...]:
            return (("page 2 damaged",), ("index mismatch",))

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "party_player.database_maintenance.sqlite3.connect",
        lambda *_args, **_kwargs: FindingConnection(),
    )

    result = DatabaseMaintenanceService(temporary_database.path).integrity_check()

    assert not result.success
    assert result.error_code is DatabaseMaintenanceErrorCode.INTEGRITY_FINDINGS
    assert result.findings == ("page 2 damaged", "index mismatch")
    assert "2 Befund" in result.message


SAFE = RestoreSafetySnapshot(True, True, False, False, False, False, False, False, False)


@pytest.mark.parametrize("operation", ["vacuum", "reindex"])
def test_destructive_maintenance_is_twice_gated_backed_up_drained_and_resumed(
    temporary_database: Database, tmp_path: Path, operation: str
) -> None:
    gate_calls = 0
    lifecycle: list[str] = []

    def snapshot() -> RestoreSafetySnapshot:
        nonlocal gate_calls
        gate_calls += 1
        return SAFE

    service = DatabaseMaintenanceService(
        temporary_database.path,
        safety_gate=RestoreSafetyGate(snapshot),
        backup_service=BackupService(temporary_database),
        safety_backup_directory=tmp_path / "safety",
        quiesce=lambda: lifecycle.append("quiesce") or True,
        resume=lambda: lifecycle.append("resume") or True,
    )

    result = getattr(service, operation)()

    assert result.success
    assert gate_calls == 2
    assert lifecycle == ["quiesce", "resume"]
    assert result.safety_backup_path is not None
    assert result.safety_backup_path.name.startswith("deckrelay-safety-backup-")
    assert validate_backup_archive(result.safety_backup_path).valid


def test_destructive_maintenance_gate_blocks_before_safety_backup(
    temporary_database: Database, tmp_path: Path
) -> None:
    blocked = RestoreSafetySnapshot(False, True, True, False, False, False, False, False, False)
    lifecycle: list[str] = []
    service = DatabaseMaintenanceService(
        temporary_database.path,
        safety_gate=RestoreSafetyGate(lambda: blocked),
        backup_service=BackupService(temporary_database),
        safety_backup_directory=tmp_path / "safety",
        quiesce=lambda: lifecycle.append("quiesce") or True,
        resume=lambda: lifecycle.append("resume") or True,
    )

    result = service.vacuum()

    assert result.error_code is DatabaseMaintenanceErrorCode.SAFETY_GATE_BLOCKED
    assert len(result.findings) == 2
    assert not (tmp_path / "safety").exists()
    assert lifecycle == []


def test_destructive_maintenance_reports_resume_failure_after_operation(
    temporary_database: Database, tmp_path: Path
) -> None:
    service = DatabaseMaintenanceService(
        temporary_database.path,
        safety_gate=RestoreSafetyGate(lambda: SAFE),
        backup_service=BackupService(temporary_database),
        safety_backup_directory=tmp_path / "safety",
        quiesce=lambda: True,
        resume=lambda: False,
    )

    result = service.reindex()

    assert not result.success
    assert result.error_code is DatabaseMaintenanceErrorCode.RESUME_FAILED
    assert result.safety_backup_path is not None


def test_second_gate_can_block_after_safety_backup_without_lifecycle_mutation(
    temporary_database: Database, tmp_path: Path
) -> None:
    blocked = RestoreSafetySnapshot(True, True, False, True, False, False, False, False, False)
    snapshots = iter((SAFE, blocked))
    lifecycle: list[str] = []
    service = DatabaseMaintenanceService(
        temporary_database.path,
        safety_gate=RestoreSafetyGate(lambda: next(snapshots)),
        backup_service=BackupService(temporary_database),
        safety_backup_directory=tmp_path / "safety",
        quiesce=lambda: lifecycle.append("quiesce") or True,
        resume=lambda: lifecycle.append("resume") or True,
    )

    result = service.vacuum()

    assert result.error_code is DatabaseMaintenanceErrorCode.SAFETY_GATE_BLOCKED
    assert result.safety_backup_path is not None
    assert validate_backup_archive(result.safety_backup_path).valid
    assert lifecycle == []


def test_vacuum_rejects_insufficient_database_volume_space_before_backup(
    temporary_database: Database, tmp_path: Path
) -> None:
    lifecycle: list[str] = []
    service = DatabaseMaintenanceService(
        temporary_database.path,
        safety_gate=RestoreSafetyGate(lambda: SAFE),
        backup_service=BackupService(temporary_database),
        safety_backup_directory=tmp_path / "safety",
        quiesce=lambda: lifecycle.append("quiesce") or True,
        resume=lambda: lifecycle.append("resume") or True,
        free_space=lambda _path: 0,
    )

    result = service.vacuum()

    assert result.error_code is DatabaseMaintenanceErrorCode.INSUFFICIENT_SPACE
    assert not (tmp_path / "safety").exists()
    assert lifecycle == []


def test_destructive_safety_exposes_all_current_gate_reasons(
    temporary_database: Database,
) -> None:
    blocked = RestoreSafetySnapshot(False, False, True, True, False, False, False, False, False)
    service = DatabaseMaintenanceService(
        temporary_database.path,
        safety_gate=RestoreSafetyGate(lambda: blocked),
        backup_service=BackupService(temporary_database),
        safety_backup_directory=temporary_database.path.parent / "safety",
        quiesce=lambda: True,
        resume=lambda: True,
    )

    result = service.destructive_safety()

    assert not result.allowed
    assert len(result.reasons) == 4
