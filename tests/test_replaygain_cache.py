from pathlib import Path
from threading import Event, get_ident

from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.loudness import LoudnessRepository, LoudnessService
from party_player.models import Track
from party_player.replaygain_cache import ReplayGainCacheService
from party_player.repositories.track_repository import TrackRepository
from party_player.services.library_service import LibraryService


def _services(
    tmp_path: Path,
) -> tuple[LibraryService, LoudnessRepository, LoudnessService]:
    database = Database(tmp_path / "replaygain-cache.db")
    migrate(database)
    tracks = TrackRepository(database)
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO tracks
               (id, file_path, title, artist, album, duration_seconds)
               VALUES (1, 'slow-network.mp3', 'One', '', '', 100),
                      (2, 'other.flac', 'Two', '', '', 100)"""
        )
    repository = LoudnessRepository(database)
    return LibraryService(tracks, repository), repository, LoudnessService(repository)


def test_request_reads_tags_on_worker_and_persists_cache(tmp_path: Path) -> None:
    library, repository, loudness = _services(tmp_path)
    track = library.get_track(1)
    assert track is not None
    entered = Event()
    release = Event()
    worker_thread: list[int] = []
    caller_thread = get_ident()

    def slow_refresh(track_to_refresh: Track) -> bool:
        assert track_to_refresh.id == 1
        worker_thread.append(get_ident())
        entered.set()
        release.wait(2)
        repository.save_replaygain(1, -5.0, 0.8, None, None)
        return True

    library.refresh_replaygain = slow_refresh  # type: ignore[method-assign]
    cache = ReplayGainCacheService(library, loudness)
    try:
        future = cache.request(track)
        assert future is not None
        assert entered.wait(1)
        assert not future.done()
        release.set()
        assert future.result(timeout=2)
        assert len(worker_thread) == 1
        assert worker_thread[0] != caller_thread
        assert repository.get(1).replaygain_scanned_at is not None
        assert cache.request(track) is None
    finally:
        release.set()
        cache.close()


def test_catalog_refresh_scans_each_uncached_track_once(tmp_path: Path) -> None:
    library, repository, loudness = _services(tmp_path)
    calls: list[int] = []

    def refresh(track: Track) -> bool:
        track_id = track.id
        calls.append(track_id)
        repository.save_replaygain(track_id, None, None, None, None)
        return True

    library.refresh_replaygain = refresh  # type: ignore[method-assign]
    cache = ReplayGainCacheService(library, loudness)
    try:
        first = cache.refresh_catalog()
        assert first is not None
        first.result(timeout=2)
        second = cache.refresh_catalog()
        assert second is not None
        second.result(timeout=2)
        assert calls == [1, 2]
    finally:
        cache.close()
