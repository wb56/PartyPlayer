"""Versioned equalizer preset export, preview, and atomic import."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import json
import os
from pathlib import Path

from party_player.equalizer import EqualizerPreset, EqualizerService
from party_player.repositories.equalizer_repository import EqualizerPresetRepository


EQUALIZER_FORMAT_VERSION = 1
MAX_EQUALIZER_FILE_BYTES = 256 * 1024


class EqualizerConflictStrategy(StrEnum):
    ERROR = "ERROR"
    SKIP = "SKIP"
    REPLACE = "REPLACE"
    COPY = "COPY"


class EqualizerTransferErrorCode(StrEnum):
    NONE = ""
    PRESET_NOT_FOUND = "EQUALIZER_PRESET_NOT_FOUND"
    FORMAT_INVALID = "EQUALIZER_FORMAT_INVALID"
    VERSION_UNSUPPORTED = "EQUALIZER_VERSION_UNSUPPORTED"
    NAME_CONFLICT = "EQUALIZER_NAME_CONFLICT"
    BUILTIN_CONFLICT = "EQUALIZER_BUILTIN_CONFLICT"
    SOURCE_CHANGED = "EQUALIZER_SOURCE_CHANGED"
    IO_FAILED = "EQUALIZER_IO_FAILED"


@dataclass(frozen=True, slots=True)
class EqualizerTransferResult:
    success: bool
    error_code: EqualizerTransferErrorCode
    message: str
    preset: EqualizerPreset | None = None
    path: Path | None = None
    skipped: bool = False


@dataclass(frozen=True, slots=True)
class EqualizerImportPreview:
    valid: bool
    error_code: EqualizerTransferErrorCode
    message: str
    source: Path
    source_sha256: str = ""
    preset: EqualizerPreset | None = None
    conflicts: tuple[tuple[int, str, str, bool], ...] = ()
    state_token: str = ""

    @property
    def has_conflict(self) -> bool:
        return bool(self.conflicts)

    @property
    def builtin_conflict(self) -> bool:
        return any(item[3] for item in self.conflicts)


class EqualizerTransferService:
    def __init__(
        self,
        repository: EqualizerPresetRepository,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._validator = EqualizerService()
        self._now = now or (lambda: datetime.now(timezone.utc))

    def export(self, preset_key: str, destination: Path) -> EqualizerTransferResult:
        preset = self._repository.get_by_key(preset_key)
        if preset is None:
            return self._failure(
                EqualizerTransferErrorCode.PRESET_NOT_FOUND,
                "Das Equalizer-Preset wurde nicht gefunden.",
            )
        document = {
            "type": "partyplayer-equalizer-preset",
            "format_version": EQUALIZER_FORMAT_VERSION,
            "created_at": self._now().astimezone(timezone.utc).isoformat(),
            "payload": {
                "preset_key": preset.preset_id,
                "name": preset.name,
                "preamp_db": preset.preamp_db,
                "bands": [
                    {"frequency_hz": frequency, "gain_db": gain} for frequency, gain in preset.curve
                ],
            },
        }
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            os.replace(temporary, destination)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            return self._failure(
                EqualizerTransferErrorCode.IO_FAILED,
                "Das Equalizer-Preset konnte nicht atomar exportiert werden.",
            )
        return EqualizerTransferResult(
            True,
            EqualizerTransferErrorCode.NONE,
            "Equalizer-Preset wurde exportiert.",
            preset,
            destination,
        )

    def preview_import(self, source: Path) -> EqualizerImportPreview:
        try:
            if not source.is_file() or source.stat().st_size > MAX_EQUALIZER_FILE_BYTES:
                return self._preview_failure(
                    source,
                    EqualizerTransferErrorCode.FORMAT_INVALID,
                    "Die Equalizerdatei fehlt oder überschreitet die Größenbegrenzung.",
                )
            raw = source.read_bytes()
            value = json.loads(raw.decode("utf-8-sig"))
        except (OSError, UnicodeError):
            return self._preview_failure(
                source,
                EqualizerTransferErrorCode.IO_FAILED,
                "Die Equalizerdatei konnte nicht gelesen werden.",
            )
        except json.JSONDecodeError:
            return self._preview_failure(
                source,
                EqualizerTransferErrorCode.FORMAT_INVALID,
                "Die Equalizerdatei enthält kein gültiges JSON.",
            )
        parsed = self._parse(value)
        if isinstance(parsed, EqualizerTransferResult):
            return self._preview_failure(source, parsed.error_code, parsed.message)
        preset = parsed
        conflicts = self._repository.import_conflicts(preset.preset_id, preset.name)
        digest = sha256(raw).hexdigest()
        state_token = self._state_token(digest, conflicts)
        return EqualizerImportPreview(
            True,
            EqualizerTransferErrorCode.NONE,
            "Equalizer-Preset ist gültig und kann importiert werden.",
            source,
            digest,
            preset,
            conflicts,
            state_token,
        )

    def import_preview(
        self, preview: EqualizerImportPreview, strategy: EqualizerConflictStrategy
    ) -> EqualizerTransferResult:
        if not preview.valid or preview.preset is None:
            return self._failure(preview.error_code, preview.message)
        try:
            digest = sha256(preview.source.read_bytes()).hexdigest()
        except OSError:
            digest = ""
        conflicts = self._repository.import_conflicts(preview.preset.preset_id, preview.preset.name)
        if self._state_token(digest, conflicts) != preview.state_token:
            return self._failure(
                EqualizerTransferErrorCode.SOURCE_CHANGED,
                "Datei oder Presetbestand wurde nach der Vorschau verändert.",
            )
        if conflicts:
            if strategy is EqualizerConflictStrategy.ERROR:
                return self._failure(
                    EqualizerTransferErrorCode.NAME_CONFLICT,
                    "Preset-Key oder Name ist bereits vorhanden.",
                )
            if strategy is EqualizerConflictStrategy.SKIP:
                return EqualizerTransferResult(
                    True,
                    EqualizerTransferErrorCode.NONE,
                    "Vorhandenes Equalizer-Preset wurde unverändert übersprungen.",
                    skipped=True,
                )
            if strategy is EqualizerConflictStrategy.REPLACE and preview.builtin_conflict:
                return self._failure(
                    EqualizerTransferErrorCode.BUILTIN_CONFLICT,
                    "Eingebaute Equalizer-Presets können nicht ersetzt werden.",
                )
        preset = preview.preset
        if strategy is EqualizerConflictStrategy.COPY:
            preset = self._copy_identity(preset)
        try:
            saved = self._repository.import_custom(
                preset,
                replace=strategy is EqualizerConflictStrategy.REPLACE,
            )
        except ValueError:
            return self._failure(
                EqualizerTransferErrorCode.NAME_CONFLICT,
                "Das Equalizer-Preset konnte wegen eines Konflikts nicht importiert werden.",
            )
        return EqualizerTransferResult(
            True,
            EqualizerTransferErrorCode.NONE,
            "Equalizer-Preset wurde importiert.",
            saved,
            preview.source,
        )

    def _copy_identity(self, preset: EqualizerPreset) -> EqualizerPreset:
        existing = self._repository.list_enabled()
        keys = {item.preset_id.casefold() for item in existing}
        names = {item.name.casefold() for item in existing}
        suffix = 2
        while (
            f"{preset.preset_id}-copy-{suffix}".casefold() in keys
            or f"{preset.name} ({suffix})".casefold() in names
        ):
            suffix += 1
        return EqualizerPreset(
            f"{preset.preset_id}-copy-{suffix}",
            f"{preset.name} ({suffix})",
            preset.preamp_db,
            preset.curve,
        )

    def _parse(self, value: object) -> EqualizerPreset | EqualizerTransferResult:
        if (
            not isinstance(value, dict)
            or set(value) != {"type", "format_version", "created_at", "payload"}
            or value.get("type") != "partyplayer-equalizer-preset"
        ):
            return self._failure(
                EqualizerTransferErrorCode.FORMAT_INVALID,
                "Die Equalizer-JSON-Hülle ist ungültig.",
            )
        if value.get("format_version") != EQUALIZER_FORMAT_VERSION:
            return self._failure(
                EqualizerTransferErrorCode.VERSION_UNSUPPORTED,
                "Die Equalizer-Formatversion wird nicht unterstützt.",
            )
        payload = value.get("payload")
        try:
            created_at = datetime.fromisoformat(str(value.get("created_at")))
        except ValueError:
            created_at = None
        if (
            created_at is None
            or created_at.tzinfo is None
            or not isinstance(payload, dict)
            or set(payload) != {"preset_key", "name", "preamp_db", "bands"}
        ):
            return self._failure(
                EqualizerTransferErrorCode.FORMAT_INVALID,
                "Die Equalizer-Nutzdaten sind unvollständig.",
            )
        key = payload["preset_key"]
        name = payload["name"]
        preamp = payload["preamp_db"]
        bands = payload["bands"]
        if (
            not isinstance(key, str)
            or not isinstance(name, str)
            or type(preamp) not in {int, float}
            or not isinstance(bands, list)
        ):
            return self._failure(
                EqualizerTransferErrorCode.FORMAT_INVALID,
                "Das Equalizer-Preset enthält ungültige Feldtypen.",
            )
        curve: list[tuple[float, float]] = []
        for band in bands:
            if (
                not isinstance(band, dict)
                or set(band) != {"frequency_hz", "gain_db"}
                or type(band["frequency_hz"]) not in {int, float}
                or type(band["gain_db"]) not in {int, float}
            ):
                return self._failure(
                    EqualizerTransferErrorCode.FORMAT_INVALID,
                    "Ein Equalizer-Band ist ungültig.",
                )
            curve.append((float(band["frequency_hz"]), float(band["gain_db"])))
        preset = EqualizerPreset(key.strip(), name.strip(), float(preamp), tuple(curve))
        try:
            self._validator.validate_preset(preset)
        except ValueError:
            return self._failure(
                EqualizerTransferErrorCode.FORMAT_INVALID,
                "Bandstruktur oder Equalizerwerte sind fachlich ungültig.",
            )
        return preset

    @staticmethod
    def _state_token(digest: str, conflicts: tuple[tuple[int, str, str, bool], ...]) -> str:
        return sha256(repr((digest, conflicts)).encode("utf-8")).hexdigest()

    @staticmethod
    def _preview_failure(
        source: Path, code: EqualizerTransferErrorCode, message: str
    ) -> EqualizerImportPreview:
        return EqualizerImportPreview(False, code, message, source)

    @staticmethod
    def _failure(code: EqualizerTransferErrorCode, message: str) -> EqualizerTransferResult:
        return EqualizerTransferResult(False, code, message)
