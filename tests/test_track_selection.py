"""Selection-rule pipeline tests independent from GUI and decks."""

import pytest

from party_player.enums import QueueStatus
from party_player.models import QueueEntry, Track
from party_player.track_selection import (
    BlockService,
    RepetitionService,
    SelectionDecision,
    TrackSelectionService,
    normalize_artist_name,
)


class RejectTrack:
    def __init__(self, track_id: int) -> None:
        self.track_id = track_id

    def evaluate(
        self,
        _entry: QueueEntry,
        track: Track,
    ) -> SelectionDecision | None:
        if track.id == self.track_id:
            return SelectionDecision.reject(
                "BLOCKED_TRACK",
                reason="Titel ist für die Automatik gesperrt",
            )
        return None


def test_missing_track_is_a_stable_failed_decision() -> None:
    service = TrackSelectionService()

    decision = service.evaluate(
        QueueEntry(1, 99, 1, QueueStatus.WAITING),
        None,
    )

    assert not decision.accepted
    assert decision.code == "TRACK_MISSING"
    assert decision.terminal_status == QueueStatus.FAILED


@pytest.mark.parametrize(
    "track",
    [
        Track(1, "", "Song", "Artist", "", 120.0),
        Track(1, "song.mp3", "  ", "Artist", "", 120.0),
        Track(1, "song.mp3", "Song", "Artist", "", 0.0),
        Track(1, "song.mp3", "Song", "Artist", "", float("nan")),
    ],
)
def test_invalid_metadata_is_a_stable_failed_decision(track: Track) -> None:
    decision = TrackSelectionService().evaluate(
        QueueEntry(1, track.id, 1, QueueStatus.WAITING),
        track,
    )

    assert not decision.accepted
    assert decision.code == "INVALID_METADATA"
    assert decision.terminal_status == QueueStatus.FAILED


def test_injected_rule_rejects_without_deck_or_gui_dependency() -> None:
    service = TrackSelectionService((RejectTrack(7),))
    entry = QueueEntry(1, 7, 1, QueueStatus.WAITING)
    track = Track(7, "song.mp3", "Song", "Artist", "Album", 120.0)

    decision = service.evaluate(entry, track)

    assert not decision.accepted
    assert decision.code == "BLOCKED_TRACK"
    assert decision.terminal_status == QueueStatus.SKIPPED


def test_rejection_status_must_be_terminal_selection_result() -> None:
    with pytest.raises(ValueError, match="SKIPPED oder FAILED"):
        SelectionDecision.reject(
            "INVALID",
            terminal_status=QueueStatus.READY,
        )


def test_block_service_uses_track_ids_and_normalized_artist_names() -> None:
    rule = BlockService(blocked_track_ids={7}, blocked_artists={"  The   Band "})
    entry = QueueEntry(1, 7, 1, QueueStatus.WAITING)
    blocked_track = Track(7, "one.mp3", "One", "Other", "", 120.0)
    blocked_artist = Track(8, "two.mp3", "Two", "THE BAND", "", 120.0)

    assert rule.evaluate(entry, blocked_track).code == "BLOCKED_TRACK"  # type: ignore[union-attr]
    assert rule.evaluate(entry, blocked_artist).code == "BLOCKED_ARTIST"  # type: ignore[union-attr]
    rule.allow_track(7)
    rule.allow_artist("the band")
    assert rule.evaluate(entry, blocked_track) is None
    assert rule.evaluate(entry, blocked_artist) is None


def test_repetition_service_uses_independent_bounded_windows() -> None:
    rule = RepetitionService(track_window_size=2, artist_window_size=1)
    first = Track(1, "one.mp3", "One", "Artist A", "", 120.0)
    second = Track(2, "two.mp3", "Two", "Artist B", "", 120.0)
    same_artist = Track(3, "three.mp3", "Three", " artist B ", "", 120.0)
    entry = QueueEntry(1, 1, 1, QueueStatus.WAITING)
    rule.record_played(first)
    rule.record_played(second)

    track_decision = rule.evaluate(entry, first)
    artist_decision = rule.evaluate(entry, same_artist)

    assert track_decision is not None and track_decision.code == "TRACK_REPETITION"
    assert artist_decision is not None and artist_decision.code == "ARTIST_REPETITION"


def test_artist_normalization_is_casefolded_and_whitespace_stable() -> None:
    assert normalize_artist_name("  Die   ÄRZTE  ") == "die ärzte"


def test_relaxation_never_bypasses_hard_track_block() -> None:
    service = TrackSelectionService((RejectTrack(7),))
    entry = QueueEntry(1, 7, 1, QueueStatus.WAITING)
    track = Track(7, "song.mp3", "Song", "Artist", "", 120.0)

    decision = service.evaluate(
        entry,
        track,
        relaxed_codes=frozenset({"BLOCKED_TRACK"}),
    )

    assert not decision.accepted
    assert decision.code == "BLOCKED_TRACK"
