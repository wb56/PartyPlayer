"""Validated persistent-ready result model for automatic cue analysis."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import math
from pathlib import Path

from party_player.analysis.cue_estimation import DetectedCueBoundaries
from party_player.analysis.levels import PcmLevelWindow


@dataclass(frozen=True, slots=True)
class CueAnalysisResult:
    """One immutable, versioned automatic cue-analysis result."""

    file_path: Path
    file_duration_seconds: float
    cue_in: float
    cue_out: float
    suggested_fade_duration: float
    minimum_level_dbfs: float
    maximum_level_dbfs: float
    peak: float
    measured_window_count: int
    confidence: float
    analysis_version: str
    analyzed_at: datetime
    backend_name: str

    def __post_init__(self) -> None:
        numeric = (
            self.file_duration_seconds,
            self.cue_in,
            self.cue_out,
            self.suggested_fade_duration,
            self.minimum_level_dbfs,
            self.maximum_level_dbfs,
            self.peak,
            self.confidence,
        )
        if not all(math.isfinite(value) for value in numeric):
            raise ValueError("Analyseergebnis enthält keinen endlichen Zahlenwert")
        if self.file_duration_seconds <= 0:
            raise ValueError("Analyseergebnis benötigt eine positive Titeldauer")
        if not 0 <= self.cue_in < self.cue_out <= self.file_duration_seconds + 0.25:
            raise ValueError("Analyseergebnis enthält ungültige Cue-Grenzen")
        if not 0 < self.suggested_fade_duration < self.cue_out - self.cue_in:
            raise ValueError("Analyseergebnis enthält eine ungültige Überblenddauer")
        if self.minimum_level_dbfs > self.maximum_level_dbfs:
            raise ValueError("Minimalpegel darf nicht über dem Maximalpegel liegen")
        if self.peak < 0:
            raise ValueError("PCM-Peak darf nicht negativ sein")
        if self.measured_window_count <= 0:
            raise ValueError("Analyseergebnis benötigt mindestens ein Pegelfenster")
        if not 0 <= self.confidence <= 1:
            raise ValueError("Analysekonfidenz muss zwischen 0 und 1 liegen")
        if not self.analysis_version.strip() or not self.backend_name.strip():
            raise ValueError("Analyseversion und Backendname dürfen nicht leer sein")
        if self.analyzed_at.tzinfo is None or self.analyzed_at.utcoffset() is None:
            raise ValueError("Analysezeitpunkt muss eine Zeitzone enthalten")

    @classmethod
    def from_measurements(
        cls,
        file_path: Path,
        file_duration_seconds: float,
        boundaries: DetectedCueBoundaries,
        levels: Sequence[PcmLevelWindow],
        *,
        confidence: float,
        analysis_version: str,
        backend_name: str,
        analyzed_at: datetime | None = None,
    ) -> "CueAnalysisResult":
        """Summarize bounded windows without retaining their individual samples."""
        if not levels:
            raise ValueError("Analyseergebnis benötigt mindestens ein Pegelfenster")
        return cls(
            file_path=file_path,
            file_duration_seconds=file_duration_seconds,
            cue_in=boundaries.cue_in,
            cue_out=boundaries.cue_out,
            suggested_fade_duration=boundaries.suggested_fade_duration,
            minimum_level_dbfs=min(level.level_dbfs for level in levels),
            maximum_level_dbfs=max(level.level_dbfs for level in levels),
            peak=max(level.peak for level in levels),
            measured_window_count=len(levels),
            confidence=confidence,
            analysis_version=analysis_version.strip(),
            analyzed_at=analyzed_at or datetime.now(timezone.utc),
            backend_name=backend_name.strip(),
        )
