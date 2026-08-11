from party_player.audio.fake_backend import FakeAudioBackend
from pathlib import Path

from party_player.audio.factory import AudioResourceLifecycle, VlcAudioBackendFactory
from party_player.audio_recovery import AudioRecoveryService
from party_player.deck_controller import DeckController
from party_player.emergency_state import EmergencyStateService


class FakeLifecycleFactory:
    def __init__(self) -> None:
        self.created: list[str] = []

    def create_deck_backend(self, deck_id: str) -> FakeAudioBackend:
        self.created.append(deck_id)
        return FakeAudioBackend()

    def create_auxiliary_backend(self, role: str) -> FakeAudioBackend:
        self.created.append(role)
        return FakeAudioBackend()

    def lifecycle(self) -> AudioResourceLifecycle:
        return AudioResourceLifecycle(True, True, True, 1234, 2)

    def release_shared_resources(self) -> bool:
        return True


def test_recovery_uses_factory_lifecycle_instead_of_boolean_claims() -> None:
    factory = FakeLifecycleFactory()
    deck_a = DeckController("A", FakeAudioBackend())
    deck_b = DeckController("B", FakeAudioBackend())
    service = AudioRecoveryService(
        EmergencyStateService(),
        deck_a,
        deck_b,
        factory,
        independent_players=False,
        preserves_shared_instance=False,
        preserves_output_device=False,
    )

    assessment = service.can_restart_deck_independently("A")
    result = service.recover_deck("A")

    assert assessment.allowed
    assert result.success
    assert factory.created == ["A"]


def test_isolated_recovery_rejects_changed_shared_resource_identity() -> None:
    class UnsafeFactory(FakeLifecycleFactory):
        def __init__(self) -> None:
            super().__init__()
            self.identity = 1234

        def create_deck_backend(self, deck_id: str) -> FakeAudioBackend:
            backend = super().create_deck_backend(deck_id)
            self.identity += 1
            return backend

        def lifecycle(self) -> AudioResourceLifecycle:
            return AudioResourceLifecycle(True, True, True, self.identity, 2)

    factory = UnsafeFactory()
    service = AudioRecoveryService(
        EmergencyStateService(),
        DeckController("A", FakeAudioBackend()),
        DeckController("B", FakeAudioBackend()),
        factory,
    )

    result = service.recover_deck("A")

    assert not result.success
    assert result.error_code == "SHARED_RESOURCE_CHANGED_DURING_DECK_RECOVERY"


def test_vlc_factory_passes_confirmed_directory_to_every_backend(
    monkeypatch,
    tmp_path: Path,
) -> None:
    created: list[tuple[str, str, Path | None]] = []

    class CapturingBackend(FakeAudioBackend):
        def __init__(
            self,
            output_device: str,
            worker_name: str,
            installation_directory: Path | None,
        ) -> None:
            super().__init__()
            created.append((output_device, worker_name, installation_directory))

    monkeypatch.setattr("party_player.audio.factory.VlcAudioBackend", CapturingBackend)
    directory = tmp_path / "VLC install"
    factory = VlcAudioBackendFactory("device-1", directory)

    factory.create_deck_backend("A")
    factory.create_auxiliary_backend("preview")

    assert created == [
        ("device-1", "vlc-volume-A", directory.resolve()),
        ("device-1", "vlc-volume-preview", directory.resolve()),
    ]
