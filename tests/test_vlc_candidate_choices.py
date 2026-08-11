"""Valid VLC candidate presentation without creating Tk widgets."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from party_player.system_dependencies import DependencyStatus, VlcDependencyInfo
from party_player.ui.external_programs_dialog import valid_vlc_candidate_choices


def test_choices_include_only_valid_vlc_candidates_in_rank_order(tmp_path: Path) -> None:
    first = VlcDependencyInfo(
        DependencyStatus.AVAILABLE,
        installation_directory=tmp_path / "VLC One",
        source="standard",
        version="3.0.21",
    )
    invalid = VlcDependencyInfo(
        DependencyStatus.INVALID,
        installation_directory=tmp_path / "Broken",
        source="user",
    )
    second = VlcDependencyInfo(
        DependencyStatus.AVAILABLE,
        installation_directory=tmp_path / "VLC Two",
        source="registry",
        version="3.0.23",
    )
    report = cast(
        Any,
        SimpleNamespace(
            resolution=SimpleNamespace(
                vlc=SimpleNamespace(attempts=(first, invalid, second, first))
            )
        ),
    )

    choices = valid_vlc_candidate_choices(report)

    assert [choice.directory for choice in choices] == [
        str(tmp_path / "VLC One"),
        str(tmp_path / "VLC Two"),
    ]
    assert choices[0].label.startswith("1. standard · 3.0.21")
    assert choices[1].label.startswith("3. registry · 3.0.23")
