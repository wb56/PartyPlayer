from pathlib import Path

from party_player.bounded_executor import BoundedThreadPoolExecutor
from party_player.database.connection import Database
from party_player.persistence_participant import single_worker_participant
from party_player.restore_runtime import build_restore_runtime


def test_restore_runtime_rejects_missing_required_participant(tmp_path: Path) -> None:
    database = Database(tmp_path / "active.db")

    runtime = build_restore_runtime(
        database.path,
        database,
        (None,),
        expected_participant_count=1,
    )

    assert not runtime.available
    assert runtime.pipeline is None
    assert "blockiert" in runtime.reason


def test_restore_runtime_binds_complete_lifecycle_to_pipeline(tmp_path: Path) -> None:
    database = Database(tmp_path / "active.db")
    executor = BoundedThreadPoolExecutor(
        max_workers=1, maximum_pending=1, thread_name_prefix="runtime-participant"
    )
    participant = single_worker_participant("runtime-owner", executor)

    runtime = build_restore_runtime(
        database.path,
        database,
        (participant,),
        expected_participant_count=1,
    )

    assert runtime.available
    assert runtime.pipeline is not None
    assert runtime.lifecycle is not None
    assert runtime.lifecycle.quiesce()
    assert runtime.lifecycle.resume_after_rollback()
    executor.shutdown()
