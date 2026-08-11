from threading import Event
import sqlite3
from pathlib import Path
from time import sleep

import pytest

from party_player.bounded_executor import BoundedThreadPoolExecutor
from party_player.database.connection import Database
from party_player.persistence_participant import single_worker_database_participant


def test_executor_rejects_work_beyond_fixed_capacity() -> None:
    release = Event()
    executor = BoundedThreadPoolExecutor(
        max_workers=1, maximum_pending=2, thread_name_prefix="bounded-test"
    )
    first = executor.submit(lambda: release.wait(1.0))
    second = executor.submit(lambda: None)

    with pytest.raises(RuntimeError, match="Kapazität"):
        executor.submit(lambda: None)

    release.set()
    first.result(1.0)
    second.result(1.0)
    executor.shutdown()


def test_persistence_participant_closes_worker_owned_connection(tmp_path: Path) -> None:
    database = Database(tmp_path / "worker-cache.db")
    executor = BoundedThreadPoolExecutor(
        max_workers=1, maximum_pending=2, thread_name_prefix="persistence-test"
    )
    worker_connection: list[sqlite3.Connection] = []

    def open_cached_connection() -> None:
        with database.connect_cached() as connection:
            connection.execute("CREATE TABLE probe (id INTEGER)")
            worker_connection.append(connection)

    executor.submit(open_cached_connection).result()
    participant = single_worker_database_participant("test", executor, database)

    assert participant.block_new_work()
    assert participant.drain(1.0)
    assert participant.close_connections()
    with pytest.raises(sqlite3.ProgrammingError):
        worker_connection[0].execute("SELECT 1")
    assert participant.resume()
    assert executor.submit(lambda: 42).result() == 42
    executor.shutdown()


def test_timed_out_owner_finalizer_keeps_executor_blocked_until_it_finishes() -> None:
    executor = BoundedThreadPoolExecutor(
        max_workers=1, maximum_pending=1, thread_name_prefix="finalizer-timeout"
    )
    release = Event()
    assert executor.block_new_work()

    assert not executor.run_owner_finalizer(lambda: release.wait(1.0), timeout=0.1)
    assert not executor.resume_new_work()
    release.set()
    assert executor.drain(1.0)
    for _attempt in range(100):
        if executor.resume_new_work():
            break
        sleep(0.01)
    else:
        pytest.fail("Owner-Finalizer wurde nicht abgeschlossen")
    assert executor.submit(lambda: True).result()
    executor.shutdown()
