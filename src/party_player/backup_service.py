"""Consistent, atomic DeckRelay data backups without touching media files."""

from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import sqlite3
from tempfile import TemporaryDirectory
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

from party_player import __version__
from party_player.database.connection import Database
from party_player.database.migrations import LATEST_SCHEMA_VERSION, migrate
from party_player.performance_monitor import PerformanceMonitor
from party_player.product import PRODUCT_NAME, PRODUCT_SLUG


BACKUP_FORMAT_VERSION = 1
BACKUP_EXTENSION = ".partyplayer-backup"
DATABASE_ARCHIVE_PATH = "database/partyplayer.db"
MANIFEST_ARCHIVE_PATH = "manifest.json"
MAX_BACKUP_ENTRIES = 16
MAX_BACKUP_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MAX_BACKUP_FILE_BYTES = 2 * 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
MAX_COMPRESSION_RATIO = 200.0
MAX_BACKUP_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024

BACKUP_MANIFEST_JSON_SCHEMA: dict[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://partyplayer.local/schemas/backup-manifest-v1.json",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "format_version",
        "application_version",
        "product_name",
        "product_slug",
        "created_at",
        "database_schema_version",
        "platform",
        "included_sections",
        "files",
    ],
    "properties": {
        "format_version": {"type": "integer", "minimum": 0},
        "application_version": {
            "type": "string",
            "pattern": r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$",
        },
        "product_name": {"const": PRODUCT_NAME},
        "product_slug": {"const": PRODUCT_SLUG},
        "created_at": {"type": "string", "format": "date-time"},
        "database_schema_version": {"type": "integer", "minimum": 0},
        "platform": {"type": "string", "minLength": 1},
        "included_sections": {
            "type": "array",
            "const": ["database"],
        },
        "files": {
            "type": "array",
            "minItems": 1,
            "maxItems": 1,
            "uniqueItems": True,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "size", "sha256"],
                "properties": {
                    "path": {"const": DATABASE_ARCHIVE_PATH},
                    "size": {"type": "integer", "minimum": 0},
                    "sha256": {"type": "string", "pattern": "^[0-9a-fA-F]{64}$"},
                },
            },
        },
    },
}


class BackupOperationState(str, Enum):
    IDLE = "IDLE"
    VALIDATING = "VALIDATING"
    SNAPSHOTTING = "SNAPSHOTTING"
    ARCHIVING = "ARCHIVING"
    RESTORING = "RESTORING"
    MIGRATING = "MIGRATING"
    MAINTENANCE = "MAINTENANCE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class BackupPurpose(str, Enum):
    MANUAL = "MANUAL"
    SAFETY = "SAFETY"


class BackupErrorCode(str, Enum):
    NONE = ""
    SOURCE_DATABASE_MISSING = "BACKUP_SOURCE_DATABASE_MISSING"
    TARGET_DIRECTORY_INVALID = "BACKUP_TARGET_DIRECTORY_INVALID"
    TARGET_NOT_WRITABLE = "BACKUP_TARGET_NOT_WRITABLE"
    INSUFFICIENT_SPACE = "BACKUP_INSUFFICIENT_SPACE"
    SNAPSHOT_FAILED = "BACKUP_SNAPSHOT_FAILED"
    SNAPSHOT_INTEGRITY_FAILED = "BACKUP_SNAPSHOT_INTEGRITY_FAILED"
    SCHEMA_VERSION_MISSING = "BACKUP_SCHEMA_VERSION_MISSING"
    ARCHIVE_WRITE_FAILED = "BACKUP_ARCHIVE_WRITE_FAILED"
    MANIFEST_INVALID = "BACKUP_MANIFEST_INVALID"
    CHECKSUM_MISMATCH = "BACKUP_CHECKSUM_MISMATCH"
    FORMAT_VERSION_UNSUPPORTED = "BACKUP_FORMAT_VERSION_UNSUPPORTED"
    FORMAT_VERSION_TOO_NEW = "BACKUP_FORMAT_VERSION_TOO_NEW"
    APPLICATION_VERSION_TOO_NEW = "BACKUP_APPLICATION_VERSION_TOO_NEW"
    SCHEMA_VERSION_TOO_NEW = "BACKUP_SCHEMA_VERSION_TOO_NEW"
    RESTORE_ARCHIVE_MISSING = "RESTORE_ARCHIVE_MISSING"
    RESTORE_STAGING_FAILED = "RESTORE_STAGING_FAILED"
    RESTORE_DATABASE_INVALID = "RESTORE_DATABASE_INVALID"
    RESTORE_SCHEMA_MISMATCH = "RESTORE_SCHEMA_MISMATCH"
    RESTORE_MIGRATION_FAILED = "RESTORE_MIGRATION_FAILED"
    RESTORE_MIGRATION_INCOMPLETE = "RESTORE_MIGRATION_INCOMPLETE"
    RESTORE_SAFETY_BACKUP_FAILED = "RESTORE_SAFETY_BACKUP_FAILED"


