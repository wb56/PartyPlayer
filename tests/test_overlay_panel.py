from party_player.ui.overlay_panel import OverlayPanel
from party_player.ui.overlay_presentation import (
    FavoritePadViewModel,
    OverlayState,
    OverlayViewModel,
)
from party_player.overlay import OverlayStatus
from party_player.ui.main_window import _overlay_favorite_reclick_fades


def test_status_tooltip_keeps_complete_names_and_error_details() -> None:
    model = OverlayViewModel(
        state=OverlayState.ERROR,
        selected_name="Sehr langer ausgewählter Jingle",
        active_name="Sehr langer aktiver Jingle",
        error_message="Datei fehlt",
    )

    assert OverlayPanel._status_tooltip_text(model) == (
        "Aktiv: Sehr langer aktiver Jingle\n"
        "Ausgewählt: Sehr langer ausgewählter Jingle\n"
        "Zustand: error\n"
        "Fehler: Datei fehlt"
    )


def test_ready_status_without_selection_is_explicit() -> None:
    assert OverlayPanel._status_text(OverlayViewModel()) == "Keine Auswahl"


def test_favorite_tooltip_contains_complete_metadata_and_safety_state() -> None:
    model = FavoritePadViewModel(
        name="Sehr langer vollständiger Applaus",
        category="Publikum",
        ducking_db=-8.0,
        shortcut="Ctrl+3",
        missing_file=True,
        enabled=False,
    )

    assert OverlayPanel._favorite_tooltip(3, model) == (
        "Sehr langer vollständiger Applaus\n"
        "Kategorie: Publikum\n"
        "Musikabsenkung: -8 dB\n"
        "Tastenkürzel: Ctrl+3\n"
        "Datei fehlt – in der Verwaltung neu zuweisen\n"
        "Jingle ist deaktiviert – in der Verwaltung aktivieren"
    )


def test_pressing_active_favorite_fades_only_during_active_playback() -> None:
    assert _overlay_favorite_reclick_fades(17, OverlayStatus.PLAYING, 17)
    assert _overlay_favorite_reclick_fades(17, OverlayStatus.PREPARING, 17)
    assert not _overlay_favorite_reclick_fades(17, OverlayStatus.FINISHED, 17)
    assert not _overlay_favorite_reclick_fades(17, OverlayStatus.PLAYING, 18)
