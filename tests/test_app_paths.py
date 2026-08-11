from pathlib import Path

from party_player.core.paths import AppPaths


def test_runtime_directories_include_standard_backup_target(tmp_path: Path) -> None:
    paths = AppPaths(tmp_path)

    paths.ensure_runtime_directories()

    assert paths.backups_directory == tmp_path / "data" / "Backups"
    assert paths.backups_directory.is_dir()