class BackupCompatibility(str, Enum):
    EXACT = "EXACT"
    MIGRATION_REQUIRED = "MIGRATION_REQUIRED"


@dataclass(frozen=True, slots=True)
class BackupManifestFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class BackupManifest:
    format_version: int
    application_version: str
    created_at: str
    database_schema_version: int
    platform: str
    included_sections: tuple[str, ...]
    files: tuple[BackupManifestFile, ...]
    product_name: str | None = None
    product_slug: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


@dataclass(frozen=True, slots=True)
class BackupResult:
    success: bool
    state: BackupOperationState
    error_code: BackupErrorCode
    message: str
    backup_path: Path | None = None
    manifest: BackupManifest | None = None
    purpose: BackupPurpose = BackupPurpose.MANUAL
    retention_removed: tuple[Path, ...] = ()
    retention_warning: str = ""


@dataclass(frozen=True, slots=True)
class BackupValidationResult:
    valid: bool
    error_code: BackupErrorCode
    message: str
    manifest: BackupManifest | None = None
    compatibility: BackupCompatibility | None = None


@dataclass(frozen=True, slots=True)
class RestoreResult:
    success: bool
    state: BackupOperationState
    error_code: BackupErrorCode
    message: str
    manifest: BackupManifest | None = None
    compatibility: BackupCompatibility | None = None
    migration_performed: bool = False
    prepared_schema_version: int | None = None


@dataclass(frozen=True, slots=True)
class RestorePreparationResult:
    success: bool
    state: BackupOperationState
    error_code: BackupErrorCode
    message: str
    candidate: RestoreResult
    safety_backup_path: Path | None = None
    candidate_sha256: str = ""


@dataclass(frozen=True, slots=True)
class RestoreMaterializationResult:
    success: bool
    error_code: BackupErrorCode
    message: str
    database_path: Path | None = None
    validation: RestoreResult | None = None


