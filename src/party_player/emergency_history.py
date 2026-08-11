"""Queue-independent asynchronous history for confirmed emergency playback."""

from concurrent.futures import Future
from dataclasses import dataclass
import logging
import sqlite3

from party_player.bounded_executor import BoundedThreadPoolExecutor
from party_player.database.connection import Database
from party_player.persistence_participant import single_worker_participant
from party_player.restore_lifecycle import PersistenceParticipant


@dataclass(frozen=True, slots=True)
class EmergencyHistoryEntry:
    session_id: int | None
    track_id: int
    deck_id: str
    media_type: str
    title: str
    file_path: str
    cue_in: float
    effective_gain_db: float
    clip_protection_enabled: bool
    source: str = "EMERGENCY"


class EmergencyHistoryRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def record_started(self, entry: EmergencyHistoryEntry) -> None:
        with self._database.connect() as connection:
            connection.execute(
                """INSERT INTO emergency_play_history
                   (session_id, track_id, deck_id, media_type, source, title, file_path,
                    cue_in, effective_gain_db, clip_protection_enabled)
                   VALUES (?, ?, ?, ?, 'EMERGENCY', ?, ?, ?, ?, ?)""",
                (
                    entry.session_id,
                    entry.track_id,
                    entry.deck_id,
                    entry.media_type,
                    entry.title,
                    entry.file_path,
                    entry.cue_in,
                    entry.effective_gain_db,
                    int(entry.clip_protection_enabled),
                ),
            )


class EmergencyHistoryService:
    """Queue history writes without delaying or deciding emergency playback."""

    def __init__(self, repository: EmergencyHistoryRepository) -> None:
        self._repository = repository
        self._executor = BoundedThreadPoolExecutor(
            max_workers=1,
            maximum_pending=64,
            thread_name_prefix="emergency-history",
        )
        self._logger = logging.getLogger(__name__)

    def record_started(self, entry: EmergencyHistoryEntry) -> bool:
        try:
            future = self._executor.submit(self._repository.record_started, entry)
        except RuntimeError:
            self._logger.error("Notfall-History-Warteschlange ist voll")
            return False

        def log_failure(completed: Future[object]) -> None:
            try:
                completed.result()
            except (sqlite3.Error, RuntimeError, ValueError):
                self._logger.exception("Notfallwiedergabe konnte nicht gespeichert werden")

        future.add_done_callback(log_failure)
        return True

    def close(self) -> None:
        self._executor.shutdown(wait=True)

    def restore_participant(self) -> PersistenceParticipant:
        return single_worker_participant("emergency-history", self._executor)
