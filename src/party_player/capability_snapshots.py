"""Generation-safe boundary between active and next-start runtime capabilities."""

from dataclasses import dataclass
from threading import Lock

from party_player.system_dependencies import SystemDiagnosticSnapshot


@dataclass(frozen=True, slots=True)
class CapabilitySnapshotView:
    active: SystemDiagnosticSnapshot
    pending: SystemDiagnosticSnapshot | None
    pending_generation: int
    restart_required: bool


class CapabilitySnapshotState:
    """Keep the process-bound start snapshot separate from validated proposals."""

    def __init__(self, active: SystemDiagnosticSnapshot) -> None:
        self._active = active
        self._pending: SystemDiagnosticSnapshot | None = None
        self._pending_generation = 0
        self._lock = Lock()

    @property
    def active(self) -> SystemDiagnosticSnapshot:
        return self._active

    def publish_pending(self, generation: int, snapshot: SystemDiagnosticSnapshot) -> bool:
        """Accept only a proposal newer than every previously observed proposal."""
        if generation <= 0:
            raise ValueError("Capability generation must be positive")
        with self._lock:
            if generation <= self._pending_generation:
                return False
            self._pending = snapshot
            self._pending_generation = generation
            return True

    def view(self) -> CapabilitySnapshotView:
        with self._lock:
            return CapabilitySnapshotView(
                self._active,
                self._pending,
                self._pending_generation,
                self._pending is not None
                and _runtime_identity(self._active) != _runtime_identity(self._pending),
            )


def _runtime_identity(snapshot: SystemDiagnosticSnapshot) -> tuple[object, ...]:
    """Compare process-relevant dependency identity, excluding timestamps/messages."""
    return (
        snapshot.vlc.status,
        snapshot.vlc.installation_directory,
        snapshot.vlc.version,
        snapshot.ffmpeg.status,
        snapshot.ffmpeg.executable_path,
        snapshot.ffmpeg.version,
        snapshot.ffprobe.status,
        snapshot.ffprobe.executable_path,
        snapshot.ffprobe.version,
        snapshot.capabilities,
    )
