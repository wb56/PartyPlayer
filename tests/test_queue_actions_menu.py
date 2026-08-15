from party_player.ui.main_window import MainWindow


def test_queue_action_toggle_labels_expose_current_state() -> None:
    assert MainWindow._queue_toggle_menu_labels(True, False) == (
        "Duplikate erlauben: aktiv",
        "Cue-Restlaufzeit: inaktiv",
    )
    assert MainWindow._queue_toggle_menu_labels(False, True) == (
        "Duplikate erlauben: inaktiv",
        "Cue-Restlaufzeit: aktiv",
    )
