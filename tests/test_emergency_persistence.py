from pathlib import Path

from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.emergency_persistence import (
    EmergencyIncidentRepository,
    EmergencyPersistenceService,
)
from party_player.emergency_state import (
    DeckHealth,
    EmergencyStateService,
    EmergencySystemState,
)


def repository(path: Path) -> tuple[Database, EmergencyIncidentRepository]:
    database = Database(path)
    migrate(database)
    return database, EmergencyIncidentRepository(database)


def test_incident_is_independent_from_session_audit_and_survives_resolution(
    tmp_path: Path,
) -> None:
    database, incidents = repository(tmp_path / "incidents.db")
    state = EmergencyStateService()
    state.set_deck_health("B", DeckHealth.FAILED, "Player hängt")
    state.transition(EmergencySystemState.WARNING, "Player hängt")
    state.transition(EmergencySystemState.DEGRADED, "Player hängt")

    incident_id = incidents.record(
        77,
        "AUDIO_TRANSITION_FAILED",
        {"incoming_deck_id": "B", "error_code": "PLAYBACK_LOST"},
        state.snapshot(),
        "usb-dac",
    )

    assert incident_id is not None
    unresolved = incidents.latest_unresolved(77)
    assert unresolved is not None
    assert unresolved.system_state == "DEGRADED"
    assert unresolved.deck_b_health == "FAILED"
    assert unresolved.audio_device_id == "usb-dac"
    assert unresolved.last_result["error_code"] == "PLAYBACK_LOST"
    with database.connect() as connection:
        assert (
            connection.execute("SELECT COUNT(*) AS n FROM session_audit_events").fetchone()["n"]
            == 0
        )

    state.transition(EmergencySystemState.RECOVERING, "Reparatur")
    incidents.record(77, "AUDIO_GLOBAL_RECOVERY", {"success": True}, state.snapshot())
    state.set_deck_health("B", DeckHealth.HEALTHY, "Repariert")
    state.transition(EmergencySystemState.NORMAL, "Wiederhergestellt")
    incidents.record(77, "EMERGENCY_STATE_CHANGED", {}, state.snapshot())

    assert incidents.latest_unresolved(77) is None
    with database.connect() as connection:
        stored = connection.execute(
            "SELECT status, resolved_at FROM emergency_incidents WHERE id = ?",
            (incident_id,),
        ).fetchone()
        event_count = connection.execute(
            "SELECT COUNT(*) AS n FROM emergency_incident_events WHERE incident_id = ?",
            (incident_id,),
        ).fetchone()
    assert stored is not None and stored["status"] == "RESOLVED"
    assert stored["resolved_at"] is not None
    assert event_count is not None and event_count["n"] == 3


def test_normal_state_without_open_incident_does_not_create_noise(tmp_path: Path) -> None:
    _database, incidents = repository(tmp_path / "normal.db")
    state = EmergencyStateService()

    assert incidents.record(1, "EMERGENCY_STATE_CHANGED", {}, state.snapshot()) is None
    assert incidents.latest_unresolved() is None


def test_bounded_service_flushes_background_writes_on_close(tmp_path: Path) -> None:
    database, incidents = repository(tmp_path / "async.db")
    state = EmergencyStateService()
    state.transition(EmergencySystemState.WARNING, "Audiowarnung")
    service = EmergencyPersistenceService(incidents)

    assert service.record(
        5,
        "EMERGENCY_STATE_CHANGED",
        {"state": "WARNING"},
        state.snapshot(),
    )
    service.close()

    restored = EmergencyIncidentRepository(database).latest_unresolved(5)
    assert restored is not None
    assert restored.last_event_code == "EMERGENCY_STATE_CHANGED"


def test_operator_review_closes_incident_without_rewriting_last_technical_state(
    tmp_path: Path,
) -> None:
    database, incidents = repository(tmp_path / "review.db")
    state = EmergencyStateService()
    state.transition(EmergencySystemState.WARNING, "Gerätewarnung")
    incident_id = incidents.record(9, "DEVICE_WARNING", {}, state.snapshot())
    assert incident_id is not None

    assert incidents.resolve_reviewed(incident_id, {"review": "OPERATOR_CONFIRMED"})

    assert incidents.latest_unresolved(9) is None
    with database.connect() as connection:
        incident = connection.execute(
            """SELECT status, system_state, last_event_code, last_result
               FROM emergency_incidents WHERE id = ?""",
            (incident_id,),
        ).fetchone()
        event = connection.execute(
            """SELECT event_code, system_state FROM emergency_incident_events
               WHERE incident_id = ? ORDER BY id DESC LIMIT 1""",
            (incident_id,),
        ).fetchone()
    assert incident is not None
    assert incident["status"] == "RESOLVED"
    assert incident["system_state"] == "WARNING"
    assert incident["last_event_code"] == "INCIDENT_REVIEWED"
    assert "OPERATOR_CONFIRMED" in incident["last_result"]
    assert event is not None
    assert event["event_code"] == "INCIDENT_REVIEWED"
    assert event["system_state"] == "WARNING"
