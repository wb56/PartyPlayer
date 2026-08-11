from pathlib import Path

from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.system_diagnostic_service import SystemDiagnosticService
from tests.test_system_diagnostic_service import dependency_resolution
from party_player.ui.system_diagnostic_dialog import format_system_report


def test_formatted_report_contains_dependencies_database_and_audio(tmp_path: Path) -> None:
    database = Database(tmp_path / "dialog-report.db")
    migrate(database)
    report = SystemDiagnosticService(
        database,
        application_version="1.0.0",
    ).check(dependency_resolution(), full=True)

    text = format_system_report(report)

    assert "DeckRelay: 1.0.0" in text
    assert "VLC / libVLC: available" in text
    assert "FFmpeg: not_found" in text
    assert "SQLite: available" in text
    assert "quick_check: ok" in text
    assert "Audiogeräte: not_checked" in text
