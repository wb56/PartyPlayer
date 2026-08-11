"""Versioned overlay catalog export, preview, and atomic import."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import json
import os
from pathlib import Path

from party_player.overlay import OverlayDefinition, OverlayRecord
from party_player.overlay_service import OverlayService
from party_player.repositories.overlay_repository import OverlayRepository


OVERLAY_FORMAT_VERSION = 1
MAX_OVERLAY_FILE_BYTES = 2 * 1024 * 1024


class OverlayConflictStrategy(StrEnum):
    KEEP_EXISTING = "KEEP_EXISTING"
    REPLACE_EXISTING = "REPLACE_EXISTING"


class OverlayTransferErrorCode(StrEnum):
    NONE = ""
    FORMAT_INVALID = "OVERLAY_FORMAT_INVALID"
    VERSION_UNSUPPORTED = "OVERLAY_VERSION_UNSUPPORTED"
    SOURCE_CHANGED = "OVERLAY_SOURCE_CHANGED"
    STATE_CHANGED = "OVERLAY_STATE_CHANGED"
    IO_FAILED = "OVERLAY_IO_FAILED"
    COMMIT_FAILED = "OVERLAY_COMMIT_FAILED"


OverlayConflict = tuple[int, str, int | None, str | None]


@dataclass(frozen=True, slots=True)
class OverlayTransferResult:
    success: bool
    error_code: OverlayTransferErrorCode
    message: str
    path: Path | None = None
    imported_count: int = 0


@dataclass(frozen=True, slots=True)
class OverlayImportPreview:
    valid: bool
    error_code: OverlayTransferErrorCode
    message: str
    source: Path
    source_sha256: str = ""
    records: tuple[OverlayRecord, ...] = ()
    conflicts: tuple[OverlayConflict, ...] = ()
    state_token: str = ""

    @property
    def can_import(self) -> bool:
        return self.valid and bool(self.records)


class OverlayTransferService:
    def __init__(
        self,
        repository: OverlayRepository,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._now = now or (lambda: datetime.now(timezone.utc))

    def export(self, destination: Path) -> OverlayTransferResult:
        records = tuple(self._repository.list_all())
        document = {
            "type": "partyplayer-overlay-catalog",
            "format_version": OVERLAY_FORMAT_VERSION,
            "created_at": self._now().astimezone(timezone.utc).isoformat(),
            "payload": {"overlays": [self._serialize(record) for record in records]},
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
                OverlayTransferErrorCode.IO_FAILED,
                "Overlay-Konfiguration konnte nicht atomar exportiert werden.",
            )
        return OverlayTransferResult(
            True,
            OverlayTransferErrorCode.NONE,
            "Overlay-Konfiguration wurde exportiert; Audiodateien wurden nicht kopiert.",
            destination,
        )

    def preview_import(self, source: Path) -> OverlayImportPreview:
        try:
            if not source.is_file() or source.stat().st_size > MAX_OVERLAY_FILE_BYTES:
                return self._preview_failure(
                    source,
                    OverlayTransferErrorCode.FORMAT_INVALID,
                    "Overlay-Datei fehlt oder überschreitet die Größenbegrenzung.",
                )
            raw = source.read_bytes()
            value = json.loads(raw.decode("utf-8-sig"))
        except (OSError, UnicodeError):
            return self._preview_failure(
                source,
                OverlayTransferErrorCode.IO_FAILED,
                "Overlay-Datei konnte nicht gelesen werden.",
            )
        except json.JSONDecodeError:
            return self._preview_failure(
                source,
                OverlayTransferErrorCode.FORMAT_INVALID,
                "Overlay-Datei enthält kein gültiges JSON.",
            )
        parsed = self._parse(value)
        if isinstance(parsed, OverlayTransferResult):
            return self._preview_failure(source, parsed.error_code, parsed.message)
        digest = sha256(raw).hexdigest()
        conflicts = self._repository.import_conflicts(parsed)
        return OverlayImportPreview(
            True,
            OverlayTransferErrorCode.NONE,
            "Overlay-Konfiguration ist gültig und kann importiert werden.",
            source,
            digest,
            parsed,
            conflicts,
            self._state_token(digest, conflicts),
        )

    def import_preview(
        self, preview: OverlayImportPreview, strategy: OverlayConflictStrategy
    ) -> OverlayTransferResult:
        if not preview.can_import:
            return self._failure(preview.error_code, preview.message)
        try:
            digest = sha256(preview.source.read_bytes()).hexdigest()
        except OSError:
            digest = ""
        conflicts = self._repository.import_conflicts(preview.records)
        if digest != preview.source_sha256:
            return self._failure(
                OverlayTransferErrorCode.SOURCE_CHANGED,
                "Overlay-Datei wurde nach der Vorschau verändert.",
            )
        if self._state_token(digest, conflicts) != preview.state_token:
            return self._failure(
                OverlayTransferErrorCode.STATE_CHANGED,
                "Overlay-Bestand wurde nach der Vorschau verändert.",
            )
        try:
            imported = self._repository.import_records(
                preview.records,
                replace_existing=strategy is OverlayConflictStrategy.REPLACE_EXISTING,
            )
        except (ValueError, KeyError):
            return self._failure(
                OverlayTransferErrorCode.COMMIT_FAILED,
                "Overlay-Konfiguration konnte nicht vollständig importiert werden.",
            )
        return OverlayTransferResult(
            True,
            OverlayTransferErrorCode.NONE,
            "Overlay-Konfiguration wurde importiert; Audiodateien wurden nicht kopiert.",
            preview.source,
            len(imported),
        )

    @staticmethod
    def _serialize(record: OverlayRecord) -> dict[str, object]:
        definition = record.definition
        return {
            "name": definition.name,
            "category": definition.category,
            "file_path": definition.file_path,
            "enabled": record.enabled,
            "volume_percent": definition.volume_percent,
            "fade_in_ms": definition.fade_in_ms,
            "fade_out_ms": definition.fade_out_ms,
            "cue_in_ms": definition.cue_in_ms,
            "cue_out_ms": definition.cue_out_ms,
            "ducking_enabled": definition.ducking_enabled,
            "ducking_db": definition.ducking_db,
            "ducking_attack_ms": definition.ducking_attack_ms,
            "ducking_release_ms": definition.ducking_release_ms,
            "favorite_position": record.favorite_position,
            "keyboard_shortcut": record.keyboard_shortcut,
        }

    def _parse(self, value: object) -> tuple[OverlayRecord, ...] | OverlayTransferResult:
        if (
            not isinstance(value, dict)
            or set(value) != {"type", "format_version", "created_at", "payload"}
            or value.get("type") != "partyplayer-overlay-catalog"
        ):
            return self._failure(
                OverlayTransferErrorCode.FORMAT_INVALID, "Overlay-JSON-Hülle ist ungültig."
            )
        if value.get("format_version") != OVERLAY_FORMAT_VERSION:
            return self._failure(
                OverlayTransferErrorCode.VERSION_UNSUPPORTED,
                "Overlay-Formatversion wird nicht unterstützt.",
            )
        try:
            created_at = datetime.fromisoformat(str(value["created_at"]))
        except ValueError:
            created_at = None
        payload = value.get("payload")
        if (
            created_at is None
            or created_at.tzinfo is None
            or not isinstance(payload, dict)
            or set(payload) != {"overlays"}
            or not isinstance(payload["overlays"], list)
        ):
            return self._failure(
                OverlayTransferErrorCode.FORMAT_INVALID, "Overlay-Nutzdaten sind unvollständig."
            )
        records: list[OverlayRecord] = []
        names: set[str] = set()
        favorites: set[int] = set()
        shortcuts: set[str] = set()
        expected = set(self._serialize(OverlayRecord(OverlayDefinition(0, "x", "x.mp3"))))
        for item in payload["overlays"]:
            if not isinstance(item, dict) or set(item) != expected:
                return self._failure(
                    OverlayTransferErrorCode.FORMAT_INVALID,
                    "Eine Overlaydefinition ist unvollständig.",
                )
            try:
                record = self._record(item)
                OverlayService.validate(record)
            except (TypeError, ValueError):
                return self._failure(
                    OverlayTransferErrorCode.FORMAT_INVALID,
                    "Eine Overlaydefinition ist fachlich ungültig.",
                )
            name_key = record.definition.name.casefold()
            shortcut_key = record.keyboard_shortcut.casefold() if record.keyboard_shortcut else None
            if (
                name_key in names
                or (record.favorite_position is not None and record.favorite_position in favorites)
                or (shortcut_key is not None and shortcut_key in shortcuts)
            ):
                return self._failure(
                    OverlayTransferErrorCode.FORMAT_INVALID,
                    "Die Importdatei enthält interne Overlaykonflikte.",
                )
            names.add(name_key)
            if record.favorite_position is not None:
                favorites.add(record.favorite_position)
            if shortcut_key is not None:
                shortcuts.add(shortcut_key)
            records.append(record)
        if not records:
            return self._failure(
                OverlayTransferErrorCode.FORMAT_INVALID, "Die Importdatei enthält keine Overlays."
            )
        return tuple(records)

    @staticmethod
    def _record(item: dict[str, object]) -> OverlayRecord:
        def integer(name: str) -> int:
            value = item[name]
            if type(value) is not int:
                raise TypeError(name)
            return value

        def boolean(name: str) -> bool:
            value = item[name]
            if type(value) is not bool:
                raise TypeError(name)
            return value

        name = item["name"]
        category = item["category"]
        file_path = item["file_path"]
        if (
            not isinstance(name, str)
            or not isinstance(category, str)
            or not isinstance(file_path, str)
        ):
            raise TypeError("text")
        cue_out = item["cue_out_ms"]
        favorite = item["favorite_position"]
        shortcut = item["keyboard_shortcut"]
        ducking_db = item["ducking_db"]
        if (
            cue_out is not None
            and type(cue_out) is not int
            or favorite is not None
            and type(favorite) is not int
            or shortcut is not None
            and not isinstance(shortcut, str)
            or type(ducking_db) not in {int, float}
        ):
            raise TypeError("optional")
        assert isinstance(ducking_db, (int, float))
        return OverlayRecord(
            OverlayDefinition(
                0,
                name.strip(),
                file_path.strip(),
                category.strip(),
                integer("volume_percent"),
                integer("fade_in_ms"),
                integer("fade_out_ms"),
                integer("cue_in_ms"),
                cue_out,
                boolean("ducking_enabled"),
                float(ducking_db),
                integer("ducking_attack_ms"),
                integer("ducking_release_ms"),
            ),
            boolean("enabled"),
            favorite,
            shortcut.strip() if isinstance(shortcut, str) else None,
        )

    @staticmethod
    def _state_token(digest: str, conflicts: tuple[OverlayConflict, ...]) -> str:
        return sha256(repr((digest, conflicts)).encode("utf-8")).hexdigest()

    @staticmethod
    def _preview_failure(
        source: Path, code: OverlayTransferErrorCode, message: str
    ) -> OverlayImportPreview:
        return OverlayImportPreview(False, code, message, source)

    @staticmethod
    def _failure(code: OverlayTransferErrorCode, message: str) -> OverlayTransferResult:
        return OverlayTransferResult(False, code, message)
