from pathlib import Path
from subprocess import CompletedProcess

import pytest

from party_player.analysis.ebur128_backend import FfmpegEbur128Backend
from party_player.analysis.ffmpeg_backend import AudioDecodeError


SUMMARY = """
[Parsed_ebur128_0] Summary:

  Integrated loudness:
    I:         -14.2 LUFS
    Threshold: -24.2 LUFS

  Loudness range:
    LRA:         6.4 LU

  True peak:
    Peak:       -1.1 dBFS
"""


def test_parser_reads_final_ebu_r128_summary() -> None:
    result = FfmpegEbur128Backend().parse_summary(SUMMARY)

    assert result.integrated_loudness_lufs == -14.2
    assert result.loudness_range_lu == 6.4
    assert result.true_peak_dbfs == -1.1
    assert result.method == "EBU R128 / ITU-R BS.1770"
    assert result.backend_name == "ffmpeg-ebur128"


def test_parser_rejects_incomplete_summary() -> None:
    with pytest.raises(AudioDecodeError, match="Zusammenfassung"):
        FfmpegEbur128Backend().parse_summary("I: -14.0 LUFS")


def test_backend_runs_ffmpeg_without_writing_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "track.flac"
    source.write_bytes(b"audio")
    commands: list[list[str]] = []
    monkeypatch.setattr(
        "party_player.analysis.ebur128_backend.FfmpegAudioAnalysisBackend._resolve_command",
        lambda _command: "ffmpeg",
    )

    def run(command: list[str], **_kwargs: object) -> CompletedProcess[bytes]:
        commands.append(command)
        return CompletedProcess(command, 0, b"", SUMMARY.encode())

    monkeypatch.setattr("party_player.analysis.ebur128_backend.subprocess.run", run)

    result = FfmpegEbur128Backend().analyze(source)

    assert result.integrated_loudness_lufs == -14.2
    assert commands[0][-3:] == ["-f", "null", "-"]
    assert source.read_bytes() == b"audio"
