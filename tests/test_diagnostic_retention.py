from pathlib import Path

from party_player.diagnostic_retention import retain_latest


def test_diagnostic_retention_keeps_only_newest_files(tmp_path: Path) -> None:
    for index in range(6):
        (tmp_path / f"report-{index}.txt").write_text(str(index), encoding="utf-8")

    retain_latest(tmp_path, "report-*.txt", 3)

    assert [path.name for path in sorted(tmp_path.glob("report-*.txt"))] == [
        "report-3.txt",
        "report-4.txt",
        "report-5.txt",
    ]
