"""Timeout-bounded source reachability checks for cached UI status."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from threading import Event, Thread


class SourceAvailabilityState(StrEnum):
    UNKNOWN = "UNBEKANNT"
    CHECKING = "PRÜFUNG LÄUFT"
    AVAILABLE = "ERREICHBAR"
    UNAVAILABLE = "NICHT ERREICHBAR"
    TIMEOUT = "ZEITÜBERSCHREITUNG"
    EMPTY = "LEER"


@dataclass(frozen=True, slots=True)
class SourceAvailabilitySnapshot:
    path: str
    state: SourceAvailabilityState
    checked_at: str = ""
    reason: str = ""


class SourceAvailabilityMonitor:
    def __init__(
        self,
        *,
        timeout_seconds: float = 2.0,
        probe: Callable[[Path], None] | None = None,
    ) -> None:
        self._timeout = max(0.05, timeout_seconds)
        self._probe = probe or self._probe_readable

    def check(self, file_path: str) -> SourceAvailabilitySnapshot:
        """Bound caller time even when an operating-system network lookup hangs."""
        path = Path(file_path)
        completed = Event()
        result: dict[str, object] = {}

        def worker() -> None:
            try:
                self._probe(path)
            except (FileNotFoundError, PermissionError, OSError) as exc:
                result["error"] = exc
            finally:
                completed.set()

        Thread(target=worker, name="source-availability-probe", daemon=True).start()
        checked_at = datetime.now().astimezone().isoformat()
        if not completed.wait(self._timeout):
            return SourceAvailabilitySnapshot(
                file_path,
                SourceAvailabilityState.TIMEOUT,
                checked_at,
                "Quelldateiprüfung überschritt ihr Zeitlimit",
            )
        error = result.get("error")
        if isinstance(error, BaseException):
            return SourceAvailabilitySnapshot(
                file_path,
                SourceAvailabilityState.UNAVAILABLE,
                checked_at,
                str(error),
            )
        return SourceAvailabilitySnapshot(file_path, SourceAvailabilityState.AVAILABLE, checked_at)

    @staticmethod
    def _probe_readable(path: Path) -> None:
        if not path.is_file():
            raise FileNotFoundError(path)
        with path.open("rb") as source:
            source.read(1)
