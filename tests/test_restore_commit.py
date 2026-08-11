from pathlib import Path
from zipfile import ZipFile

from _pytest.monkeypatch import MonkeyPatch

from party_player.backup_service import (
    DATABASE_ARCHIVE_PATH,
    BackupService,
    RestorePreparationService,
    RestorePreparationResult,
    RestoreValidator,
)
from party_player.database.connection import Database
from party_player.restore_commit import (
    RestoreCommitErrorCode,
    RestoreCommitService,
    _database_is_current_and_valid,
)


def _prepared_restore(database: Database, tmp_path: Path) -> tuple[RestorePreparationResult, Path]:
    candidate = BackupService(database).create_backup(tmp_path / "candidate")
    assert candidate.backup_path is not None
    preparation = RestorePreparationService(RestoreValidator(), BackupService(database)).prepare(
        candidate.backup_path, tmp_path / "safety"
    )
    assert preparation.success
    return preparation, candidate.backup_path


def _staged_database(candidate: Path, active: Path) -> Path:
    staged = active.with_name(".partyplayer.restore.tmp")
    with ZipFile(candidate) as archive:
        staged.write_bytes(archive.read(DATABASE_ARCHIVE_PATH))
    return staged


def test_atomic_commit_replaces_database_and_requires_restart(
    temporary_database: Database, tmp_path: Path
) -> None:
    preparation, candidate = _prepared_restore(temporary_database, tmp_path)
    staged = _staged_database(candidate, temporary_database.path)
    wal = Path(f"{temporary_database.path}-wal")
    shm = Path(f"{temporary_database.path}-shm")
    wal.write_bytes(b"old wal")
    shm.write_bytes(b"old shm")
    lifecycle_calls: list[str] = []

    def quiesce() -> bool:
        lifecycle_calls.append("quiesce")
        return True

    def resume() -> bool:
        lifecycle_calls.append("resume")
        return True

    service = RestoreCommitService(
        temporary_database.path,
        quiesce=quiesce,
        resume_after_rollback=resume,
    )

    result = service.commit(preparation, candidate, staged)

    assert result.success
    assert result.restart_required
    assert result.error_code is RestoreCommitErrorCode.NONE
    assert lifecycle_calls == ["quiesce"]
    assert _database_is_current_and_valid(temporary_database.path)
    assert not staged.exists()
    assert not wal.exists() or wal.read_bytes() != b"old wal"
    assert not shm.exists() or shm.read_bytes() != b"old shm"
    assert not list(temporary_database.path.parent.glob(".*.rollback*"))


def test_changed_candidate_is_rejected_before_lifecycle_gate(
    temporary_database: Database, tmp_path: Path
) -> None:
    preparation, candidate = _prepared_restore(temporary_database, tmp_path)
    staged = _staged_database(candidate, temporary_database.path)
    candidate.write_bytes(candidate.read_bytes() + b"changed")
    quiesced = False

    def quiesce() -> bool:
        nonlocal quiesced
        quiesced = True
        return True

    result = RestoreCommitService(
        temporary_database.path, quiesce=quiesce, resume_after_rollback=lambda: True
    ).commit(preparation, candidate, staged)

    assert result.error_code is RestoreCommitErrorCode.CANDIDATE_CHANGED
    assert not quiesced
    assert staged.exists()


def test_lifecycle_gate_blocks_exchange(temporary_database: Database, tmp_path: Path) -> None:
    preparation, candidate = _prepared_restore(temporary_database, tmp_path)
    staged = _staged_database(candidate, temporary_database.path)
    active_before = temporary_database.path.read_bytes()

    result = RestoreCommitService(
        temporary_database.path, quiesce=lambda: False, resume_after_rollback=lambda: True
    ).commit(preparation, candidate, staged)

    assert result.error_code is RestoreCommitErrorCode.LIFECYCLE_GATE_FAILED
    assert temporary_database.path.read_bytes() == active_before
    assert staged.exists()


def test_exchange_failure_restores_previous_database(
    temporary_database: Database, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    preparation, candidate = _prepared_restore(temporary_database, tmp_path)
    staged = _staged_database(candidate, temporary_database.path).resolve()
    active = temporary_database.path.resolve()
    active_before = active.read_bytes()
    resumed = False
    original_replace = __import__("os").replace

    def controlled_replace(source: Path, destination: Path) -> None:
        if Path(source).resolve() == staged and Path(destination).resolve() == active:
            raise PermissionError(13, "simulated exchange failure")
        original_replace(source, destination)

    def resume() -> bool:
        nonlocal resumed
        resumed = True
        return True

    monkeypatch.setattr("party_player.restore_commit.os.replace", controlled_replace)
    result = RestoreCommitService(
        active, quiesce=lambda: True, resume_after_rollback=resume
    ).commit(preparation, candidate, staged)

    assert result.error_code is RestoreCommitErrorCode.EXCHANGE_FAILED
    assert result.rollback_performed
    assert resumed
    assert active.read_bytes() == active_before
    assert _database_is_current_and_valid(active)


def test_failed_post_commit_validation_removes_candidate_sidecars_before_rollback(
    temporary_database: Database, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    preparation, candidate = _prepared_restore(temporary_database, tmp_path)
    staged = _staged_database(candidate, temporary_database.path)
    active = temporary_database.path.resolve()
    active_before = active.read_bytes()
    calls = 0

    def controlled_validation(path: Path) -> bool:
        nonlocal calls
        calls += 1
        if calls == 2:
            Path(f"{active}-wal").write_bytes(b"candidate wal")
            Path(f"{active}-shm").write_bytes(b"candidate shm")
            return False
        return True

    monkeypatch.setattr(
        "party_player.restore_commit._database_is_current_and_valid", controlled_validation
    )
    result = RestoreCommitService(
        active, quiesce=lambda: True, resume_after_rollback=lambda: True
    ).commit(preparation, candidate, staged)

    assert result.error_code is RestoreCommitErrorCode.EXCHANGE_FAILED
    assert result.rollback_performed
    assert active.read_bytes() == active_before
    assert not Path(f"{active}-wal").exists()
    assert not Path(f"{active}-shm").exists()
