"""Application path definitions."""

from dataclasses import dataclass
from pathlib import Path
import sys


@dataclass(frozen=True, slots=True)
class AppPaths:
    """Runtime paths used by the application."""

    root: Path

    @classmethod
    def for_project(cls) -> "AppPaths":
        """Resolve paths relative to the executable or development directory."""
        if getattr(sys, "frozen", False):
            return cls(Path(sys.executable).resolve().parent)
        return cls(Path.cwd())

    @property
    def database_file(self) -> Path:
        return self.root / "data" / "party_player.db"

    @property
    def log_file(self) -> Path:
        return self.root / "logs" / "party_player.log"

    @property
    def diagnostics_directory(self) -> Path:
        return self.root / "diagnostics"

    @property
    def backups_directory(self) -> Path:
        return self.root / "data" / "Backups"

    def ensure_runtime_directories(self) -> None:
        """Create directories for mutable application data."""
        self.database_file.parent.mkdir(parents=True, exist_ok=True)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self.diagnostics_directory.mkdir(parents=True, exist_ok=True)
        self.backups_directory.mkdir(parents=True, exist_ok=True)
