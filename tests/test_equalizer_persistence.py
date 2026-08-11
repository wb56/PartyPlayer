from pathlib import Path

from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.equalizer import QueueEqualizerContext
from party_player.equalizer_resolver import EqualizerResolver
from party_player.models import Track
from party_player.repositories.equalizer_repository import (
    EqualizerAssignmentRepository,
    EqualizerPresetRepository,
)


def _components(
    tmp_path: Path,
) -> tuple[
    Database,
    EqualizerPresetRepository,
    EqualizerAssignmentRepository,
    EqualizerResolver,
]:
    database = Database(tmp_path / "party-player.db")
    migrate(database)
    presets = EqualizerPresetRepository(database)
    assignments = EqualizerAssignmentRepository(database)
    return database, presets, assignments, EqualizerResolver(presets, assignments)


def _insert_track(database: Database, genre: str = "Rock") -> Track:
    with database.connect() as connection:
        cursor = connection.execute(
            """INSERT INTO tracks (file_path, title, artist, album, genre)
               VALUES ('test.mp3', 'Titel', 'Interpret', 'Album', ?)""",
            (genre,),
        )
        assert cursor.lastrowid is not None
        track_id = cursor.lastrowid
    return Track(track_id, "test.mp3", "Titel", "Interpret", "Album", 180.0, genre)


def test_schema_seeds_builtin_presets_idempotently(tmp_path: Path) -> None:
    database, presets, _assignments, _resolver = _components(tmp_path)

    migrate(database)

    assert [preset.preset_id for preset in presets.list_enabled()] == [
        "bluesrock",
        "dance",
        "disabled",
        "neutral",
        "pop",
        "rock",
    ]
    rock = presets.get_by_key("rock")
    assert rock is not None
    assert rock.curve


def test_resolver_uses_title_queue_genre_global_priority(tmp_path: Path) -> None:
    database, presets, assignments, resolver = _components(tmp_path)
    track = _insert_track(database)
    rock = presets.get_by_key("rock")
    pop = presets.get_by_key("pop")
    dance = presets.get_by_key("dance")
    bluesrock = presets.get_by_key("bluesrock")
    assert rock and pop and dance and bluesrock
    with database.connect() as connection:
        cursor = connection.execute("INSERT INTO saved_queues (name) VALUES ('Party')")
        assert cursor.lastrowid is not None
        queue_id = cursor.lastrowid
    assignments.assign_genre("  ROCK ", rock.database_id)
    assignments.assign_saved_queue(queue_id, pop.database_id)
    resolver.refresh()
    context = QueueEqualizerContext(
        transient_preset_id=dance.database_id,
        saved_queue_id=queue_id,
    )

    preset, source = resolver.resolve(track, context, "neutral")
    assert preset is not None
    assert (preset.preset_id, source) == ("dance", "QUEUE")

    assignments.assign_track(track.id, bluesrock.database_id)
    resolver.refresh()
    preset, source = resolver.resolve(track, context, "neutral")
    assert preset is not None
    assert (preset.preset_id, source) == ("bluesrock", "TITLE")

    assignments.assign_track(track.id, None)
    resolver.refresh()
    context = QueueEqualizerContext(saved_queue_id=queue_id)
    preset, source = resolver.resolve(track, context, "neutral")
    assert preset is not None
    assert (preset.preset_id, source) == ("pop", "PLAYLIST")

    assignments.assign_saved_queue(queue_id, None)
    resolver.refresh()
    preset, source = resolver.resolve(track, context, "neutral")
    assert preset is not None
    assert (preset.preset_id, source) == ("rock", "GENRE")

    assignments.assign_genre("rock", None)
    resolver.refresh()
    preset, source = resolver.resolve(track, context, "neutral")
    assert preset is not None
    assert (preset.preset_id, source) == ("neutral", "GLOBAL")


def test_deleted_preset_assignment_falls_back_to_inheritance(tmp_path: Path) -> None:
    database, presets, assignments, resolver = _components(tmp_path)
    track = _insert_track(database, genre="")
    rock = presets.get_by_key("rock")
    assert rock is not None and rock.database_id is not None
    assignments.assign_track(track.id, rock.database_id)

    with database.connect() as connection:
        connection.execute("DELETE FROM equalizer_presets WHERE id = ?", (rock.database_id,))
    resolver.refresh()

    preset, source = resolver.resolve(track, None, "neutral")
    assert preset is not None
    assert (preset.preset_id, source) == ("neutral", "GLOBAL")
    assert assignments.track_preset_id(track.id) is None
