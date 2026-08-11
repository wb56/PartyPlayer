from pathlib import Path

from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.repositories.track_repository import TrackRepository


def test_network_roots_are_bounded_deduplicated_and_do_not_touch_network(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "network-roots.db")
    migrate(database)
    with database.connect() as connection:
        connection.executemany(
            """INSERT INTO tracks
               (file_path, title, artist, album, duration_seconds)
               VALUES (?, ?, '', '', 120)""",
            (
                (r"\\server\music\Party\one.mp3", "One"),
                (r"\\SERVER\MUSIC\Party\two.mp3", "Two"),
                (r"\\server\music\Quiet\three.mp3", "Three"),
                (r"C:\Music\local.mp3", "Local"),
            ),
        )

    roots = TrackRepository(database).network_roots()

    assert tuple(root.casefold() for root in roots) == (
        r"\\server\music\party",
        r"\\server\music\quiet",
    )