def _file_digest(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _schema_version(path: Path) -> int | None:
    with closing(sqlite3.connect(path)) as connection:
        row = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return int(row[0]) if row and row[0] is not None else None


def _database_check(path: Path, pragma: str) -> tuple[str, ...]:
    if pragma not in {"quick_check", "integrity_check"}:
        raise ValueError("Unbekannte SQLite-Prüfung")
    with closing(sqlite3.connect(path)) as connection:
        return tuple(str(row[0]) for row in connection.execute(f"PRAGMA {pragma}"))


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?", value)
    if match is None:
        raise ValueError("Anwendungsversion ist ungültig")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _manifest_from_data(value: object) -> BackupManifest:
    if not isinstance(value, dict):
        raise ValueError("Manifestwurzel ist kein Objekt")
    required = {
        "format_version",
        "application_version",
        "created_at",
        "database_schema_version",
        "platform",
        "included_sections",
        "files",
    }
    product_fields = {"product_name", "product_slug"}
    if set(value) not in (required, required | product_fields):
        raise ValueError("Manifestfelder sind unvollständig oder unbekannt")
    if product_fields & set(value) and product_fields - set(value):
        raise ValueError("Produktfelder im Manifest sind unvollständig")
    files_value = value["files"]
    sections_value = value["included_sections"]
    if not isinstance(files_value, list) or not isinstance(sections_value, list):
        raise ValueError("Manifestlisten sind ungültig")
    files: list[BackupManifestFile] = []
    for item in files_value:
        if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
            raise ValueError("Manifestdatei ist ungültig")
        path = item["path"]
        size = item["size"]
        checksum = item["sha256"]
        if (
            not isinstance(path, str)
            or not path
            or PurePosixPath(path).is_absolute()
            or ".." in PurePosixPath(path).parts
            or "\\" in path
            or ":" in path
            or "\x00" in path
            or not isinstance(size, int)
            or size < 0
            or not isinstance(checksum, str)
            or len(checksum) != 64
            or any(character not in "0123456789abcdef" for character in checksum.casefold())
        ):
            raise ValueError("Manifestdateiattribute sind ungültig")
        files.append(BackupManifestFile(path, size, checksum))
    format_version = value["format_version"]
    schema_version = value["database_schema_version"]
    if type(format_version) is not int or type(schema_version) is not int:
        raise ValueError("Backupformat oder Datenbankschema ist ungültig")
    if format_version < 0 or schema_version < 0:
        raise ValueError("Backupformat oder Datenbankschema ist negativ")
    text_fields = ("application_version", "created_at", "platform")
    if any(not isinstance(value[field], str) or not value[field] for field in text_fields):
        raise ValueError("Manifesttextfeld ist ungültig")
    if any(not isinstance(section, str) or not section for section in sections_value):
        raise ValueError("Manifestbereich ist ungültig")
    if sections_value != ["database"]:
        raise ValueError("Manifestbereiche sind unbekannt, doppelt oder unvollständig")
    if len({item.path for item in files}) != len(files):
        raise ValueError("Manifest enthält doppelte Dateipfade")
    created_at = datetime.fromisoformat(str(value["created_at"]))
    if created_at.tzinfo is None or created_at.utcoffset() != timezone.utc.utcoffset(created_at):
        raise ValueError("Erstellzeit benötigt eine explizite UTC-Zeitzone")
    _version_tuple(str(value["application_version"]))
    product_name = value.get("product_name")
    product_slug = value.get("product_slug")
    if product_name is not None and (
        not isinstance(product_name, str) or not isinstance(product_slug, str)
    ):
        raise ValueError("Produktfelder im Manifest sind ungültig")
    return BackupManifest(
        format_version,
        str(value["application_version"]),
        str(value["created_at"]),
        schema_version,
        str(value["platform"]),
        tuple(sections_value),
        tuple(files),
        product_name,
        product_slug,
    )


def validate_backup_archive(
    path: Path,
    *,
    current_application_version: str = __version__,
    current_schema_version: int = LATEST_SCHEMA_VERSION,
) -> BackupValidationResult:
    """Validate format, declared entries, sizes, and checksums without extraction."""
    try:
        with ZipFile(path, "r") as archive:
            names = archive.namelist()
            entries = archive.infolist()
            if (
                len(entries) > MAX_BACKUP_ENTRIES
                or sum(entry.file_size for entry in entries) > MAX_BACKUP_UNCOMPRESSED_BYTES
                or any(entry.file_size > MAX_BACKUP_FILE_BYTES for entry in entries)
            ):
                raise ValueError("Archivgrenzen wurden überschritten")
            if any(
                entry.file_size > 0
                and (
                    entry.compress_size == 0
                    or entry.file_size / entry.compress_size > MAX_COMPRESSION_RATIO
                )
                for entry in entries
            ):
                raise ValueError("Kompressionsverhältnis überschreitet die Sicherheitsgrenze")
            if any(entry.flag_bits & 0x1 for entry in entries):
                raise ValueError("Verschlüsselte Backupeinträge sind nicht erlaubt")
            if any((entry.external_attr >> 16) & 0o170000 == 0o120000 for entry in entries):
                raise ValueError("Symbolische Links sind im Backup nicht erlaubt")
            if len(names) != len(set(names)) or set(names) != {
                MANIFEST_ARCHIVE_PATH,
                DATABASE_ARCHIVE_PATH,
            }:
                raise ValueError("Archiv enthält fehlende, doppelte oder unerwartete Einträge")
            manifest_info = archive.getinfo(MANIFEST_ARCHIVE_PATH)
            if manifest_info.file_size > MAX_MANIFEST_BYTES:
                raise ValueError("Manifest überschreitet die Sicherheitsgrenze")
            manifest = _manifest_from_data(
                json.loads(archive.read(MANIFEST_ARCHIVE_PATH).decode("utf-8"))
            )
            if manifest.format_version > BACKUP_FORMAT_VERSION:
                return BackupValidationResult(
                    False,
                    BackupErrorCode.FORMAT_VERSION_TOO_NEW,
                    f"Das Backupformat ist neuer als diese {PRODUCT_NAME}-Version.",
                    manifest,
                )
            if manifest.format_version < BACKUP_FORMAT_VERSION:
                return BackupValidationResult(
                    False,
                    BackupErrorCode.FORMAT_VERSION_UNSUPPORTED,
                    "Das Backupformat wird nicht unterstützt.",
                    manifest,
                )
            archived_app_version = _version_tuple(manifest.application_version)
            active_app_version = _version_tuple(current_application_version)
            if archived_app_version > active_app_version:
                return BackupValidationResult(
                    False,
                    BackupErrorCode.APPLICATION_VERSION_TOO_NEW,
                    f"Das Backup stammt aus einer neueren {PRODUCT_NAME}-Version.",
                    manifest,
                )
            if manifest.database_schema_version > current_schema_version:
                return BackupValidationResult(
                    False,
                    BackupErrorCode.SCHEMA_VERSION_TOO_NEW,
                    "Das Datenbankschema des Backups ist neuer als unterstützt.",
                    manifest,
                )
            declared = {item.path: item for item in manifest.files}
            if set(declared) != {DATABASE_ARCHIVE_PATH}:
                raise ValueError("Manifestdateiliste ist ungültig")
            database_bytes = archive.read(DATABASE_ARCHIVE_PATH)
            declared_database = declared[DATABASE_ARCHIVE_PATH]
            if len(database_bytes) != declared_database.size:
                return BackupValidationResult(
                    False, BackupErrorCode.CHECKSUM_MISMATCH, "Dateigröße stimmt nicht.", manifest
                )
            if sha256(database_bytes).hexdigest() != declared_database.sha256:
                return BackupValidationResult(
                    False, BackupErrorCode.CHECKSUM_MISMATCH, "Prüfsumme stimmt nicht.", manifest
                )
    except (OSError, BadZipFile, ValueError, KeyError, json.JSONDecodeError) as exc:
        return BackupValidationResult(
            False,
            BackupErrorCode.MANIFEST_INVALID,
            f"Backup kann nicht validiert werden ({type(exc).__name__}).",
        )
    compatibility = (
        BackupCompatibility.EXACT
        if archived_app_version == active_app_version
        and manifest.database_schema_version == current_schema_version
        else BackupCompatibility.MIGRATION_REQUIRED
    )
    return BackupValidationResult(
        True, BackupErrorCode.NONE, "Backup ist gültig.", manifest, compatibility
    )


class BackupService:
    """Create full database backups from a live SQLite database."""

    def __init__(
        self,
        database: Database,
        *,
        now: Callable[[], datetime] | None = None,
        free_space: Callable[[Path], int] | None = None,
        safety_retention_limit: int = 10,
        performance_monitor: PerformanceMonitor | None = None,
    ) -> None:
        if not 1 <= safety_retention_limit <= 1000:
            raise ValueError("Safety-Backup-Retention muss zwischen 1 und 1000 liegen.")
        self._database = database
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._free_space = free_space or (lambda path: shutil.disk_usage(path).free)
        self._safety_retention_limit = safety_retention_limit
        self._performance = performance_monitor or PerformanceMonitor(enabled=False)

    def create_backup(
        self,
        target_directory: Path,
        *,
        purpose: BackupPurpose = BackupPurpose.MANUAL,
    ) -> BackupResult:
        def failure(error_code: BackupErrorCode, message: str) -> BackupResult:
            return self._failure(error_code, message, purpose)

        if not self._database.path.is_file():
            return failure(BackupErrorCode.SOURCE_DATABASE_MISSING, "Quelldatenbank fehlt.")
        try:
            target_directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            return failure(
                BackupErrorCode.TARGET_DIRECTORY_INVALID, "Backupziel ist nicht beschreibbar."
            )
        if not target_directory.is_dir():
            return failure(
                BackupErrorCode.TARGET_DIRECTORY_INVALID, "Backupziel ist kein Verzeichnis."
            )
        preflight = self._preflight_target(target_directory, purpose)
        if preflight is not None:
            return preflight

        created_at = self._now().astimezone(timezone.utc)
        destination = self._available_destination(target_directory, created_at, purpose)
        temporary_archive = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        try:
            with TemporaryDirectory(prefix=".partyplayer-backup-", dir=target_directory) as temp:
                temporary_directory = Path(temp)
                snapshot = temporary_directory / "partyplayer.db"
                with self._performance.measure(
                    "backup.database.snapshot", warning_threshold_ms=1000.0
                ):
                    self._create_snapshot(snapshot)
                remaining_required = snapshot.stat().st_size + 1024 * 1024
                if self._free_space(target_directory) < remaining_required:
                    return failure(
                        BackupErrorCode.INSUFFICIENT_SPACE,
                        "Nach dem Snapshot reicht der verbleibende Speicherplatz nicht für das Archiv.",
                    )
                with self._performance.measure(
                    "backup.integrity_check", warning_threshold_ms=1000.0
                ):
                    quick_findings = _database_check(snapshot, "quick_check")
                    integrity_findings = _database_check(snapshot, "integrity_check")
                if quick_findings != ("ok",) or integrity_findings != ("ok",):
                    return failure(
                        BackupErrorCode.SNAPSHOT_INTEGRITY_FAILED,
                        "Der Datenbanksnapshot hat die Integritätsprüfung nicht bestanden.",
                    )
                schema_version = _schema_version(snapshot)
                if schema_version is None:
                    return failure(
                        BackupErrorCode.SCHEMA_VERSION_MISSING,
                        "Der Datenbanksnapshot enthält keine Schemaversion.",
                    )
                manifest = BackupManifest(
                    BACKUP_FORMAT_VERSION,
                    __version__,
                    created_at.isoformat(),
                    schema_version,
                    platform.system(),
                    ("database",),
                    (
                        BackupManifestFile(
                            DATABASE_ARCHIVE_PATH, snapshot.stat().st_size, _file_digest(snapshot)
                        ),
                    ),
                    PRODUCT_NAME,
                    PRODUCT_SLUG,
                )
                with self._performance.measure(
                    "backup.archive.create", warning_threshold_ms=1000.0
                ):
                    with ZipFile(temporary_archive, "w", compression=ZIP_DEFLATED) as archive:
                        archive.write(snapshot, DATABASE_ARCHIVE_PATH)
                        archive.writestr(MANIFEST_ARCHIVE_PATH, manifest.to_json())
                validation = validate_backup_archive(temporary_archive)
                if not validation.valid:
                    return failure(validation.error_code, validation.message)
                os.replace(temporary_archive, destination)
        except sqlite3.Error:
            return failure(BackupErrorCode.SNAPSHOT_FAILED, "SQLite-Snapshot fehlgeschlagen.")
        except OSError as exc:
            detail = f"{type(exc).__name__}, errno={exc.errno}"
            return failure(
                BackupErrorCode.ARCHIVE_WRITE_FAILED,
                f"Backup konnte nicht geschrieben werden ({detail}).",
            )
        finally:
            try:
                temporary_archive.unlink(missing_ok=True)
            except OSError:
                pass
        retention_removed: tuple[Path, ...] = ()
        retention_warning = ""
        if purpose is BackupPurpose.SAFETY:
            retention_removed, retention_warning = self._apply_safety_retention(
                target_directory, destination
            )
        return BackupResult(
            True,
            BackupOperationState.COMPLETED,
            BackupErrorCode.NONE,
            "Backup wurde erfolgreich erstellt.",
            destination,
            manifest,
            purpose,
            retention_removed,
            retention_warning,
        )

    def _apply_safety_retention(
        self, target_directory: Path, current_backup: Path
    ) -> tuple[tuple[Path, ...], str]:
        pattern = re.compile(
            rf"(?:partyplayer|{PRODUCT_SLUG})-safety-backup-\d{{4}}-\d{{2}}-\d{{2}}-\d{{6}}(?:-\d+)?"
            + re.escape(BACKUP_EXTENSION)
        )
        try:
            candidates = [
                path
                for path in target_directory.iterdir()
                if path.is_file() and pattern.fullmatch(path.name)
            ]
            previous = [path for path in candidates if path != current_backup]
            previous.sort(key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)
        except OSError as exc:
            return (
                (),
                f"Safety-Backup-Retention konnte nicht vorbereitet werden ({type(exc).__name__}).",
            )
        removed: list[Path] = []
        retained_previous = max(0, self._safety_retention_limit - 1)
        for expired in previous[retained_previous:]:
            try:
                expired.unlink()
                removed.append(expired)
            except OSError as exc:
                return (
                    tuple(removed),
                    f"Mindestens ein altes Safety-Backup konnte nicht entfernt werden ({type(exc).__name__}).",
                )
        return tuple(removed), ""

    def _preflight_target(
        self, target_directory: Path, purpose: BackupPurpose
    ) -> BackupResult | None:
        probe = target_directory / f".{PRODUCT_SLUG}-write-probe-{os.getpid()}.tmp"
        try:
            with probe.open("xb") as stream:
                stream.write(f"{PRODUCT_NAME} backup target probe\n".encode())
            probe.unlink()
        except OSError:
            try:
                probe.unlink(missing_ok=True)
            except OSError:
                pass
            return self._failure(
                BackupErrorCode.TARGET_NOT_WRITABLE,
                "Das Backupziel ist nicht zuverlässig beschreibbar.",
                purpose,
            )
        source_size = self._database.path.stat().st_size
        required_bytes = max(10 * 1024 * 1024, source_size * 3)
        try:
            free_bytes = self._free_space(target_directory)
        except OSError:
            return self._failure(
                BackupErrorCode.TARGET_DIRECTORY_INVALID,
                "Der freie Speicherplatz des Backupziels konnte nicht ermittelt werden.",
                purpose,
            )
        if free_bytes < required_bytes:
            return self._failure(
                BackupErrorCode.INSUFFICIENT_SPACE,
                "Für Snapshot und Archiv steht am Backupziel nicht genügend Speicherplatz zur Verfügung.",
                purpose,
            )
        return None

    def _create_snapshot(self, target: Path) -> None:
        with self._database.connect() as source:
            with closing(sqlite3.connect(target)) as destination:
                source.backup(destination)

    @staticmethod
    def _available_destination(target: Path, created_at: datetime, purpose: BackupPurpose) -> Path:
        kind = "backup" if purpose is BackupPurpose.MANUAL else "safety-backup"
        stem = f"{PRODUCT_SLUG}-{kind}-{created_at:%Y-%m-%d-%H%M%S}"
        candidate = target / f"{stem}{BACKUP_EXTENSION}"
        suffix = 1
        while candidate.exists():
            candidate = target / f"{stem}-{suffix}{BACKUP_EXTENSION}"
            suffix += 1
        return candidate

    @staticmethod
    def _failure(
        error_code: BackupErrorCode,
        message: str,
        purpose: BackupPurpose = BackupPurpose.MANUAL,
    ) -> BackupResult:
        return BackupResult(
            False, BackupOperationState.FAILED, error_code, message, purpose=purpose
        )


class RestoreValidator:
    """Validate a restore candidate without modifying the active database."""

    def __init__(
        self,
        *,
        migrator: Callable[[Database], None] = migrate,
        current_schema_version: int = LATEST_SCHEMA_VERSION,
        performance_monitor: PerformanceMonitor | None = None,
    ) -> None:
        self._migrator = migrator
        self._current_schema_version = current_schema_version
        self._performance = performance_monitor or PerformanceMonitor(enabled=False)

    def validate(self, archive_path: Path) -> RestoreResult:
        with self._performance.measure("restore.validate", warning_threshold_ms=1000.0):
            return self._validate(archive_path)

    def _validate(self, archive_path: Path) -> RestoreResult:
        if not archive_path.is_file():
            return self._failure(BackupErrorCode.RESTORE_ARCHIVE_MISSING, "Das Backuparchiv fehlt.")
        try:
            if archive_path.stat().st_size > MAX_BACKUP_ARCHIVE_BYTES:
                return self._failure(
                    BackupErrorCode.MANIFEST_INVALID,
                    "Das Backuparchiv überschreitet die Sicherheitsgrenze.",
                )
            with TemporaryDirectory(prefix="partyplayer-restore-validation-") as temp:
                staging = Path(temp)
                staged_archive = staging / "candidate.partyplayer-backup"
                shutil.copyfile(archive_path, staged_archive)
                validation = validate_backup_archive(
                    staged_archive, current_schema_version=self._current_schema_version
                )
                if not validation.valid or validation.manifest is None:
                    return RestoreResult(
                        False,
                        BackupOperationState.FAILED,
                        validation.error_code,
                        validation.message,
                        validation.manifest,
                        validation.compatibility,
                    )
                staged_database = staging / "partyplayer.db"
                with ZipFile(staged_archive, "r") as archive:
                    with archive.open(DATABASE_ARCHIVE_PATH, "r") as source:
                        with staged_database.open("wb") as destination:
                            shutil.copyfileobj(source, destination, length=1024 * 1024)
                try:
                    quick_findings = _database_check(staged_database, "quick_check")
                    integrity_findings = _database_check(staged_database, "integrity_check")
                    schema_version = _schema_version(staged_database)
                except sqlite3.Error:
                    return self._failure(
                        BackupErrorCode.RESTORE_DATABASE_INVALID,
                        "Die wiederherzustellende Datei ist keine gültige SQLite-Datenbank.",
                        validation,
                    )
                if quick_findings != ("ok",) or integrity_findings != ("ok",):
                    return self._failure(
                        BackupErrorCode.RESTORE_DATABASE_INVALID,
                        "Die wiederherzustellende Datenbank ist nicht integer.",
                        validation,
                    )
                if schema_version != validation.manifest.database_schema_version:
                    return self._failure(
                        BackupErrorCode.RESTORE_SCHEMA_MISMATCH,
                        "Manifest und Datenbank enthalten unterschiedliche Schemaversionen.",
                        validation,
                    )
                migration_performed = False
                prepared_schema_version = schema_version
                if schema_version < self._current_schema_version:
                    migrated_database = staging / "migrated-partyplayer.db"
                    shutil.copyfile(staged_database, migrated_database)
                    try:
                        with self._performance.measure(
                            "restore.migration", warning_threshold_ms=1000.0
                        ):
                            self._migrator(Database(migrated_database))
                        migrated_quick = _database_check(migrated_database, "quick_check")
                        migrated_integrity = _database_check(migrated_database, "integrity_check")
                        migrated_schema_version = _schema_version(migrated_database)
                    except (OSError, sqlite3.Error, RuntimeError, ValueError):
                        return self._failure(
                            BackupErrorCode.RESTORE_MIGRATION_FAILED,
                            "Die temporäre Datenbankmigration ist fehlgeschlagen.",
                            validation,
                        )
                    if (
                        migrated_quick != ("ok",)
                        or migrated_integrity != ("ok",)
                        or migrated_schema_version != self._current_schema_version
                    ):
                        return self._failure(
                            BackupErrorCode.RESTORE_MIGRATION_INCOMPLETE,
                            "Die migrierte Datenbank hat die Abschlussprüfung nicht bestanden.",
                            validation,
                        )
                    prepared_schema_version = migrated_schema_version
                    migration_performed = True
        except (OSError, BadZipFile, sqlite3.Error):
            return self._failure(
                BackupErrorCode.RESTORE_STAGING_FAILED,
                "Das Backup konnte nicht sicher für die Wiederherstellung vorbereitet werden.",
            )
        return RestoreResult(
            True,
            BackupOperationState.COMPLETED,
            BackupErrorCode.NONE,
            "Das Backup wurde vollständig geprüft und kann vorbereitet werden.",
            validation.manifest,
            validation.compatibility,
            migration_performed,
            prepared_schema_version,
        )

    @staticmethod
    def _failure(
        error_code: BackupErrorCode,
        message: str,
        validation: BackupValidationResult | None = None,
    ) -> RestoreResult:
        return RestoreResult(
            False,
            BackupOperationState.FAILED,
            error_code,
            message,
            validation.manifest if validation is not None else None,
            validation.compatibility if validation is not None else None,
        )


class RestorePreparationService:
    """Require a validated candidate and safety backup before any future commit."""

    def __init__(
        self,
        validator: RestoreValidator,
        backup_service: BackupService,
        performance_monitor: PerformanceMonitor | None = None,
    ) -> None:
        self._validator = validator
        self._backup_service = backup_service
        self._performance = performance_monitor or PerformanceMonitor(enabled=False)

    def prepare(
        self, archive_path: Path, safety_backup_directory: Path
    ) -> RestorePreparationResult:
        candidate = self._validator.validate(archive_path)
        if not candidate.success:
            return RestorePreparationResult(
                False,
                BackupOperationState.FAILED,
                candidate.error_code,
                candidate.message,
                candidate,
            )
        try:
            candidate_digest = _file_digest(archive_path)
        except OSError:
            return RestorePreparationResult(
                False,
                BackupOperationState.FAILED,
                BackupErrorCode.RESTORE_STAGING_FAILED,
                "Der geprüfte Restore-Kandidat ist nicht mehr lesbar.",
                candidate,
            )
        with self._performance.measure("restore.safety_backup", warning_threshold_ms=1000.0):
            safety_backup = self._backup_service.create_backup(
                safety_backup_directory, purpose=BackupPurpose.SAFETY
            )
        if not safety_backup.success or safety_backup.backup_path is None:
            return RestorePreparationResult(
                False,
                BackupOperationState.FAILED,
                BackupErrorCode.RESTORE_SAFETY_BACKUP_FAILED,
                "Das obligatorische Sicherheitsbackup ist fehlgeschlagen.",
                candidate,
                candidate_sha256=candidate_digest,
            )
        safety_validation = validate_backup_archive(safety_backup.backup_path)
        if not safety_validation.valid:
            return RestorePreparationResult(
                False,
                BackupOperationState.FAILED,
                BackupErrorCode.RESTORE_SAFETY_BACKUP_FAILED,
                "Das obligatorische Sicherheitsbackup ist nach Erstellung ungültig.",
                candidate,
                safety_backup.backup_path,
                candidate_digest,
            )
        return RestorePreparationResult(
            True,
            BackupOperationState.COMPLETED,
            BackupErrorCode.NONE,
            "Restore-Kandidat und Sicherheitsbackup sind vollständig geprüft.",
            candidate,
            safety_backup.backup_path,
            candidate_digest,
        )


class RestoreMaterializer:
    """Materialize a validated candidate as one current-schema staging database."""

    def __init__(
        self,
        validator: RestoreValidator | None = None,
        performance_monitor: PerformanceMonitor | None = None,
    ) -> None:
        self._validator = validator or RestoreValidator(performance_monitor=performance_monitor)
        self._performance = performance_monitor or PerformanceMonitor(enabled=False)

    def materialize(self, archive_path: Path, destination: Path) -> RestoreMaterializationResult:
        validation = self._validator.validate(archive_path)
        if not validation.success:
            return RestoreMaterializationResult(
                False, validation.error_code, validation.message, validation=validation
            )
        if destination.exists() or not destination.parent.is_dir():
            return RestoreMaterializationResult(
                False,
                BackupErrorCode.RESTORE_STAGING_FAILED,
                "Der Restore-Stagingpfad ist nicht frei oder nicht verfügbar.",
                validation=validation,
            )
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.materializing")
        try:
            with ZipFile(archive_path, "r") as archive:
                with archive.open(DATABASE_ARCHIVE_PATH, "r") as source:
                    with temporary.open("xb") as target:
                        shutil.copyfileobj(source, target, length=1024 * 1024)
            schema_version = _schema_version(temporary)
            if schema_version is None:
                raise sqlite3.DatabaseError("missing schema version")
            if schema_version < LATEST_SCHEMA_VERSION:
                with self._performance.measure("restore.migration", warning_threshold_ms=1000.0):
                    migrate(Database(temporary))
            if (
                _schema_version(temporary) != LATEST_SCHEMA_VERSION
                or _database_check(temporary, "quick_check") != ("ok",)
                or _database_check(temporary, "integrity_check") != ("ok",)
            ):
                raise sqlite3.DatabaseError("materialized database validation failed")
            os.replace(temporary, destination)
        except (OSError, BadZipFile, sqlite3.Error, RuntimeError, ValueError):
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            return RestoreMaterializationResult(
                False,
                BackupErrorCode.RESTORE_STAGING_FAILED,
                "Die validierte Restore-Datenbank konnte nicht materialisiert werden.",
                validation=validation,
            )
        return RestoreMaterializationResult(
            True,
            BackupErrorCode.NONE,
            "Restore-Datenbank wurde für den atomaren Commit materialisiert.",
            destination,
            validation,
        )
