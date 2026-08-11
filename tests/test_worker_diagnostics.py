from threading import current_thread
from time import monotonic

from party_player.worker_diagnostics import WorkerInfo, WorkerRegistry, collect_thread_snapshot


def test_thread_snapshot_is_read_only_and_contains_current_thread() -> None:
    before = current_thread().is_alive()
    snapshots = collect_thread_snapshot()
    assert any(item.name == current_thread().name for item in snapshots)
    assert current_thread().is_alive() == before


def test_worker_registry_removes_finished_worker() -> None:
    registry = WorkerRegistry()
    worker = WorkerInfo("1", "cover-deck-A", "cover", monotonic(), True, "cover-1")
    registry.started(worker)
    assert registry.active() == (worker,)
    registry.finished(worker.worker_id)
    assert registry.active() == ()
    assert registry.history()[0].name == "cover-deck-A"
    assert registry.history()[0].state == "completed"


def test_disabled_worker_registry_keeps_no_diagnostic_history() -> None:
    registry = WorkerRegistry(enabled=False)
    worker = WorkerInfo("1", "preload-deck-A", "preload", monotonic(), True, "op")
    registry.started(worker)
    registry.finished(worker.worker_id)
    assert registry.active() == ()
    assert registry.history() == ()
