"""Explicit ownership boundary for player and shared audio resources."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from party_player.audio.base import AudioBackend
from party_player.audio.vlc_backend import VlcAudioBackend


@dataclass(frozen=True, slots=True)
class AudioResourceLifecycle:
    independent_players: bool
    preserves_shared_resource_on_deck_close: bool
    preserves_output_device_on_deck_restart: bool
    shared_resource_identity: int | None
    active_players: int


@runtime_checkable
class AudioBackendFactory(Protocol):
    def create_deck_backend(self, deck_id: str) -> AudioBackend: ...
    def create_auxiliary_backend(self, role: str) -> AudioBackend: ...
    def lifecycle(self) -> AudioResourceLifecycle: ...
    def release_shared_resources(self) -> bool: ...


class VlcAudioBackendFactory:
    """Create independent players while retaining one process-wide VLC instance."""

    def __init__(
        self,
        output_device: str = "",
        installation_directory: str | Path | None = None,
    ) -> None:
        self._output_device = output_device.strip()
        self._installation_directory = (
            Path(installation_directory).resolve() if installation_directory else None
        )

    def create_deck_backend(self, deck_id: str) -> AudioBackend:
        normalized = deck_id.upper()
        if normalized not in {"A", "B"}:
            raise ValueError("Unbekanntes Deck")
        return VlcAudioBackend(
            self._output_device,
            worker_name=f"vlc-volume-{normalized}",
            installation_directory=self._installation_directory,
        )

    def create_auxiliary_backend(self, role: str) -> AudioBackend:
        normalized = role.strip().casefold() or "auxiliary"
        return VlcAudioBackend(
            self._output_device,
            worker_name=f"vlc-volume-{normalized}",
            installation_directory=self._installation_directory,
        )

    def lifecycle(self) -> AudioResourceLifecycle:
        return AudioResourceLifecycle(
            independent_players=True,
            preserves_shared_resource_on_deck_close=True,
            preserves_output_device_on_deck_restart=True,
            shared_resource_identity=VlcAudioBackend.shared_instance_identity(),
            active_players=VlcAudioBackend.active_player_count(),
        )

    def release_shared_resources(self) -> bool:
        """Release VLC itself only after every deck/auxiliary player is closed."""
        return VlcAudioBackend.release_shared_instance()
