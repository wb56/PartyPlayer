"""FFmpeg implementation of bounded offline PCM decoding."""

from array import array
from collections.abc import Iterable, Sequence
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

from party_player.analysis.base import (
    AnalysisSegment,
    AudioFileInfo,
    CancellationToken,
    PcmChunk,
)


class AnalysisBackendUnavailableError(RuntimeError):
    """Raised when FFmpeg or FFprobe cannot be executed."""


class UnsupportedAudioFormatError(ValueError):
    """Raised before spawning a decoder for an unsupported file type."""


class AudioDecodeError(RuntimeError):
    """Raised for invalid probe output or a failed decoder process."""


class FfmpegAudioAnalysisBackend:
    """Decode selected file ranges as normalized interleaved float32 PCM."""

    _EXTENSIONS = frozenset(
        {".aac", ".aiff", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".wma"}
    )

    def __init__(
        self,
        ffmpeg_command: str = "ffmpeg",
        ffprobe_command: str = "ffprobe",
        *,
        frames_per_chunk: int = 4096,
    ) -> None:
        if frames_per_chunk <= 0:
            raise ValueError("frames_per_chunk muss positiv sein")
        self._ffmpeg_command = ffmpeg_command
        self._ffprobe_command = ffprobe_command
        self._frames_per_chunk = frames_per_chunk

    @property
    def name(self) -> str:
        return "ffmpeg"

    def is_available(self) -> bool:
        return (
            self._resolve_command(self._ffmpeg_command) is not None
            and self._resolve_command(self._ffprobe_command) is not None
        )

    def supported_extensions(self) -> frozenset[str]:
        return self._EXTENSIONS

    def probe(self, file_path: Path) -> AudioFileInfo:
        self._validate_file(file_path)
        ffprobe = self._required_command(self._ffprobe_command)
        command = [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name,sample_rate,channels,duration:format=duration",
            "-of",
            "json",
            str(file_path),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                check=False,
                timeout=30.0,
                **self._process_options(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AnalysisBackendUnavailableError(
                f"FFprobe konnte nicht ausgeführt werden: {exc}"
            ) from exc
        if completed.returncode != 0:
            message = completed.stderr.decode(errors="replace").strip()
            raise AudioDecodeError(message or "FFprobe konnte die Audiodatei nicht lesen")
        try:
            payload = json.loads(completed.stdout)
            stream = payload["streams"][0]
            duration = float(stream.get("duration") or payload["format"]["duration"])
            sample_rate = int(stream["sample_rate"])
            channels = int(stream["channels"])
            codec_name = str(stream.get("codec_name") or "")
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AudioDecodeError("FFprobe lieferte unvollständige Audiodaten") from exc
        if not math.isfinite(duration) or duration <= 0 or sample_rate <= 0 or channels <= 0:
            raise AudioDecodeError("FFprobe lieferte ungültige Audiodaten")
        return AudioFileInfo(duration, sample_rate, channels, codec_name)

    def decode_segments(
        self,
        file_path: Path,
        segments: Sequence[AnalysisSegment],
        cancellation: CancellationToken,
    ) -> Iterable[PcmChunk]:
        info = self.probe(file_path)
        ffmpeg = self._required_command(self._ffmpeg_command)
        for segment in segments:
            if cancellation.is_set():
                return
            start, duration = self._validated_segment(segment, info.duration_seconds)
            if duration <= 0:
                continue
            yield from self._decode_segment(ffmpeg, file_path, start, duration, info, cancellation)

    def _decode_segment(
        self,
        ffmpeg: str,
        file_path: Path,
        start: float,
        duration: float,
        info: AudioFileInfo,
        cancellation: CancellationToken,
    ) -> Iterable[PcmChunk]:
        command = [
            ffmpeg,
            "-v",
            "error",
            "-ss",
            format(start, ".9g"),
            "-i",
            str(file_path),
            "-t",
            format(duration, ".9g"),
            "-vn",
            "-f",
            "f32le",
            "-acodec",
            "pcm_f32le",
            "-ar",
            str(info.sample_rate_hz),
            "-ac",
            str(info.channels),
            "pipe:1",
        ]
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **self._process_options(),
            )
        except OSError as exc:
            raise AnalysisBackendUnavailableError(
                f"FFmpeg konnte nicht ausgeführt werden: {exc}"
            ) from exc
        assert process.stdout is not None
        frame_offset = 0
        bytes_per_chunk = self._frames_per_chunk * info.channels * 4
        try:
            while True:
                if cancellation.is_set():
                    process.terminate()
                    return
                raw = process.stdout.read(bytes_per_chunk)
                if not raw:
                    break
                complete_bytes = len(raw) - (len(raw) % 4)
                samples = array("f")
                samples.frombytes(raw[:complete_bytes])
                if sys.byteorder != "little":
                    samples.byteswap()
                chunk = PcmChunk(
                    start + frame_offset / info.sample_rate_hz,
                    info.sample_rate_hz,
                    info.channels,
                    tuple(samples),
                )
                frame_offset += chunk.frame_count
                if chunk.frame_count:
                    yield chunk
            return_code = process.wait()
            if return_code != 0:
                assert process.stderr is not None
                message = process.stderr.read().decode(errors="replace").strip()
                raise AudioDecodeError(message or f"FFmpeg wurde mit Code {return_code} beendet")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()

    def _validate_file(self, file_path: Path) -> None:
        if file_path.suffix.lower() not in self._EXTENSIONS:
            raise UnsupportedAudioFormatError(
                f"Nicht unterstütztes Audioformat: {file_path.suffix or '(ohne Endung)'}"
            )
        if not file_path.is_file():
            raise FileNotFoundError(file_path)

    @staticmethod
    def _validated_segment(segment: AnalysisSegment, file_duration: float) -> tuple[float, float]:
        start = float(segment.start_seconds)
        duration = float(segment.duration_seconds)
        if not math.isfinite(start) or not math.isfinite(duration) or start < 0 or duration <= 0:
            raise ValueError(
                "Analysesegmente benötigen einen gültigen Start und eine positive Dauer"
            )
        if start >= file_duration:
            return start, 0.0
        return start, min(duration, file_duration - start)

    @staticmethod
    def _resolve_command(command: str) -> str | None:
        candidate = Path(command)
        if candidate.parent != Path("."):
            return str(candidate) if candidate.is_file() else None
        installed = shutil.which(command)
        return installed

    def _required_command(self, command: str) -> str:
        resolved = self._resolve_command(command)
        if resolved is None:
            raise AnalysisBackendUnavailableError(
                f"{command} wurde nicht gefunden; FFmpeg muss installiert oder konfiguriert sein"
            )
        return resolved

    @staticmethod
    def _process_options() -> dict[str, Any]:
        if sys.platform == "win32":
            return {"creationflags": subprocess.CREATE_NO_WINDOW}
        return {}
