"""Independent, bounded persistence for unresolved audio emergency incidents."""

from dataclasses import dataclass
from concurrent.futures import Future
import json
import logging
import sqlite3

from party_player.bounded_executor import BoundedThreadPoolExecutor
from party_player.database.connection import Database
from party_player.emergency_state import EmergencyStateSnapshot, EmergencySystemState
from party_player.persistence_participant import single_worker_participant
from party_player.restore_lifecycle import PersistenceParticipant


@dataclass(frozen=True, slots=True)
class EmergencyIncident:
    incident_id: int
    session_id: int | None
    status: str
    system_state: str
    reason: str
    deck_a_health: str
    deck_b_health: str
    audio_device_id: str
    last_event_code: str
    last_result: dict[str, object]
    started_at: str
    updated_at: str
    resolved_at: str | None


class EmergencyIncidentRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def record(
        self,
        session_id: int | None,
        event_code: str,
        details: dict[str, object],
        snapshot: EmergencyStateSnapshot,
        audio_device_id: str = "",
    ) -> int | None:
        """Append to the current incident, opening or resolving it as needed."""
        with self._database.transaction() as connection:
            row = connection.execute(
                """SELECT id FROM emergency_incidents
                   WHERE status = 'ACTIVE'
                     AND (session_id = ? OR (session_id IS NULL AND ? IS NULL))
                   ORDER BY id DESC LIMIT 1""",
                (session_id, session_id),
            ).fetchone()
            incident_id = int(row["id"]) if row is not None else None
            if incident_id is None and snapshot.system == EmergencySystemState.NORMAL:
                return None
            payload = json.dumps(details, ensure_ascii=False, sort_keys=True)
            if incident_id is None:
                cursor = connection.execute(
                    """INSERT INTO emergency_incidents
                       (session_id, system_state, reason, deck_a_health, deck_b_health,
                        audio_device_id, last_event_code, last_result)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        session_id,
                        snapshot.system.value,
                        snapshot.reason,
                        snapshot.deck_a.value,
                        snapshot.deck_b.value,
                        audio_device_id.strip(),
                        event_code,
                        payload,
                    ),
                )
                incident_id = int(cursor.lastrowid or 0)
            else:
                resolved = snapshot.system == EmergencySystemState.NORMAL
                connection.execute(
                    """UPDATE emergency_incidents
                       SET status = ?, system_state = ?, reason = ?, deck_a_health = ?,
                           deck_b_health = ?, audio_device_id = ?, last_event_code = ?,
                           last_result = ?, updated_at = CURRENT_TIMESTAMP,
                           resolved_at = CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE resolved_at END
                       WHERE id = ?""",
                    (
                        "RESOLVED" if resolved else "ACTIVE",
                        snapshot.system.value,
                        snapshot.reason,
                        snapshot.deck_a.value,
                        snapshot.deck_b.value,
                        audio_device_id.strip(),
                        event_code,
                        payload,
                        resolved,
                        incident_id,
                    ),
                )
            connection.execute(
                """INSERT INTO emergency_incident_events
                   (incident_id, session_id, event_code, system_state, details)
                   VALUES (?, ?, ?, ?, ?)""",
                (incident_id, session_id, event_code, snapshot.system.value, payload),
            )
            return incident_id

    def latest_unresolved(self, session_id: int | None = None) -> EmergencyIncident | None:
        query = "SELECT * FROM emergency_incidents WHERE status = 'ACTIVE'"
        parameters: tuple[object, ...] = ()
        if session_id is not None:
            query += " AND session_id = ?"
            parameters = (session_id,)
        query += " ORDER BY updated_at DESC, id DESC LIMIT 1"
        with self._database.connect() as connection:
            row = connection.execute(query, parameters).fetchone()
        if row is None:
            return None
        return EmergencyIncident(
            int(row["id"]),
            int(row["session_id"]) if row["session_id"] is not None else None,
            str(row["status"]),
            str(row["system_state"]),
            str(row["reason"]),
            str(row["deck_a_health"]),
            str(row["deck_b_health"]),
            str(row["audio_device_id"]),
            str(row["last_event_code"]),
            json.loads(str(row["last_result"])),
            str(row["started_at"]),
            str(row["updated_at"]),
            str(row["resolved_at"]) if row["resolved_at"] is not None else None,
        )

    def resolve_reviewed(self, incident_id: int, details: dict[str, object] | None = None) -> bool:
        """Close one historical incident as reviewed without rewriting its last state."""
        payload = json.dumps(details or {}, ensure_ascii=False, sort_keys=True)
        with self._database.transaction() as connection:
            row = connection.execute(
                """SELECT session_id, system_state FROM emergency_incidents
                   WHERE id = ? AND status = 'ACTIVE'""",
                (incident_id,),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                """UPDATE emergency_incidents
                   SET status = 'RESOLVED', last_event_code = 'INCIDENT_REVIEWED',
                       last_result = ?, updated_at = CURRENT_TIMESTAMP,
                       resolved_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (payload, incident_id),
            )
            connection.execute(
                """INSERT INTO emergency_incident_events
                   (incident_id, session_id, event_code, system_state, details)
                   VALUES (?, ?, 'INCIDENT_REVIEWED', ?, ?)""",
                (incident_id, row["session_id"], row["system_state"], payload),
            )
        return True


class EmergencyPersistenceService:
    """Serialize incident writes off the caller thread with bounded backlog."""

    def __init__(self, repository: EmergencyIncidentRepository) -> None:
        self._repository = repository
        self._executor = BoundedThreadPoolExecutor(
            max_workers=1,
            maximum_pending=128,
            thread_name_prefix="emergency-persistence",
        )
        self._logger = logging.getLogger(__name__)

    def record(
        self,
        session_id: int | None,
        event_code: str,
        details: dict[str, object],
        snapshot: EmergencyStateSnapshot,
        audio_device_id: str = "",
    ) -> bool:
        try:
            future = self._executor.submit(
                self._repository.record,
                session_id,
                event_code,
                dict(details),
                snapshot,
                audio_device_id,
            )
        except RuntimeError:
            self._logger.error("Notfallpersistenz-Warteschlange ist voll")
            return False

        def log_failure(completed: Future[object]) -> None:
            try:
                completed.result()
            except (sqlite3.Error, RuntimeError, ValueError):
                self._logger.exception("Notfallereignis konnte nicht gespeichert werden")

        future.add_done_callback(log_failure)
        return True

    def close(self) -> None:
        self._executor.shutdown(wait=True)

    def restore_participant(self) -> PersistenceParticipant:
        return single_worker_participant("emergency-persistence", self._executor)

    def resolve_reviewed(self, incident_id: int, details: dict[str, object] | None = None) -> bool:
        """Queue an explicit operator review without blocking the GUI thread."""
        try:
            future = self._executor.submit(
                self._repository.resolve_reviewed,
                incident_id,
                dict(details or {}),
            )
        except RuntimeError:
            self._logger.error("Incident-Prüfung konnte nicht eingeplant werden")
            return False

        def log_failure(completed: Future[object]) -> None:
            try:
                completed.result()
            except (sqlite3.Error, RuntimeError, ValueError):
                self._logger.exception("Incident-Prüfung konnte nicht gespeichert werden")

        future.add_done_callback(log_failure)
        return True
