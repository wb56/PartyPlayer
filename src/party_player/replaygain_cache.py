"""Asynchronous ReplayGain tag cache for existing catalog tracks."""

from concurrent.futures import Future
import logging
from threading import Event, Lock

from party_player.bounded_executor import BoundedThreadPoolExecutor
from party_player.loudness import LoudnessService
from party_player.models import Track
from party_player.services.library_service import LibraryService
from party_player.persistence_participant import single_worker_participant
from party_player.restore_lifecycle import PersistenceParticipant


class ReplayGainCacheService:
    """Read slow media tags on one bounded worker and persist completed scans."""

    PAGE_SIZE = 200

    def __init__(self, library: LibraryService, loudness: LoudnessService) -> None:
        self._library = library
        self._loudness = loudness
        self._executor = BoundedThreadPoolExecutor(
            max_workers=1,
            maximum_pending=64,
            thread_name_prefix="replaygain-cache",
        )
        self._pending: set[int] = set()
        self._lock = Lock()
        self._closing = Event()
        self._catalog_refresh: Future[None] | None = None
        self._logger = logging.getLogger(__name__)

    def request(self, track: Track) -> Future[bool] | None:
        """Schedule one uncached track without waiting for file or network I/O."""
        if self._loudness.get(track.id).replaygain_scanned_at is not None:
            return None
        with self._lock:
            if track.id in self._pending:
                return None
            self._pending.add(track.id)
        try:
            future = self._executor.submit(self._refresh_one, track)
        except RuntimeError:
            with self._lock:
                self._pending.discard(track.id)
            self._logger.warning("ReplayGain-Cachewarteschlange ist ausgelastet")
            return None
        return future

    def refresh_catalog(self) -> Future[None] | None:
        """Scan every uncached catalog entry on the background worker."""
        with self._lock:
            if self._catalog_refresh is not None and not self._catalog_refresh.done():
                return self._catalog_refresh
            try:
                future = self._executor.submit(self._refresh_catalog)
            except RuntimeError:
                self._logger.warning("ReplayGain-Katalogaktualisierung konnte nicht starten")
                return None
            self._catalog_refresh = future
            return future

    def close(self) -> None:
        self._closing.set()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def restore_participant(self) -> PersistenceParticipant:
        return single_worker_participant("replaygain-cache", self._executor)

    def _refresh_one(self, track: Track) -> bool:
        try:
            return self._library.refresh_replaygain(track)
        finally:
            with self._lock:
                self._pending.discard(track.id)

    def _refresh_catalog(self) -> None:
        offset = 0
        while not self._closing.is_set():
            tracks = self._library.page(self.PAGE_SIZE, offset)
            if not tracks:
                return
            for track in tracks:
                if self._closing.is_set():
                    return
                if self._loudness.get(track.id).replaygain_scanned_at is not None:
                    continue
                with self._lock:
                    if track.id in self._pending:
                        continue
                    self._pending.add(track.id)
                self._refresh_one(track)
            offset += len(tracks)
