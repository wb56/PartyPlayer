"""Generation-safe active and pending capability snapshots."""

import pytest

from party_player.capability_snapshots import CapabilitySnapshotState
from party_player.system_dependencies import (
    DependencyInfo,
    DependencyStatus,
    RuntimeCapabilities,
    SystemDiagnosticSnapshot,
    VlcDependencyInfo,
)


def snapshot(version: str, playback: bool) -> SystemDiagnosticSnapshot:
    vlc = VlcDependencyInfo(
        DependencyStatus.AVAILABLE if playback else DependencyStatus.NOT_FOUND,
        source="test",
        libvlc_loaded=playback,
    )
    missing_ffmpeg = DependencyInfo("FFmpeg", DependencyStatus.NOT_FOUND)
    missing_ffprobe = DependencyInfo("FFprobe", DependencyStatus.NOT_FOUND)
    return SystemDiagnosticSnapshot(
        version,
        vlc,
        missing_ffmpeg,
        missing_ffprobe,
        RuntimeCapabilities(playback, False, False, False),
    )


def test_pending_snapshot_never_replaces_active_or_accepts_older_generation() -> None:
    active = snapshot("active", True)
    newer = snapshot("newer", False)
    older = snapshot("older", True)
    state = CapabilitySnapshotState(active)

    assert state.publish_pending(2, newer)
    assert not state.publish_pending(1, older)

    view = state.view()
    assert view.active is active
    assert view.pending is newer
    assert view.pending_generation == 2
    assert view.restart_required


def test_capability_generation_must_be_positive() -> None:
    state = CapabilitySnapshotState(snapshot("active", True))

    with pytest.raises(ValueError, match="positive"):
        state.publish_pending(0, snapshot("pending", False))


def test_recheck_with_same_runtime_identity_does_not_require_restart() -> None:
    active = snapshot("active-check-time", True)
    rechecked = snapshot("later-check-time", True)
    state = CapabilitySnapshotState(active)

    assert state.publish_pending(1, rechecked)

    assert not state.view().restart_required
