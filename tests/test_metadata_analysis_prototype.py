from array import array
from datetime import datetime, timezone
import math
from pathlib import Path
import subprocess
from time import monotonic, sleep
import wave

import pytest

from party_player.metadata_analysis_contracts import (
    FileSnapshot,
    MetadataAnalysisBackendKind,
    MetadataAnalysisJob,
    MetadataAnalysisKind,
    MetadataAnalysisOutcome,
)
from party_player.metadata_tempo_backend import (
    _combine_tempos,
    _features,
    _tempo,
    select_ranges,
)
from party_player.metadata_analysis_supervisor import MetadataAnalysisProcessSupervisor


FFMPEG_BIN = Path(".tools/ffmpeg/ffmpeg-8.1.2-essentials_build/bin")
FFMPEG = FFMPEG_BIN / "ffmpeg.exe"
FFPROBE = FFMPEG_BIN / "ffprobe.exe"


def click_samples(bpm: float, seconds: float = 45.0, sample_rate: int = 11_025) -> array:
    samples = array("f", [0.0]) * round(seconds * sample_rate)
    interval = sample_rate * 60.0 / bpm
    for beat in range(round(seconds * bpm / 60.0)):
        offset = round(beat * interval)
        for index in range(min(120, len(samples) - offset)):
            samples[offset + index] += 0.8 * math.exp(-index / 25.0)
    return samples


@pytest.mark.parametrize("reference", [60.0, 100.0, 140.0, 200.0])
def test_onset_autocorrelation_detects_synthetic_click_tempo(reference: float) -> None:
    bpm, alternative, confidence, _stability = _tempo(_features(click_samples(reference)).onset)
    assert min(abs(bpm - reference), abs(alternative - reference)) <= 2.0
    assert 20.0 <= bpm <= 300.0
    assert 20.0 <= alternative <= 300.0
    assert confidence > 0.1


def test_silence_has_no_tempo() -> None:
    assert _tempo(_features(array("f", [0.0]) * 20_000).onset)[0] == 0.0


def test_distributed_tempo_change_uses_median_and_reduces_stability() -> None:
    estimates = tuple(
        _tempo(_features(click_samples(reference, 30.0)).onset)
        for reference in (100.0, 120.0, 150.0)
    )
    bpm, alternative, confidence, stability = _combine_tempos(estimates)
    assert min(abs(bpm - 120.0), abs(alternative - 120.0)) <= 2.0
    assert 0.0 < confidence < 0.9
    assert stability < 0.65


def test_range_strategies_are_bounded() -> None:
    assert select_ranges(20.0, "distributed") == select_ranges(20.0, "full")
    assert len(select_ranges(240.0, "middle")) == 1
    assert len(select_ranges(240.0, "distributed")) == 3
    assert len(select_ranges(240.0, "begin_middle_end")) == 3


def write_wave(path: Path, samples: array, sample_rate: int = 11_025) -> None:
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(sample_rate)
        target.writeframes(
            b"".join(
                round(max(-1.0, min(1.0, value)) * 32767).to_bytes(2, "little", signed=True)
                for value in samples
            )
        )


def product_job(path: Path, suffix: str = "") -> MetadataAnalysisJob:
    return MetadataAnalysisJob(
        f"product{suffix}",
        1,
        1,
        FileSnapshot.capture(str(path)),
        "tempo",
        "ffmpeg-onset-acf-v0.1",
        (MetadataAnalysisKind.BPM, MetadataAnalysisKind.ENERGY),
        0,
        30.0,
        datetime.now(timezone.utc).isoformat(),
        MetadataAnalysisBackendKind.FFMPEG_TEMPO,
        (
            ("ffmpeg", str(FFMPEG.resolve())),
            ("ffprobe", str(FFPROBE.resolve())),
            ("segment_strategy", "middle"),
        ),
    )


def await_result(supervisor: MetadataAnalysisProcessSupervisor):
    deadline = monotonic() + 30.0
    while monotonic() < deadline:
        result = supervisor.poll()
        if result is not None:
            return result
        sleep(0.01)
    raise AssertionError("Kein Analyseergebnis")


@pytest.mark.skipif(not FFMPEG.is_file() or not FFPROBE.is_file(), reason="lokales FFmpeg fehlt")
@pytest.mark.parametrize(
    ("suffix", "codec"),
    [
        (".mp3", ("-codec:a", "libmp3lame", "-b:a", "128k")),
        (".flac", ("-codec:a", "flac")),
        (".vbr.mp3", ("-codec:a", "libmp3lame", "-q:a", "4")),
    ],
)
def test_real_formats_run_in_spawn_process(
    tmp_path: Path, suffix: str, codec: tuple[str, ...]
) -> None:
    source = tmp_path / "source.wav"
    encoded = tmp_path / f"tempo{suffix}"
    write_wave(source, click_samples(120.0, 20.0))
    subprocess.run(
        [str(FFMPEG), "-v", "error", "-y", "-i", str(source), *codec, str(encoded)],
        check=True,
        timeout=30,
    )
    supervisor = MetadataAnalysisProcessSupervisor()
    try:
        supervisor.submit(product_job(encoded, suffix))
        result = await_result(supervisor)
        assert result.outcome is MetadataAnalysisOutcome.SUCCESS
        bpm = float(result.suggestions[0].canonical_value)
        alternative = float(result.suggestions[1].canonical_value)
        assert min(abs(bpm - 120.0), abs(alternative - 120.0)) <= 2.0
        assert result.technical_metrics
        assert result.analyzed_ranges
    finally:
        supervisor.close()
