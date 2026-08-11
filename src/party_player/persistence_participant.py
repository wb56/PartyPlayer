"""Adapters for persistence executors participating in an atomic restore."""

from collections.abc import Callable

from party_player.bounded_executor import BoundedThreadPoolExecutor
from party_player.database.connection import Database
from party_player.restore_lifecycle import PersistenceParticipant


def single_worker_participant(
    name: str,
    executor: BoundedThreadPoolExecutor,
    close_connections: Callable[[], bool] = lambda: True,
    *,
    close_timeout_seconds: float = 2.0,
) -> PersistenceParticipant:
    """Bind one single-worker executor to an owner-thread finalizer."""
    return PersistenceParticipant(
        name=name,
        block_new_work=executor.block_new_work,
        drain=executor.drain,
        close_connections=lambda: executor.run_owner_finalizer(
            close_connections, timeout=close_timeout_seconds
        ),
        resume=executor.resume_new_work,
    )


def single_worker_database_participant(
    name: str,
    executor: BoundedThreadPoolExecutor,
    database: Database,
    *,
    close_timeout_seconds: float = 2.0,
) -> PersistenceParticipant:
    """Bind one single-worker executor to its thread-local SQLite cache lifecycle."""
    return single_worker_participant(
        name,
        executor,
        database.close_cached_connection,
        close_timeout_seconds=close_timeout_seconds,
    )
