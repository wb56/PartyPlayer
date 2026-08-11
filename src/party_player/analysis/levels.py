"""Streaming RMS/dBFS measurements over small PCM time windows."""

from dataclasses import dataclass
import math

from party_player.analysis.base import PcmChunk


@dataclass(frozen=True, slots=True)
class PcmLevelWindow:
    """Measured level for one contiguous source-time window."""

    start_seconds: float
    duration_seconds: float
    rms: float
    level_dbfs: float
    peak: float


class PcmLevelAnalyzer:
    """Accumulate bounded PCM chunks into deterministic level windows."""

    def __init__(
        self,
        *,
        window_seconds: float = 0.1,
        floor_dbfs: float = -120.0,
    ) -> None:
        if not math.isfinite(window_seconds) or not 0.01 <= window_seconds <= 1.0:
            raise ValueError("Das Pegelfenster muss zwischen 10 ms und 1 s liegen")
        if not math.isfinite(floor_dbfs) or floor_dbfs >= 0:
            raise ValueError("Der dBFS-Mindestwert muss endlich und negativ sein")
        self.window_seconds = window_seconds
        self.floor_dbfs = floor_dbfs
        self._sample_rate = 0
        self._channels = 0
        self._target_frames = 0
        self._window_start = 0.0
        self._frames = 0
        self._sample_count = 0
        self._sum_squares = 0.0
        self._peak = 0.0
        self._expected_chunk_start: float | None = None

    def consume(self, chunk: PcmChunk) -> tuple[PcmLevelWindow, ...]:
        """Consume one chunk and return every newly completed level window."""
        if chunk.sample_rate_hz <= 0 or chunk.channels <= 0:
            raise ValueError("PCM-Rate und Kanalzahl müssen positiv sein")
        if self._sample_rate == 0:
            self._set_format(chunk)
        elif (chunk.sample_rate_hz, chunk.channels) != (self._sample_rate, self._channels):
            raise ValueError("PCM-Format darf sich innerhalb einer Analyse nicht ändern")

        completed: list[PcmLevelWindow] = []
        tolerance = 1.5 / self._sample_rate
        if (
            self._expected_chunk_start is not None
            and abs(chunk.start_seconds - self._expected_chunk_start) > tolerance
        ):
            partial = self._flush()
            if partial is not None:
                completed.append(partial)
        if self._frames == 0:
            self._window_start = chunk.start_seconds

        complete_sample_count = chunk.frame_count * self._channels
        samples = chunk.samples[:complete_sample_count]
        sample_index = 0
        while sample_index < len(samples):
            remaining_frames = self._target_frames - self._frames
            available_frames = (len(samples) - sample_index) // self._channels
            take_frames = min(remaining_frames, available_frames)
            end = sample_index + take_frames * self._channels
            for sample in samples[sample_index:end]:
                value = float(sample)
                if not math.isfinite(value):
                    raise ValueError("PCM-Daten enthalten keinen endlichen Samplewert")
                self._sum_squares += value * value
                self._peak = max(self._peak, abs(value))
            taken_samples = end - sample_index
            self._sample_count += taken_samples
            self._frames += take_frames
            sample_index = end
            if self._frames == self._target_frames:
                measured = self._flush()
                assert measured is not None
                completed.append(measured)
                if sample_index < len(samples):
                    self._window_start = (
                        chunk.start_seconds + (sample_index // self._channels) / self._sample_rate
                    )
        self._expected_chunk_start = chunk.start_seconds + chunk.frame_count / self._sample_rate
        return tuple(completed)

    def finish(self) -> tuple[PcmLevelWindow, ...]:
        """Flush a final partial window after the backend reaches EOF."""
        final = self._flush()
        return (final,) if final is not None else ()

    def _set_format(self, chunk: PcmChunk) -> None:
        self._sample_rate = chunk.sample_rate_hz
        self._channels = chunk.channels
        self._target_frames = max(1, round(self.window_seconds * self._sample_rate))

    def _flush(self) -> PcmLevelWindow | None:
        if self._frames == 0 or self._sample_count == 0:
            return None
        rms = math.sqrt(self._sum_squares / self._sample_count)
        level_dbfs = self.floor_dbfs if rms == 0 else max(self.floor_dbfs, 20.0 * math.log10(rms))
        measured = PcmLevelWindow(
            self._window_start,
            self._frames / self._sample_rate,
            rms,
            level_dbfs,
            self._peak,
        )
        self._frames = 0
        self._sample_count = 0
        self._sum_squares = 0.0
        self._peak = 0.0
        return measured
