"""Backend-neutral EBU R128 loudness-analysis contracts."""

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class LoudnessAnalysisResult:
    """Validated BS.1770/EBU R128 measurements for one complete audio file."""

    integrated_loudness_lufs: float
    loudness_range_lu: float
    true_peak_dbfs: float
    method: str
    backend_name: str

    def __post_init__(self) -> None:
        values = (
            self.integrated_loudness_lufs,
            self.loudness_range_lu,
            self.true_peak_dbfs,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Lautheitsmesswerte müssen endlich sein")
        if not self.method.strip() or not self.backend_name.strip():
            raise ValueError("Messmethode und Backend dürfen nicht leer sein")


@runtime_checkable
class LoudnessAnalysisBackend(Protocol):
    """Measure a complete file without modifying its audio data or tags."""

    @property
    def name(self) -> str: ...

    def is_available(self) -> bool: ...

    def analyze(self, file_path: Path) -> LoudnessAnalysisResult: ...
