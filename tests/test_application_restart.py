from pathlib import Path

from party_player.application_restart import build_restart_command, restart_current_application


def test_source_restart_uses_module_entry_point(tmp_path: Path) -> None:
    executable = tmp_path / "python.exe"

    command = build_restart_command(executable, frozen=False)

    assert command.executable == executable.resolve()
    assert command.argv == (str(executable.resolve()), "-m", "party_player")


def test_frozen_restart_reexecutes_only_current_executable(tmp_path: Path) -> None:
    executable = tmp_path / "DeckRelay.exe"

    command = build_restart_command(executable, frozen=True)

    assert command.argv == (str(executable.resolve()),)


def test_restart_boundary_is_injectable_without_replacing_test_process(tmp_path: Path) -> None:
    executable = tmp_path / "python.exe"
    calls: list[tuple[str, ...]] = []

    restart_current_application(
        executable=executable,
        frozen=False,
        replace_process=lambda *arguments: calls.append(arguments),
    )

    resolved = str(executable.resolve())
    assert calls == [(resolved, resolved, "-m", "party_player")]
