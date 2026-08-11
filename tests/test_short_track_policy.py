"""Effective-duration and short-track policy tests."""

from pathlib import Path

from party_player.cue_points import CuePointRepository, CuePointService
from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.enums import QueueSource, QueueStatus, ShortTrackPolicy
from party_player.models import QueueEntry, Track
from party_player.short_track_policy import ShortTrackSelectionRule


def _cue_service(
    path: Path,
    policy: ShortTrackPolicy,
) -> CuePointService:
    database = Database(path)
    migrate(database)
    return CuePointService(
        CuePointRepository(database),
        global_fade_duration=8,
        minimum_fade_duration=0.5,
        minimum_playable_duration=5,
        short_track_threshold=30,
        short_track_policy=policy,
    )


def test_effective_cue_duration_drives_manual_only_policy(tmp_path: Path) -> None:
    cues = _cue_service(tmp_path / "manual-only.db", ShortTrackPolicy.MANUAL_ONLY)
    track = Track(1, "song.mp3", "Song", "Artist", "", 180)
    automatic = QueueEntry(
        1,
        1,
        1,
        QueueStatus.WAITING,
        source=QueueSource.AUTOMATIC,
        cue_in_override=40,
        cue_out_override=60,
        cue_override_source="queue",
    )
    manual = QueueEntry(
        2,
        1,
        1,
        QueueStatus.WAITING,
        source=QueueSource.MANUAL,
        cue_in_override=40,
        cue_out_override=60,
        cue_override_source="queue",
    )
    rule = ShortTrackSelectionRule(
        cues,
        threshold_seconds=30,
        policy=ShortTrackPolicy.MANUAL_ONLY,
    )

    rejected = rule.evaluate(automatic, track)
    assert rejected is not None
    assert rejected.code == "SHORT_TRACK_MANUAL_ONLY"
    assert rule.evaluate(manual, track) is None


def test_reduced_fade_is_applied_to_short_effective_duration(tmp_path: Path) -> None:
    cues = _cue_service(tmp_path / "reduced-fade.db", ShortTrackPolicy.USE_REDUCED_FADE)
    track = Track(1, "song.mp3", "Song", "Artist", "", 180)
    entry = QueueEntry(
        1,
        1,
        1,
        QueueStatus.WAITING,
        cue_in_override=20,
        cue_out_override=40,
        fade_duration_override=8,
        cue_override_source="queue",
    )

    resolved = cues.resolve(track, queue_entry=entry)

    assert resolved.cue_out - resolved.cue_in == 20
    assert resolved.fade_duration == 5
    assert resolved.fade_source == "SHORT_TRACK_POLICY"


def test_too_short_track_disables_automatic_crossfade_without_rejecting_manual(
    tmp_path: Path,
) -> None:
    cues = _cue_service(tmp_path / "unsafe.db", ShortTrackPolicy.ALLOW)
    track = Track(1, "song.mp3", "Song", "Artist", "", 180)
    entry = QueueEntry(
        1,
        1,
        1,
        QueueStatus.WAITING,
        cue_in_override=20,
        cue_out_override=23,
        cue_override_source="queue",
    )

    resolved = cues.resolve(track, queue_entry=entry)

    assert not resolved.automatic_crossfade_allowed
    assert "unterschreitet das Minimum" in resolved.warning
