"""Productive dependency-free FFmpeg tempo and technical-energy backend."""

from __future__ import annotations

from array import array
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import statistics
import subprocess
import sys
from time import monotonic
from typing import Any

from party_player.metadata_analysis_contracts import (
    AnalyzedAudioRange,
    MetadataAnalysisJob,
    MetadataAnalysisOutcome,
    MetadataAnalysisResult,
    MetadataAnalysisSource,
    MetadataFieldSuggestion,
    TechnicalAudioMetric,
)
from party_player.metadata_analysis_profiles import (
    ALGORITHM_VERSION,
    ConfidenceBand,
    confidence_band,
)


SAMPLE_RATE = 11_025
ENVELOPE_HZ = 100


@dataclass(frozen=True, slots=True)
class _SegmentFeatures:
    onset: tuple[float, ...]
    rms_mean: float
    rms_std: float
    peak: float
    transient_density: float


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _process_options() -> dict[str, Any]:
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def _option(job: MetadataAnalysisJob, name: str, default: str) -> str:
    value = dict(job.technical_options).get(name, default)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Technische Option {name} ist ungültig")
    return value


def _probe_duration(job: MetadataAnalysisJob, cancellation: object) -> float:
    command = [
        _option(job, "ffprobe", "ffprobe"),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        job.input_snapshot.normalized_path,
    ]
    completed = _communicate(command, cancellation, 30.0)
    payload = json.loads(completed.decode("utf-8"))
    duration = float(payload["format"]["duration"])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("FFprobe lieferte keine gültige Dauer")
    return duration


def _communicate(command: list[str], cancellation: object, timeout: float) -> bytes:
    process: subprocess.Popen[bytes] = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **_process_options()
    )
    deadline = monotonic() + timeout
    try:
        while True:
            if cancellation.is_set():  # type: ignore[attr-defined]
                process.terminate()
                raise InterruptedError("Analyse abgebrochen")
            try:
                stdout, stderr = process.communicate(timeout=0.1)
                if process.returncode != 0:
                    raise RuntimeError(stderr.decode(errors="replace")[:500])
                return stdout
            except subprocess.TimeoutExpired:
                if monotonic() >= deadline:
                    process.terminate()
                    raise TimeoutError("FFmpeg-Teiloperation überschritt das Zeitlimit")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def select_ranges(duration: float, strategy: str) -> tuple[AnalyzedAudioRange, ...]:
    if duration <= 0:
        return ()
    if strategy == "full":
        return (AnalyzedAudioRange(0.0, duration),)
    if strategy == "middle":
        length = min(90.0, duration)
        return (AnalyzedAudioRange(max(0.0, (duration - length) / 2), length),)
    if duration <= 30.0:
        return (AnalyzedAudioRange(0.0, duration),)
    length = min(30.0, duration / 3.0)
    if strategy == "begin_middle_end":
        fractions = (0.03, 0.5, 0.97)
    else:
        fractions = (0.15, 0.5, 0.85)
    starts = tuple(
        min(max(0.0, duration * fraction - length / 2), duration - length) for fraction in fractions
    )
    return tuple(AnalyzedAudioRange(start, length) for start in dict.fromkeys(starts))


def _decode(
    job: MetadataAnalysisJob, region: AnalyzedAudioRange, cancellation: object
) -> array[float]:
    command = [
        _option(job, "ffmpeg", "ffmpeg"),
        "-v",
        "error",
        "-ss",
        format(region.start_seconds, ".6f"),
        "-i",
        job.input_snapshot.normalized_path,
        "-t",
        format(region.duration_seconds, ".6f"),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-f",
        "f32le",
        "pipe:1",
    ]
    samples = array("f")
    samples.frombytes(_communicate(command, cancellation, region.duration_seconds + 30.0))
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


