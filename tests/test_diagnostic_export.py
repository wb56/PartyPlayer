import json
from pathlib import Path

from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.diagnostic_export import (
    DiagnosticExportMode,
    DiagnosticReportExporter,
    anonymize_diagnostic_text,
    diagnostic_payload,
)
from party_player.network_source_check import NetworkSourceProbeResult
from party_player.system_diagnostic_service import SystemDiagnosticService
from tests.test_system_diagnostic_service import dependency_resolution


def report_at(tmp_path: Path):
    database = Database(tmp_path / "export.db")
    migrate(database)
    report = SystemDiagnosticService(database, application_version="1.0.0").check(
        dependency_resolution(), full=True
    )
    object.__setattr__(
        report,
        "network_sources",
        (
            NetworkSourceProbeResult(
                r"\\private-nas\music",
                False,
                message=r"Fehler für C:\Users\SensitiveUser\Music und %USERPROFILE%",
            ),
        ),
    )
    return report


def test_component_based_anonymization_handles_paths_and_nested_messages() -> None:
    value = (
        r"C:\Users\Sensitive User\Music auf \\private-nas\music; "
        r"zweite Quelle \\backup-nas\share; %USERPROFILE%"
    )

    sanitized = anonymize_diagnostic_text(value)

    assert "Sensitive" not in sanitized
    assert "private-nas" not in sanitized
    assert "backup-nas" not in sanitized
    assert "USERPROFILE" not in sanitized
    assert r"C:\Users\<user>\Music" in sanitized
    assert r"\\<server>\music" in sanitized


def test_support_payload_is_recursive_but_internal_payload_is_unchanged(tmp_path: Path) -> None:
    report = report_at(tmp_path)

    internal = diagnostic_payload(report, DiagnosticExportMode.INTERNAL)
    support = diagnostic_payload(report, DiagnosticExportMode.SUPPORT)
    internal_text = json.dumps(internal)
    support_text = json.dumps(support)

    assert "SensitiveUser" in internal_text
    assert "private-nas" in internal_text
    assert "SensitiveUser" not in support_text
    assert "private-nas" not in support_text


def test_export_is_utf8_json_and_leaves_no_temporary_file(tmp_path: Path) -> None:
    exporter = DiagnosticReportExporter(tmp_path / "diagnostics")

    target = exporter.export(report_at(tmp_path), DiagnosticExportMode.SUPPORT)

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["format"] == "partyplayer-system-diagnostic-v1"
    assert payload["export_mode"] == "support"
    assert not target.with_suffix(".json.tmp").exists()
