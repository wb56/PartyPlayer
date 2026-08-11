"""Hysteresis-based signal-region detection over PCM level windows."""

from dataclasses import dataclass
import math

from party_player.analysis.levels import PcmLevelWindow


@dataclass(frozen=True, slots=True)
class SignalDetectionSettings:
    """Configurable thresholds for robust signal onset and release."""

    signal_on_dbfs: float = -45.0
    signal_off_dbfs: float = -50.0
    minimum_signal_seconds: float = 0.5
    minimum_silence_seconds: float = 0.3

    def __post_init__(self) -> None:
        if not all(
            math.isfinite(value)
            for value in (
                self.signal_on_dbfs,
                self.signal_off_dbfs,
                self.minimum_signal_seconds,
                self.minimum_silence_seconds,
            )
        ):
            raise ValueError("Signalerkennung benötigt endliche Einstellungswerte")
        if not self.signal_on_dbfs > self.signal_off_dbfs:
            raise ValueError("Die Einschaltschwelle muss über der Ausschaltschwelle liegen")
        if not 0.01 <= self.minimum_signal_seconds <= 30.0:
            raise ValueError("Die Mindest-Signaldauer muss zwischen 10 ms und 30 s liegen")
        if not 0.01 <= self.minimum_silence_seconds <= 30.0:
            raise ValueError("Die Mindest-Stilledauer muss zwischen 10 ms und 30 s liegen")


@dataclass(frozen=True, slots=True)
class SignalRegion:
    """One contiguous region accepted as sustained audio signal."""

    start_seconds: float
    end_seconds: float
    peak: float
    maximum_level_dbfs: float

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.end_seconds - self.start_seconds)


class StreamingSignalDetector:
    """Detect sustained signal while suppressing threshold chatter."""

    def __init__(self, settings: SignalDetectionSettings | None = None) -> None:
        self.settings = settings or SignalDetectionSettings()
        self._candidate_start: float | None = None
        self._candidate_duration = 0.0
        self._active = False
        self._peak = 0.0
        self._maximum_level_dbfs = -math.inf
        self._last_end: float | None = None
        self._release_start: float | None = None
        self._release_duration = 0.0

    def consume(self, window: PcmLevelWindow) -> tuple[SignalRegion, ...]:
        """Consume one chronological level window and return completed regions."""
        if (
            not math.isfinite(window.start_seconds)
            or not math.isfinite(window.duration_seconds)
            or window.duration_seconds <= 0
        ):
            raise ValueError("Pegelfenster benötigt einen gültigen Zeitpunkt und eine Dauer")
        completed: list[SignalRegion] = []
        tolerance = max(0.001, window.duration_seconds * 0.1)
        if self._last_end is not None and abs(window.start_seconds - self._last_end) > tolerance:
            region = self._finish_at(self._last_end)
            if region is not None:
                completed.append(region)

        if self._active:
            if window.level_dbfs < self.settings.signal_off_dbfs:
                if self._release_start is None:
                    self._release_start = window.start_seconds
                    self._release_duration = window.duration_seconds
                else:
                    self._release_duration += window.duration_seconds
                if self._release_duration + 1e-9 >= self.settings.minimum_silence_seconds:
                    region = self._finish_at(self._release_start)
                    if region is not None:
                        completed.append(region)
            else:
                self._release_start = None
                self._release_duration = 0.0
                self._record_level(window)
        elif self._candidate_start is None:
            if window.level_dbfs >= self.settings.signal_on_dbfs:
                self._start_candidate(window)
                self._promote_if_sustained()
        elif window.level_dbfs >= self.settings.signal_off_dbfs:
            self._candidate_duration += window.duration_seconds
            self._record_level(window)
            self._promote_if_sustained()
        else:
            self._reset()

        self._last_end = window.start_seconds + window.duration_seconds
        return tuple(completed)

    def finish(self) -> tuple[SignalRegion, ...]:
        """Complete an active region and discard an unconfirmed candidate."""
        region = self._finish_at(self._last_end)
        self._last_end = None
        return (region,) if region is not None else ()

    def _start_candidate(self, window: PcmLevelWindow) -> None:
        self._candidate_start = window.start_seconds
        self._candidate_duration = window.duration_seconds
        self._record_level(window)

    def _record_level(self, window: PcmLevelWindow) -> None:
        self._peak = max(self._peak, window.peak)
        self._maximum_level_dbfs = max(self._maximum_level_dbfs, window.level_dbfs)

    def _promote_if_sustained(self) -> None:
        if self._candidate_duration + 1e-9 >= self.settings.minimum_signal_seconds:
            self._active = True

    def _finish_at(self, end_seconds: float | None) -> SignalRegion | None:
        region = None
        if self._active and self._candidate_start is not None and end_seconds is not None:
            region = SignalRegion(
                self._candidate_start,
                end_seconds,
                self._peak,
                self._maximum_level_dbfs,
            )
        self._reset()
        return region

    def _reset(self) -> None:
        self._candidate_start = None
        self._candidate_duration = 0.0
        self._active = False
        self._peak = 0.0
        self._maximum_level_dbfs = -math.inf
        self._release_start = None
        self._release_duration = 0.0