def _features(samples: array[float]) -> _SegmentFeatures:
    window = max(1, SAMPLE_RATE // ENVELOPE_HZ)
    rms = []
    peak = 0.0
    for offset in range(0, len(samples), window):
        block = samples[offset : offset + window]
        if not block:
            continue
        squares = sum(float(value) * float(value) for value in block)
        rms.append(math.sqrt(squares / len(block)))
        peak = max(peak, max(abs(float(value)) for value in block))
    if len(rms) < 2:
        return _SegmentFeatures((), 0.0, 0.0, peak, 0.0)
    onset = [max(0.0, rms[index] - rms[index - 1]) for index in range(1, len(rms))]
    baseline = statistics.median(onset)
    spread = statistics.median(abs(value - baseline) for value in onset) or 1e-9
    threshold = baseline + 3.0 * spread
    threshold = max(threshold, max(onset, default=0.0) * 0.1)
    refractory = max(1, round(ENVELOPE_HZ * 0.12))
    peak_count = 0
    next_allowed = 0
    for index in range(1, len(onset) - 1):
        if (
            index >= next_allowed
            and onset[index] > threshold
            and onset[index] >= onset[index - 1]
            and onset[index] > onset[index + 1]
        ):
            peak_count += 1
            next_allowed = index + refractory
    density = peak_count / (len(onset) / ENVELOPE_HZ)
    mean = statistics.fmean(onset)
    centered = tuple(value - mean for value in onset)
    return _SegmentFeatures(
        centered,
        statistics.fmean(rms),
        statistics.pstdev(rms),
        peak,
        density,
    )


def _tempo(onset: tuple[float, ...]) -> tuple[float, float, float, float]:
    if not onset or max(onset, default=0.0) <= 1e-8:
        return 0.0, 0.0, 0.0, 0.0
    minimum_lag = round(ENVELOPE_HZ * 60 / 300)
    maximum_lag = min(len(onset) // 2, round(ENVELOPE_HZ * 60 / 20))
    energy = sum(value * value for value in onset)
    scores: list[tuple[float, int]] = []
    for lag in range(minimum_lag, maximum_lag + 1):
        numerator = sum(onset[index] * onset[index - lag] for index in range(lag, len(onset)))
        score = max(0.0, numerator / max(energy, 1e-12))
        bpm = 60.0 * ENVELOPE_HZ / lag
        harmonic = score
        double_lag = lag * 2
        if double_lag <= maximum_lag:
            harmonic += 0.35 * max(
                0.0,
                sum(
                    onset[index] * onset[index - double_lag]
                    for index in range(double_lag, len(onset))
                )
                / max(energy, 1e-12),
            )
        if 55.0 <= bpm <= 190.0:
            harmonic *= 1.05
        scores.append((harmonic, lag))
    scores.sort(reverse=True)
    best_score, best_lag = scores[0]
    bpm = 60.0 * ENVELOPE_HZ / best_lag
    alternative = bpm * 2 if bpm * 2 <= 300 else bpm / 2
    runner_up = next((score for score, lag in scores[1:] if abs(lag - best_lag) > 2), 0.0)
    separation = max(0.0, best_score - runner_up) / max(best_score, 1e-9)
    confidence = min(1.0, max(0.0, 0.65 * best_score + 0.35 * separation))
    return bpm, alternative, confidence, best_score


def _combine_tempos(
    estimates: tuple[tuple[float, float, float, float], ...],
) -> tuple[float, float, float, float]:
    valid = tuple(item for item in estimates if item[0] > 0.0)
    if not valid:
        return 0.0, 0.0, 0.0, 0.0
    ordered = sorted(valid, key=lambda item: item[0])
    total_weight = sum(max(item[2], 0.05) for item in ordered)
    accumulated = 0.0
    selected = ordered[-1]
    for item in ordered:
        accumulated += max(item[2], 0.05)
        if accumulated >= total_weight / 2:
            selected = item
            break
    bpm = selected[0]
    alternative = bpm * 2 if bpm * 2 <= 300 else bpm / 2
    relative_spread = statistics.pstdev(item[0] for item in valid) / max(
        statistics.fmean(item[0] for item in valid), 1e-9
    )
    agreement = max(0.0, 1.0 - min(1.0, relative_spread * 3.0))
    confidence = statistics.fmean(item[2] for item in valid) * (0.55 + 0.45 * agreement)
    local_stability = statistics.fmean(min(1.0, item[3]) for item in valid)
    return bpm, alternative, min(1.0, confidence), min(local_stability, agreement)


class FfmpegTempoAnalysisBackend:
    """Analyze bounded mono PCM with onset-envelope autocorrelation."""

    def analyze(self, job: MetadataAnalysisJob, cancellation: object) -> MetadataAnalysisResult:
        started_at = _now()
        try:
            if Path(job.input_snapshot.normalized_path).suffix.lower() not in {".mp3", ".flac"}:
                return self._failure(
                    job,
                    started_at,
                    MetadataAnalysisOutcome.UNSUPPORTED_FORMAT,
                    "UNSUPPORTED_FORMAT",
                    "Produktive Tempoanalyse unterstützt MP3 und FLAC.",
                )
            duration = _probe_duration(job, cancellation)
            strategy = _option(job, "segment_strategy", "distributed")
            ranges = select_ranges(duration, strategy)
            features = tuple(_features(_decode(job, region, cancellation)) for region in ranges)
            bpm, alternative, confidence, stability = _combine_tempos(
                tuple(_tempo(feature.onset) for feature in features)
            )
            if bpm == 0.0:
                return MetadataAnalysisResult(
                    job.job_id,
                    job.run_id,
                    job.track_id,
                    job.input_snapshot,
                    job.analysis_profile,
                    job.analysis_version,
                    started_at,
                    _now(),
                    MetadataAnalysisOutcome.SUCCESS,
                    analyzed_ranges=ranges,
                    warnings=("Kein belastbarer Rhythmus erkannt; kein BPM-Vorschlag.",),
                    backend_name="ffmpeg-onset-autocorrelation",
                    backend_version=ALGORITHM_VERSION,
                )
            rms_mean = statistics.fmean(feature.rms_mean for feature in features)
            rms_std = statistics.fmean(feature.rms_std for feature in features)
            peak = max(feature.peak for feature in features)
            transient_density = statistics.fmean(feature.transient_density for feature in features)
            crest = peak / max(rms_mean, 1e-9)
            experimental_energy = min(
                1.0,
                max(
                    0.0,
                    0.45 * min(rms_mean / 0.25, 1.0)
                    + 0.35 * min(transient_density / 5.0, 1.0)
                    + 0.2 * min(rms_std / 0.15, 1.0),
                ),
            )
            warnings = ["Halb-/Doppeltempo-Alternative wird separat ausgewiesen."]
            if stability < 0.65:
                warnings.append(
                    "Die verteilten Ausschnitte zeigen ein wechselndes oder instabiles Tempo."
                )
            band = confidence_band(confidence)
            suggestions = (
                ()
                if band is ConfidenceBand.LOW
                else (
                    MetadataFieldSuggestion(
                        "bpm", round(bpm, 2), MetadataAnalysisSource.AUDIO_ANALYSIS, confidence
                    ),
                    MetadataFieldSuggestion(
                        "alternative_bpm",
                        round(alternative, 2),
                        MetadataAnalysisSource.AUDIO_ANALYSIS,
                        confidence * 0.8,
                    ),
                    *(
                        (
                            MetadataFieldSuggestion(
                                "energy_experimental",
                                round(experimental_energy * 100),
                                MetadataAnalysisSource.AUDIO_ANALYSIS,
                                confidence * 0.7,
                            ),
                        )
                        if "ENERGY" in {kind.value for kind in job.requested_kinds}
                        else ()
                    ),
                )
            )
            if band is ConfidenceBand.LOW:
                warnings.append("Konfidenz unter 0,55; kein regulärer BPM-Vorschlag erzeugt.")
            elif band is ConfidenceBand.MEDIUM:
                warnings.append("Mittlere Konfidenz; fachliche Prüfung erforderlich.")
            return MetadataAnalysisResult(
                job.job_id,
                job.run_id,
                job.track_id,
                job.input_snapshot,
                job.analysis_profile,
                job.analysis_version,
                started_at,
                _now(),
                MetadataAnalysisOutcome.SUCCESS,
                suggestions=suggestions,
                analyzed_ranges=ranges,
                technical_metrics=(
                    TechnicalAudioMetric("rms_mean", rms_mean, "linear"),
                    TechnicalAudioMetric("rms_variability", rms_std, "linear"),
                    TechnicalAudioMetric("peak", peak, "linear"),
                    TechnicalAudioMetric("crest_factor", crest, "ratio"),
                    TechnicalAudioMetric("transient_density", transient_density, "events/s"),
                    TechnicalAudioMetric("bpm", bpm, "BPM"),
                    TechnicalAudioMetric(
                        "energy_experimental", experimental_energy * 100, "percent"
                    ),
                ),
                rhythm_stability=min(1.0, stability),
                warnings=tuple(warnings),
                backend_name="ffmpeg-onset-autocorrelation",
                backend_version=ALGORITHM_VERSION,
            )
        except InterruptedError:
            return self._failure(
                job,
                started_at,
                MetadataAnalysisOutcome.CANCELLED,
                "CANCELLED",
                "Analyse wurde abgebrochen.",
            )
        except FileNotFoundError:
            return self._failure(
                job,
                started_at,
                MetadataAnalysisOutcome.BACKEND_UNAVAILABLE,
                "BACKEND_NOT_FOUND",
                "FFmpeg oder FFprobe ist nicht verfügbar.",
            )
        except TimeoutError as exc:
            return self._failure(
                job, started_at, MetadataAnalysisOutcome.TIMEOUT, "TIMEOUT", str(exc)
            )
        except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            safe_text = str(exc).replace(job.input_snapshot.normalized_path, "<Eingabedatei>")
            return self._failure(
                job,
                started_at,
                MetadataAnalysisOutcome.ANALYSIS_ERROR,
                "ANALYSIS_ERROR",
                safe_text[:500],
            )

    @staticmethod
    def _failure(
        job: MetadataAnalysisJob,
        started_at: str,
        outcome: MetadataAnalysisOutcome,
        code: str,
        text: str,
        ranges: tuple[AnalyzedAudioRange, ...] = (),
    ) -> MetadataAnalysisResult:
        return MetadataAnalysisResult(
            job.job_id,
            job.run_id,
            job.track_id,
            job.input_snapshot,
            job.analysis_profile,
            job.analysis_version,
            started_at,
            _now(),
            outcome,
            analyzed_ranges=ranges,
            error_code=code,
            error_text=text,
            backend_name="ffmpeg-onset-autocorrelation",
            backend_version=ALGORITHM_VERSION,
        )
