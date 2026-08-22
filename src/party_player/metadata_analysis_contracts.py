"""Serializable contracts for isolated catalog-metadata analysis."""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import math
from pathlib import Path
from typing import TypeAlias


Primitive: TypeAlias = str | int | float | bool | None


class MetadataAnalysisKind(StrEnum):
    BPM = "BPM"
    ENERGY = "ENERGY"
    DANCEABILITY = "DANCEABILITY"
    MOOD = "MOOD"


class MetadataAnalysisOutcome(StrEnum):
    SUCCESS = "SUCCESS"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"
    FILE_MISSING = "FILE_MISSING"
    FILE_CHANGED = "FILE_CHANGED"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
    ANALYSIS_ERROR = "ANALYSIS_ERROR"
    WORKER_CRASHED = "WORKER_CRASHED"


class MetadataAnalysisSource(StrEnum):
    AUDIO_ANALYSIS = "AUDIO_ANALYSIS"
    DIAGNOSTIC = "DIAGNOSTIC"


class MetadataAnalysisBackendKind(StrEnum):
    DIAGNOSTIC = "DIAGNOSTIC"
    FAKE = "FAKE"
    FFMPEG_TEMPO = "FFMPEG_TEMPO"


@dataclass(frozen=True, slots=True)
class MetadataAnalysisRequest:
    track_id: int
    input_snapshot: "FileSnapshot"
    analysis_profile: str
    analysis_version: str
    requested_kinds: tuple[MetadataAnalysisKind, ...]
    priority: int = 0
    timeout_seconds: float = 300.0
    backend: MetadataAnalysisBackendKind = MetadataAnalysisBackendKind.DIAGNOSTIC
    technical_options: tuple[tuple[str, Primitive], ...] = ()

    def __post_init__(self) -> None:
        if (
            self.track_id <= 0
            or not self.analysis_profile.strip()
            or not self.analysis_version.strip()
        ):
            raise ValueError("Analyseanforderung ist unvollständig")
        if not self.requested_kinds or len(set(self.requested_kinds)) != len(self.requested_kinds):
            raise ValueError("Analysearten müssen eindeutig und nicht leer sein")
        if not 0.1 <= self.timeout_seconds <= 86_400.0:
            raise ValueError("Timeout liegt außerhalb des zulässigen Bereichs")
        keys = [key for key, _value in self.technical_options]
        if (
            len(keys) > 20
            or any(not key or len(key) > 80 for key in keys)
            or len(set(keys)) != len(keys)
        ):
            raise ValueError("Technische Optionen sind ungültig")


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    normalized_path: str
    size: int
    modified_ns: int
    fingerprint: str | None = None

    def __post_init__(self) -> None:
        if not self.normalized_path or self.size < 0 or self.modified_ns < 0:
            raise ValueError("Dateisnapshot ist ungültig")

    @classmethod
    def capture(cls, path: str, fingerprint: str | None = None) -> "FileSnapshot":
        normalized = str(Path(path).resolve())
        stat = Path(normalized).stat()
        return cls(normalized, stat.st_size, stat.st_mtime_ns, fingerprint)

    def matches_file(self) -> bool:
        try:
            current = self.capture(self.normalized_path, self.fingerprint)
        except OSError:
            return False
        return current.size == self.size and current.modified_ns == self.modified_ns


@dataclass(frozen=True, slots=True)
class MetadataAnalysisJob:
    job_id: str
    run_id: int
    track_id: int
    input_snapshot: FileSnapshot
    analysis_profile: str
    analysis_version: str
    requested_kinds: tuple[MetadataAnalysisKind, ...]
    priority: int
    timeout_seconds: float
    created_at: str
    backend: MetadataAnalysisBackendKind = MetadataAnalysisBackendKind.DIAGNOSTIC
    technical_options: tuple[tuple[str, Primitive], ...] = ()

    def __post_init__(self) -> None:
        if not self.job_id.strip() or self.run_id <= 0 or self.track_id <= 0:
            raise ValueError("Job-, Run- und Track-ID müssen gültig sein")
        if not self.analysis_profile.strip() or not self.analysis_version.strip():
            raise ValueError("Analyseprofil und -version dürfen nicht leer sein")
        if not self.requested_kinds or len(set(self.requested_kinds)) != len(self.requested_kinds):
            raise ValueError("Analysearten müssen eindeutig und nicht leer sein")
        if not 0.1 <= self.timeout_seconds <= 86_400.0:
            raise ValueError("Timeout liegt außerhalb des zulässigen Bereichs")
        if len(self.technical_options) > 20:
            raise ValueError("Zu viele technische Optionen")
        keys = [key for key, _value in self.technical_options]
        if any(not key or len(key) > 80 for key in keys) or len(set(keys)) != len(keys):
            raise ValueError("Technische Optionen benötigen eindeutige kurze Namen")
        try:
            datetime.fromisoformat(self.created_at)
        except ValueError as exc:
            raise ValueError("Erstellungszeitpunkt ist nicht ISO-8601-konform") from exc

    @classmethod
    def created_now(cls, **values: object) -> "MetadataAnalysisJob":
        return cls(created_at=datetime.now(timezone.utc).isoformat(), **values)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class MetadataFieldSuggestion:
    field_key: str
    canonical_value: Primitive | tuple[Primitive, ...]
    source: MetadataAnalysisSource
    confidence: float

    def __post_init__(self) -> None:
        if not self.field_key.strip() or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("Metadatenvorschlag ist ungültig")


@dataclass(frozen=True, slots=True)
class AnalyzedAudioRange:
    start_seconds: float
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class TechnicalAudioMetric:
    name: str
    value: float
    unit: str = ""

    def __post_init__(self) -> None:
        if not self.name or len(self.name) > 80 or not math.isfinite(self.value):
            raise ValueError("Technischer Messwert ist ungültig")
        if len(self.unit) > 24:
            raise ValueError("Messwerteinheit ist zu lang")


@dataclass(frozen=True, slots=True)
class MetadataAnalysisResult:
    job_id: str
    run_id: int
    track_id: int
    input_snapshot: FileSnapshot
    analysis_profile: str
    analysis_version: str
    started_at: str
    finished_at: str
    outcome: MetadataAnalysisOutcome
    suggestions: tuple[MetadataFieldSuggestion, ...] = ()
    analyzed_ranges: tuple[AnalyzedAudioRange, ...] = ()
    technical_metrics: tuple[TechnicalAudioMetric, ...] = ()
    rhythm_stability: float = 0.0
    warnings: tuple[str, ...] = ()
    error_code: str = ""
    error_text: str = ""
    backend_name: str = ""
    backend_version: str = ""

    def __post_init__(self) -> None:
        if not self.job_id or self.run_id <= 0 or self.track_id <= 0:
            raise ValueError("Ergebnisidentität ist ungültig")
        if len(self.warnings) > 20 or any(len(item) > 500 for item in self.warnings):
            raise ValueError("Warnungen überschreiten die Vertragsgrenze")
        if not 0.0 <= self.rhythm_stability <= 1.0:
            raise ValueError("Rhythmusstabilität liegt außerhalb des Wertebereichs")
        if len(self.error_code) > 80 or len(self.error_text) > 500:
            raise ValueError("Fehlerangaben überschreiten die Vertragsgrenze")
        if (
            self.outcome
            not in {
                MetadataAnalysisOutcome.SUCCESS,
                MetadataAnalysisOutcome.PARTIAL_SUCCESS,
            }
            and self.suggestions
        ):
            raise ValueError("Fehlerhafte Ergebnisse dürfen keine Vorschläge enthalten")
