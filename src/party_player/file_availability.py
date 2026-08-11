"""Replaceable file-system checks for queue candidate preparation."""

from dataclasses import dataclass
from pathlib import Path
from time import sleep
from typing import Protocol
from collections.abc import Callable

from party_player.enums import QueueStatus
from party_player.models import Track
from party_player.track_selection import SelectionDecision


class FileAvailabilityChecker(Protocol):
    def evaluate(self, track: Track) -> SelectionDecision: ...


@dataclass(frozen=True, slots=True)
class FileAvailabilityService:
    """Validate audio paths without changing catalog data or media files."""

    supported_formats: frozenset[str] = frozenset({".mp3", ".flac"})
    network_retry_attempts: int = 2
    network_retry_delay_seconds: float = 3.0
    sleeper: Callable[[float], None] = sleep

    def evaluate(
        self,
        track: Track,
        *,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> SelectionDecision:
        if cancelled():
            return self._cancelled()
        path = Path(track.file_path)
        if path.suffix.casefold() not in self.supported_formats:
            return SelectionDecision.reject(
                "UNSUPPORTED_FORMAT",
                terminal_status=QueueStatus.FAILED,
                reason="Das Audioformat wird nicht unterstützt",
            )
        network_path = self._is_network_path(path)
        attempts = self.network_retry_attempts + 1 if network_path else 1
        for attempt in range(attempts):
            if cancelled():
                return self._cancelled()
            try:
                self._check_readable(path)
                if cancelled():
                    return self._cancelled()
                return SelectionDecision.allow()
            except FileNotFoundError:
                if not network_path:
                    return SelectionDecision.reject(
                        "FILE_MISSING",
                        terminal_status=QueueStatus.FAILED,
                        reason="Die Audiodatei wurde nicht gefunden",
                    )
            except PermissionError:
                return SelectionDecision.reject(
                    "FILE_UNREADABLE",
                    terminal_status=QueueStatus.FAILED,
                    reason="Die Audiodatei ist nicht lesbar",
                )
            except OSError:
                if not network_path:
                    return SelectionDecision.reject(
                        "FILE_UNREADABLE",
                        terminal_status=QueueStatus.FAILED,
                        reason="Die Audiodatei ist nicht lesbar",
                    )
            if attempt + 1 < attempts:
                if cancelled():
                    return self._cancelled()
                self.sleeper(max(0.0, self.network_retry_delay_seconds))
        return SelectionDecision.reject(
            "NETWORK_UNAVAILABLE",
            terminal_status=QueueStatus.FAILED,
            reason="Der Netzwerkspeicher ist vorübergehend nicht erreichbar",
        )

    @staticmethod
    def _cancelled() -> SelectionDecision:
        return SelectionDecision.reject(
            "CANDIDATE_CANCELLED",
            reason="Die Kandidatenprüfung wurde durch einen neueren Vorgang ersetzt",
        )

    @staticmethod
    def _check_readable(path: Path) -> None:
        if not path.is_file():
            raise FileNotFoundError(path)
        with path.open("rb") as audio_file:
            audio_file.read(1)

    @staticmethod
    def _is_network_path(path: Path) -> bool:
        text = str(path)
        return text.startswith(("\\\\", "//"))

    def is_local(self, track: Track) -> bool:
        return not self._is_network_path(Path(track.file_path))
