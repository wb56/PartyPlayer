"""Presentation tests for active versus next-start dependency configuration."""

from party_player.ui.external_programs_dialog import format_runtime_program_status


def test_runtime_program_status_keeps_active_and_next_start_distinct() -> None:
    text = format_runtime_program_status(
        selection_mode="USER",
        active_status="available",
        active_source="standard",
        active_path=r"C:\Program Files\VideoLAN\VLC",
        active_version="3.0.21",
        next_status="available",
        next_source="user",
        next_path=r"D:\Tools\VLC",
        next_version="3.0.23",
    )

    assert "Aktiv in dieser Sitzung:" in text
    assert r"C:\Program Files\VideoLAN\VLC" in text
    assert "Für nächsten Start (USER):" in text
    assert r"D:\Tools\VLC" in text
    assert text.index(r"C:\Program Files") < text.index(r"D:\Tools")


def test_runtime_program_status_has_explicit_missing_values() -> None:
    text = format_runtime_program_status(
        selection_mode="AUTO",
        active_status="not_found",
        active_source=None,
        active_path=None,
        active_version=None,
        next_status="not_found",
        next_source=None,
        next_path=None,
        next_version=None,
    )

    assert "unbekannt" in text
    assert text.count("Pfad: —") == 2
