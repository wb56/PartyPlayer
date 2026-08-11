from party_player.backup_restore_controller import (
    BackupRestoreOperation,
    BackupRestoreUiResult,
    BackupRestoreUiState,
)
from party_player.ui.database_backup_dialog import (
    DatabaseBackupDialog,
    DatabaseBackupDialogState,
)
from party_player.restore_safety import (
    RestoreSafetyBlocker,
    RestoreSafetyReason,
    RestoreSafetyResult,
)
from party_player.playlist_transfer import PlaylistConflictStrategy
from party_player.ui.database_backup_dialog import PlaylistConflictDialog


class WidgetDouble:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def configure(self, **values: str) -> None:
        self.values.update(values)


def test_dialog_close_invalidates_owner_before_destroy() -> None:
    dialog = object.__new__(DatabaseBackupDialog)
    events: list[str] = []
    dialog._on_close = lambda: events.append("invalidated")
    dialog.destroy = lambda: events.append("destroyed")

    dialog._close()

    assert events == ["invalidated", "destroyed"]


def test_playlist_conflict_choice_is_explicit_and_cancelable() -> None:
    dialog = object.__new__(PlaylistConflictDialog)
    destroyed: list[bool] = []
    dialog.destroy = lambda: destroyed.append(True)

    dialog._choose(PlaylistConflictStrategy.APPEND)

    assert dialog.result is PlaylistConflictStrategy.APPEND
    assert destroyed == [True]
    destroyed.clear()
    dialog._cancel()
    assert dialog.result is None
    assert destroyed == [True]


def test_dialog_disables_every_action_while_operation_runs() -> None:
    dialog = object.__new__(DatabaseBackupDialog)
    buttons = [WidgetDouble(), WidgetDouble(), WidgetDouble()]
    status = WidgetDouble()
    dialog._state = DatabaseBackupDialogState()
    dialog._buttons = buttons
    dialog._danger_buttons = []
    dialog._status = status
    dialog._safety_status = WidgetDouble()
    dialog._safety = lambda: RestoreSafetyResult(True, ())

    dialog._start("Schnellprüfung", lambda: True)

    assert dialog._state.busy
    assert all(button.values["state"] == "disabled" for button in buttons)
    assert status.values["text"] == "Läuft: Schnellprüfung"


def test_dialog_ignores_double_click_and_reenables_after_result() -> None:
    dialog = object.__new__(DatabaseBackupDialog)
    button = WidgetDouble()
    status = WidgetDouble()
    dialog._state = DatabaseBackupDialogState(True, "Läuft")
    dialog._buttons = [button]
    dialog._danger_buttons = []
    dialog._status = status
    dialog._safety_status = WidgetDouble()
    dialog._safety = lambda: RestoreSafetyResult(True, ())
    calls: list[bool] = []

    dialog._start("ANALYZE", lambda: calls.append(True) or True)
    dialog.complete(
        BackupRestoreUiResult(
            BackupRestoreOperation.MAINTENANCE,
            BackupRestoreUiState.COMPLETED,
            "Fertig.",
        )
    )

    assert calls == []
    assert not dialog._state.busy
    assert button.values["state"] == "normal"
    assert status.values["text"] == "Fertig."


def test_dialog_disables_only_danger_actions_and_lists_all_safety_reasons() -> None:
    dialog = object.__new__(DatabaseBackupDialog)
    safe_button = WidgetDouble()
    vacuum = WidgetDouble()
    reindex = WidgetDouble()
    safety_status = WidgetDouble()
    blocked = RestoreSafetyResult(
        False,
        (
            RestoreSafetyReason(
                RestoreSafetyBlocker.DECK_A_NOT_STOPPED, "Deck A ist nicht gestoppt."
            ),
            RestoreSafetyReason(RestoreSafetyBlocker.OVERLAY_ACTIVE, "Ein Overlay ist aktiv."),
        ),
    )
    dialog._state = DatabaseBackupDialogState()
    dialog._buttons = [safe_button, vacuum, reindex]
    dialog._danger_buttons = [vacuum, reindex]
    dialog._status = WidgetDouble()
    dialog._safety_status = safety_status
    dialog._safety = lambda: blocked

    dialog._render_state()

    assert safe_button.values["state"] == "normal"
    assert vacuum.values["state"] == "disabled"
    assert reindex.values["state"] == "disabled"
    assert "Deck A ist nicht gestoppt" in safety_status.values["text"]
    assert "Overlay" in safety_status.values["text"]
