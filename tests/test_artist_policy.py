"""Persistent artist-policy tests."""

from datetime import datetime, timedelta
from pathlib import Path

from party_player.artist_policy import (
    ArtistPolicyRepository,
    ArtistPolicyScope,
    PersistentArtistBlockService,
)
from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.enums import QueueStatus
from party_player.models import QueueEntry, Track
from party_player.track_selection import normalize_artist_name


def _database(path: Path) -> Database:
    database = Database(path)
    migrate(database)
    with database.connect() as connection:
        connection.executemany(
            """INSERT INTO party_sessions (id, name, status)
               VALUES (?, ?, 'active')""",
            [(4, "Session 4"), (5, "Session 5")],
        )
    return database


def _track(artist: str) -> Track:
    return Track(1, "song.mp3", "Song", artist, "", 60.0)


def test_artist_normalization_handles_case_whitespace_and_separators() -> None:
    assert normalize_artist_name("  Artist A  FEAT.  Artist B ") == "artist a|artist b"
    assert normalize_artist_name("Artist A / Artist B") == "artist a|artist b"


def test_permanent_artist_policy_survives_restart(tmp_path: Path) -> None:
    database = _database(tmp_path / "artist.db")
    ArtistPolicyRepository(database).set(
        "  The   Band ",
        ArtistPolicyScope.PERMANENT,
        reason="Veranstaltungsregel",
    )

    restored = ArtistPolicyRepository(database).get("THE BAND")

    assert restored is not None
    assert restored.scope is ArtistPolicyScope.PERMANENT
    assert restored.reason == "Veranstaltungsregel"


def test_session_and_temporary_artist_policies_obey_scope(tmp_path: Path) -> None:
    database = _database(tmp_path / "scopes.db")
    repository = ArtistPolicyRepository(database)
    entry = QueueEntry(1, 1, 1, QueueStatus.WAITING)
    now = datetime(2026, 7, 27, 12, 0)
    repository.set("Artist", ArtistPolicyScope.SESSION, session_id=4)

    assert (
        PersistentArtistBlockService(repository, 4, clock=lambda: now).evaluate(
            entry, _track("artist")
        )
        is not None
    )
    assert (
        PersistentArtistBlockService(repository, 5, clock=lambda: now).evaluate(
            entry, _track("artist")
        )
        is None
    )

    repository.set(
        "Artist",
        ArtistPolicyScope.TEMPORARY,
        expires_at=now + timedelta(minutes=30),
    )
    assert (
        PersistentArtistBlockService(repository, 5, clock=lambda: now).evaluate(
            entry, _track("ARTIST")
        )
        is not None
    )
    assert (
        PersistentArtistBlockService(
            repository, 5, clock=lambda: now + timedelta(hours=1)
        ).evaluate(entry, _track("ARTIST"))
        is None
    )
