"""Controlled process replacement after a successfully committed restore."""

from collections.abc import Callable
from dataclasses import dataclass
import os
from pathlib import Path
import sys


@dataclass(frozen=True, slots=True)
class RestartCommand:
    executable: Path
    argv: tuple[str, ...]


def build_restart_command(executable: str | Path, *, frozen: bool) -> RestartCommand:
    """Describe the exact current application start mode without executing it."""
    resolved = Path(executable).resolve()
    argv = (str(resolved),) if frozen else (str(resolved), "-m", "party_player")
    return RestartCommand(resolved, argv)


def restart_current_application(
    *,
    executable: str | Path | None = None,
    frozen: bool | None = None,
    replace_process: Callable[..., object] = os.execl,
) -> None:
    """Replace this fully cleaned-up process using its original deployment mode."""
    command = build_restart_command(
        executable or sys.executable,
        frozen=getattr(sys, "frozen", False) if frozen is None else frozen,
    )
    replace_process(str(command.executable), *command.argv)
