from pathlib import Path

from party_player.emergency_storage import EmergencyDriveKind, EmergencyStoragePolicy


def test_fixed_media_must_be_inside_an_approved_local_ssd_root(tmp_path: Path) -> None:
    approved = tmp_path / "emergency"
    outside = tmp_path / "music" / "track.mp3"
    policy = EmergencyStoragePolicy(
        [approved], drive_classifier=lambda _path: EmergencyDriveKind.FIXED
    )

    assert policy.evaluate(approved / "track.mp3").allowed
    rejected = policy.evaluate(outside)
    assert rejected.code == "OUTSIDE_APPROVED_LOCAL_SSD_ROOT"


def test_fixed_media_is_rejected_when_no_ssd_root_is_configured(tmp_path: Path) -> None:
    policy = EmergencyStoragePolicy([], drive_classifier=lambda _path: EmergencyDriveKind.FIXED)

    result = policy.evaluate(tmp_path / "track.mp3")

    assert not result.allowed
    assert result.code == "NO_APPROVED_LOCAL_SSD_ROOT"


def test_mapped_network_drive_is_rejected_even_under_approved_root(tmp_path: Path) -> None:
    policy = EmergencyStoragePolicy(
        [tmp_path], drive_classifier=lambda _path: EmergencyDriveKind.NETWORK
    )

    result = policy.evaluate(tmp_path / "track.mp3")

    assert not result.allowed
    assert result.code == "NETWORK_STORAGE"


def test_removable_media_requires_its_own_explicit_allowlist(tmp_path: Path) -> None:
    removable = tmp_path / "usb"
    denied = EmergencyStoragePolicy(
        [tmp_path], drive_classifier=lambda _path: EmergencyDriveKind.REMOVABLE
    )
    allowed = EmergencyStoragePolicy(
        [tmp_path],
        approved_removable_roots=[removable],
        drive_classifier=lambda _path: EmergencyDriveKind.REMOVABLE,
    )

    assert denied.evaluate(removable / "track.mp3").code == "REMOVABLE_STORAGE_NOT_APPROVED"
    assert allowed.evaluate(removable / "track.mp3").allowed


def test_cloud_root_is_rejected_before_fixed_drive_allowlist(tmp_path: Path) -> None:
    cloud = tmp_path / "OneDrive"
    policy = EmergencyStoragePolicy(
        [tmp_path],
        cloud_roots=[cloud],
        drive_classifier=lambda _path: EmergencyDriveKind.FIXED,
    )

    result = policy.evaluate(cloud / "track.mp3")

    assert not result.allowed
    assert result.code == "CLOUD_STORAGE"
