"""Contract tests for deterministic shared test doubles."""

import random

from party_player.database.connection import Database
from party_player.database.migrations import LATEST_SCHEMA_VERSION
from party_player.enums import CompletionStatus
from party_player.models import Track


def test_shared_test_infrastructure_is_isolated_and_deterministic(
    temporary_database: Database,
    fake_clock,
    fake_file_availability,
    deterministic_random: random.Random,
    fake_history,
) -> None:
    with temporary_database.connect() as connection:
        version = connection.execute("SELECT version FROM schema_version").fetchone()
    assert version is not None
    assert int(version["version"]) == LATEST_SCHEMA_VERSION

    fake_clock.advance(12.5)
    assert fake_clock() == 12.5

    track = Track(7, "test.mp3", "Test", "Artist", "", 60.0)
    assert fake_file_availability.evaluate(track).accepted
    assert fake_file_availability.checked_track_ids == [7]

    expected = random.Random(20260727)
    assert deterministic_random.random() == expected.random()

    fake_history.start("A", track, 9)
    assert fake_history.finish("A", CompletionStatus.PLAYED, 60.0)
    assert fake_history.events == [
        ("start", "A", (7, 9)),
        ("finish", "A", CompletionStatus.PLAYED),
    ]
