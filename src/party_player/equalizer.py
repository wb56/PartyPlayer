"""Backend-independent equalizer presets and safe runtime resolution."""

from dataclasses import dataclass
from math import isfinite, log10


@dataclass(frozen=True, slots=True)
class EqualizerPreset:
    """A reusable frequency curve independent from VLC's concrete band layout."""

    preset_id: str
    name: str
    preamp_db: float
    curve: tuple[tuple[float, float], ...]
    database_id: int | None = None


@dataclass(frozen=True, slots=True)
class QueueEqualizerContext:
    """The two queue-level assignment sources available during resolution."""

    transient_preset_id: int | None = None
    saved_queue_id: int | None = None


@dataclass(frozen=True, slots=True)
class ResolvedEqualizerPreset:
    """An immutable equalizer snapshot matching one backend's band layout."""

    preset_id: str | None
    name: str
    preamp_db: float
    band_frequencies_hz: tuple[float, ...]
    band_gains_db: tuple[float, ...]
    source: str
    enabled: bool = True

    @classmethod
    def disabled(cls, source: str = "DISABLED") -> "ResolvedEqualizerPreset":
        return cls(None, "Aus", 0.0, (), (), source, False)


BUILTIN_EQUALIZER_PRESETS: dict[str, EqualizerPreset] = {
    "neutral": EqualizerPreset("neutral", "Neutral", 0.0, ()),
    "rock": EqualizerPreset(
        "rock",
        "Rock",
        -3.0,
        ((60.0, 2.0), (170.0, 1.5), (600.0, -1.0), (3000.0, 1.5), (16000.0, 2.0)),
    ),
    "pop": EqualizerPreset(
        "pop",
        "Pop",
        -3.0,
        ((60.0, 1.0), (170.0, 2.0), (1000.0, -0.5), (6000.0, 2.0), (16000.0, 1.0)),
    ),
    "bluesrock": EqualizerPreset(
        "bluesrock",
        "Bluesrock",
        -3.0,
        ((60.0, 1.5), (170.0, 2.0), (600.0, -0.5), (3000.0, 1.0), (12000.0, 1.0)),
    ),
    "dance": EqualizerPreset(
        "dance",
        "Dance",
        -3.0,
        ((60.0, 2.5), (170.0, 1.5), (600.0, -1.0), (3000.0, 1.0), (12000.0, 2.0)),
    ),
}


class EqualizerService:
    """Validate presets and resolve curves to the actual backend frequencies."""

    MIN_GAIN_DB = -20.0
    MAX_GAIN_DB = 20.0

    def validate_preset(self, preset: EqualizerPreset) -> None:
        """Validate a portable preset before persistence or runtime interpolation."""
        frequencies = tuple(frequency for frequency, _gain in preset.curve)
        self._validate_preset(preset, frequencies)
        if len(preset.curve) > 64:
            raise ValueError("Equalizer-Preset enthält zu viele Bänder")
        if len(set(frequencies)) != len(frequencies):
            raise ValueError("Equalizer-Preset enthält doppelte Bandfrequenzen")

    def resolve(
        self,
        preset: EqualizerPreset | None,
        band_frequencies_hz: tuple[float, ...],
        *,
        source: str = "GLOBAL",
    ) -> ResolvedEqualizerPreset:
        if preset is None:
            return ResolvedEqualizerPreset.disabled(source)
        frequencies = tuple(float(value) for value in band_frequencies_hz)
        self._validate_preset(preset, frequencies)
        gains = tuple(self._gain_at_frequency(preset.curve, frequency) for frequency in frequencies)
        highest_boost = max((0.0, *gains))
        safe_preamp = min(preset.preamp_db, -highest_boost)
        return ResolvedEqualizerPreset(
            preset.preset_id,
            preset.name,
            safe_preamp,
            frequencies,
            gains,
            source,
        )

    def builtin(
        self,
        preset_id: str | None,
        band_frequencies_hz: tuple[float, ...],
        *,
        source: str = "GLOBAL",
    ) -> ResolvedEqualizerPreset:
        if preset_id is None or preset_id == "disabled":
            return ResolvedEqualizerPreset.disabled(source)
        try:
            preset = BUILTIN_EQUALIZER_PRESETS[preset_id]
        except KeyError as exc:
            raise ValueError(f"Unbekanntes Equalizer-Preset: {preset_id}") from exc
        return self.resolve(preset, band_frequencies_hz, source=source)

    def validate_resolved(self, preset: ResolvedEqualizerPreset) -> None:
        if not preset.enabled:
            return
        if len(preset.band_frequencies_hz) != len(preset.band_gains_db):
            raise ValueError("Equalizer-Frequenzen und Bandwerte haben unterschiedliche Länge")
        values = (preset.preamp_db, *preset.band_frequencies_hz, *preset.band_gains_db)
        if not all(isfinite(value) for value in values):
            raise ValueError("Equalizer enthält keinen endlichen Zahlenwert")
        if any(frequency <= 0 for frequency in preset.band_frequencies_hz):
            raise ValueError("Equalizer-Bandfrequenzen müssen positiv sein")
        if any(
            gain < self.MIN_GAIN_DB or gain > self.MAX_GAIN_DB
            for gain in (preset.preamp_db, *preset.band_gains_db)
        ):
            raise ValueError("Equalizer-Wert liegt außerhalb des sicheren VLC-Bereichs")
        highest_boost = max((0.0, *preset.band_gains_db))
        if preset.preamp_db > -highest_boost:
            raise ValueError("Equalizer-Preamp schützt nicht vor der größten Bandanhebung")

    def _validate_preset(self, preset: EqualizerPreset, frequencies: tuple[float, ...]) -> None:
        if not preset.preset_id.strip() or not preset.name.strip():
            raise ValueError("Equalizer-Preset benötigt ID und Namen")
        values = (
            preset.preamp_db,
            *frequencies,
            *(item for point in preset.curve for item in point),
        )
        if not all(isfinite(value) for value in values):
            raise ValueError("Equalizer-Preset enthält keinen endlichen Zahlenwert")
        if any(frequency <= 0 for frequency in frequencies):
            raise ValueError("Equalizer-Bandfrequenzen müssen positiv sein")
        if any(frequency <= 0 for frequency, _gain in preset.curve):
            raise ValueError("Equalizer-Kurvenfrequenzen müssen positiv sein")
        if (
            any(
                gain < self.MIN_GAIN_DB or gain > self.MAX_GAIN_DB
                for _frequency, gain in preset.curve
            )
            or not self.MIN_GAIN_DB <= preset.preamp_db <= self.MAX_GAIN_DB
        ):
            raise ValueError("Equalizer-Wert liegt außerhalb des sicheren VLC-Bereichs")

    @staticmethod
    def _gain_at_frequency(curve: tuple[tuple[float, float], ...], frequency: float) -> float:
        if not curve:
            return 0.0
        points = tuple(sorted(curve))
        if frequency <= points[0][0]:
            return points[0][1]
        if frequency >= points[-1][0]:
            return points[-1][1]
        for (lower_frequency, lower_gain), (upper_frequency, upper_gain) in zip(
            points, points[1:], strict=False
        ):
            if lower_frequency <= frequency <= upper_frequency:
                span = log10(upper_frequency) - log10(lower_frequency)
                fraction = (log10(frequency) - log10(lower_frequency)) / span
                return lower_gain + (upper_gain - lower_gain) * fraction
        return 0.0
