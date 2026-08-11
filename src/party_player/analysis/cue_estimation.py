"""Safe automatic Cue In, Cue Out and fade suggestions."""

from dataclasses import dataclass
import math

from party_player.analysis.signal_detection import SignalRegion


@dataclass(frozen=True, slots=True)
class CueBoundarySettings:
    """Configuration for converting detected signal into cue suggestions."""

    edge_window_seconds: float = 45.0
    preferred_fade_seconds: float = 7.0
    minimum_fade_seconds: float = 0.5

    def __post_init__(self) -> None:
        values = (
            self.edge_window_seconds,
            self.preferred_fade_seconds,
            self.minimum_fade_seconds,
        )
        if not all(math.isfinite(value) and value > 0 for value in values):
            raise ValueError("Cue-Schätzung benötigt positive endliche Einstellungen")
        if self.edge_window_seconds > 60.0:
            raise ValueError("Das Randfenster darf höchstens 60 Sekunden lang sein")
        if self.preferred_fade_seconds < self.minimum_fade_seconds:
            raise ValueError("Die bevorzugte Überblendung darf nicht unter dem Minimum liegen")


@dataclass(frozen=True, slots=True)
class DetectedCueBoundaries:
    """Automatically detected values before persistence or manual review."""

    cue_in: float
    cue_out: float
    suggested_fade_duration: float

    @property
    def usable_duration(self) -> float:
        return max(0.0, self.cue_out - self.cue_in)


class CueBoundaryEstimator:
    """Turn robust edge signal regions into conservative cue suggestions."""

    def __init__(self, settings: CueBoundarySettings | None = None) -> None:
        self.settings = settings or CueBoundarySettings()

    def estimate(
        self,
        file_duration_seconds: float,
        regions: tuple[SignalRegion, ...],
    ) -> DetectedCueBoundaries:
        duration = float(file_duration_seconds)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("Für Cue-Vorschläge wird eine gültige Titeldauer benötigt")
        valid_regions = tuple(
            region
            for region in regions
            if 0 <= region.start_seconds < region.end_seconds <= duration + 0.25
        )
        head_end = min(duration, self.settings.edge_window_seconds)
        tail_start = max(0.0, duration - self.settings.edge_window_seconds)
        head_regions = tuple(region for region in valid_regions if region.start_seconds < head_end)
        tail_regions = tuple(region for region in valid_regions if region.end_seconds > tail_start)

        cue_in = min((region.start_seconds for region in head_regions), default=0.0)
        cue_out = max((region.end_seconds for region in tail_regions), default=duration)
        cue_in = max(0.0, min(cue_in, duration))
        cue_out = max(cue_in, min(cue_out, duration))
        usable = cue_out - cue_in
        if usable <= 0:
            cue_in, cue_out, usable = 0.0, duration, duration

        fade_cap = usable * 0.5
        suggested_fade = min(self.settings.preferred_fade_seconds, fade_cap)
        if fade_cap >= self.settings.minimum_fade_seconds:
            suggested_fade = max(self.settings.minimum_fade_seconds, suggested_fade)
        return DetectedCueBoundaries(cue_in, cue_out, suggested_fade)
