"""SQLite connection factory."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import Lock, local
from typing import Iterator


class Database:
    """Create configured SQLite connections."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._wal_initialized = False
        self._initialization_lock = Lock()
        self._local = local()

    def _open_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA synchronous = NORMAL")
        if not self._wal_initialized:
            with self._initialization_lock:
                if not self._wal_initialized:
                    connection.execute("PRAGMA journal_mode = WAL")
                    self._wal_initialized = True
        return connection

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection with safe defaults and row access by name."""
        active = getattr(self._local, "transaction_connection", None)
        if active is not None:
            yield active
            return
        connection = self._open_connection()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def connect_cached(self) -> Iterator[sqlite3.Connection]:
        """Reuse one connection on the current thread for latency-critical writes."""
        active = getattr(self._local, "transaction_connection", None)
        if active is not None:
            yield active
            return
        connection = getattr(self._local, "cached_connection", None)
        if connection is None:
            connection = self._open_connection()
            self._local.cached_connection = connection
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def close_cached_connection(self) -> bool:
        """Close only the cached connection owned by the calling thread."""
        if getattr(self._local, "transaction_connection", None) is not None:
            return False
        connection = getattr(self._local, "cached_connection", None)
        if connection is None:
            return True
        try:
            connection.close()
        except sqlite3.Error:
            return False
        del self._local.cached_connection
        return True

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Reuse one configured connection for a group of repository operations."""
        active = getattr(self._local, "transaction_connection", None)
        if active is not None:
            yield active
            return
        with self.connect() as connection:
            self._local.transaction_connection = connection
            try:
                yield connection
            finally:
                self._local.transaction_connection = None
