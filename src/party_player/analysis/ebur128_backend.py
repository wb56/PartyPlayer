"""FFmpeg EBU R128/ITU-R BS.1770 loudness measurement backend."""

import math
from pathlib import Path
import re
import subprocess

from party_player.analysis.ffmpeg_backend import (
    AnalysisBackendUnavailableError,
    AudioDecodeError,
    FfmpegAudioAnalysisBackend,
)
from party_player.analysis.loudness_backend import LoudnessAnalysisResult


class FfmpegEbur128Backend:
    """Measure integrated loudness, LRA and true peak for a complete file."""

    _INTEGRATED = re.compile(r"^\s*I:\s*(-?\d+(?:\.\d+)?)\s+LUFS\s*$", re.MULTILINE)
    _RANGE = re.compile(r"^\s*LRA:\s*(\d+(?:\.\d+)?)\s+LU\s*$", re.MULTILINE)
    _TRUE_PEAK = re.compile(
        r"^\s*Peak:\s*(-?\d+(?:\.\d+)?)\s+dBFS\s*$",
        re.MULTILINE,
    )

    def __init__(self, ffmpeg_command: str = "ffmpeg") -> None:
        self._ffmpeg_command = ffmpeg_command

    @property
    def name(self) -> str:
        return "ffmpeg-ebur128"

    def is_available(self) -> bool:
        return FfmpegAudioAnalysisBackend._resolve_command(self._ffmpeg_command) is not None

    def analyze(self, file_path: Path) -> LoudnessAnalysisResult:
        if not file_path.is_file():
            raise FileNotFoundError(file_path)
        ffmpeg = FfmpegAudioAnalysisBackend._resolve_command(self._ffmpeg_command)
        if ffmpeg is None:
            raise AnalysisBackendUnavailableError(f"{self._ffmpeg_command} wurde nicht gefunden")
        command = [
            ffmpeg,
            "-hide_banner",
            "-nostats",
            "-i",
            str(file_path),
            "-filter_complex",
            "ebur128=peak=true",
            "-f",
            "null",
            "-",
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                check=False,
                timeout=3600.0,
                **FfmpegAudioAnalysisBackend._process_options(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AnalysisBackendUnavailableError(
                f"FFmpeg-Lautheitsanalyse konnte nicht ausgeführt werden: {exc}"
            ) from exc
        output = completed.stderr.decode(errors="replace")
        if completed.returncode != 0:
            raise AudioDecodeError(output.strip() or "FFmpeg-Lautheitsanalyse ist fehlgeschlagen")
        return self.parse_summary(output)

    def parse_summary(self, output: str) -> LoudnessAnalysisResult:
        """Parse the final EBU R128 summary, ignoring per-frame log lines."""
        integrated = self._last_value(self._INTEGRATED, output)
        loudness_range = self._last_value(self._RANGE, output)
        true_peak = self._last_value(self._TRUE_PEAK, output)
        if not all(math.isfinite(value) for value in (integrated, loudness_range, true_peak)):
            raise AudioDecodeError("FFmpeg lieferte keine vollständige EBU-R128-Zusammenfassung")
        return LoudnessAnalysisResult(
            integrated,
            loudness_range,
            true_peak,
            "EBU R128 / ITU-R BS.1770",
            self.name,
        )

    @staticmethod
    def _last_value(pattern: re.Pattern[str], output: str) -> float:
        matches = pattern.findall(output)
        return float(matches[-1]) if matches else math.nan
