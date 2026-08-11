"""Atomic internal and privacy-filtered system diagnostic exports."""

from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum, StrEnum
import json
from pathlib import Path
import re
from typing import Any

from party_player.system_diagnostic_service import SystemDiagnosticReport


class DiagnosticExportMode(StrEnum):
    INTERNAL = "internal"
    SUPPORT = "support"


_USER_PROFILE = re.compile(r"(?i)(?P<drive>[A-Z]:\\Users\\)(?P<user>[^\\/:*?\"<>|]+)")
_UNC_SERVER = re.compile(r"(?P<prefix>\\\\)(?P<server>[^\\\s,;:)\]}]+)")
_ENVIRONMENT_REFERENCE = re.compile(r"%[A-Za-z_][A-Za-z0-9_() -]*%")


def anonymize_diagnostic_text(value: str, *, anonymize_unc_server: bool = True) -> str:
    """Anonymize path components inside a diagnostic string, including errors."""
    sanitized = _USER_PROFILE.sub(lambda match: f"{match.group('drive')}<user>", value)
    if anonymize_unc_server:
        sanitized = _UNC_SERVER.sub(
            lambda match: f"{match.group('prefix')}<server>",
            sanitized,
        )
    return _ENVIRONMENT_REFERENCE.sub("<environment>", sanitized)


def diagnostic_payload(
    report: SystemDiagnosticReport,
    mode: DiagnosticExportMode,
    *,
    anonymize_unc_server: bool = True,
) -> dict[str, Any]:
    payload = _serialize(report)
    assert isinstance(payload, dict)
    if mode == DiagnosticExportMode.SUPPORT:
        payload = _sanitize_recursive(payload, anonymize_unc_server=anonymize_unc_server)
    return {
        "format": "partyplayer-system-diagnostic-v1",
        "export_mode": mode.value,
        "report": payload,
    }


class DiagnosticReportExporter:
    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def export(
        self,
        report: SystemDiagnosticReport,
        mode: DiagnosticExportMode,
    ) -> Path:
        self._directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
        target = self._directory / f"system-diagnostic-{mode.value}-{timestamp}.json"
        temporary = target.with_suffix(".json.tmp")
        payload = diagnostic_payload(report, mode)
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)
        return target


def _serialize(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _serialize(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _serialize(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_serialize(item) for item in value]
    return value


def _sanitize_recursive(value: Any, *, anonymize_unc_server: bool) -> Any:
    if isinstance(value, str):
        return anonymize_diagnostic_text(value, anonymize_unc_server=anonymize_unc_server)
    if isinstance(value, list):
        return [
            _sanitize_recursive(item, anonymize_unc_server=anonymize_unc_server) for item in value
        ]
    if isinstance(value, dict):
        return {
            key: _sanitize_recursive(item, anonymize_unc_server=anonymize_unc_server)
            for key, item in value.items()
        }
    return value
