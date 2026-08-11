"""Global safety fixtures for the test suite."""

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
import random
import sqlite3
from typing import Any, cast

import pytest
from _pytest.monkeypatch import MonkeyPatch

from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.enums import CompletionStatus
from party_player.models import Track
from party_player.track_selection import SelectionDecision


@dataclass
class FakeClock:
    value: float = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@dataclass
class FakeFileAvailability:
    decision: SelectionDecision = field(default_factory=SelectionDecision.allow)
    checked_track_ids: list[int] = field(default_factory=list)

    def evaluate(self, track: Track) -> SelectionDecision:
        self.checked_track_ids.append(track.id)
        return self.decision


@dataclass
class FakeHistory:
    events: list[tuple[str, object, object]] = field(default_factory=list)

    def start(self, deck_id: str, track: Track, queue_id: int | None = None) -> None:
        self.events.append(("start", deck_id, (track.id, queue_id)))

    def finish(
        self,
        deck_id: str,
        status: CompletionStatus,
        _play_duration: float,
    ) -> bool:
        self.events.append(("finish", deck_id, status))
        return True


@pytest.fixture(autouse=True)
def prevent_production_database_access(monkeypatch: MonkeyPatch) -> Iterator[None]:
    """Fail immediately if a test tries to open the development database."""
    production_database = (Path.cwd() / "data" / "party_player.db").resolve()
    original_connect = sqlite3.connect

    def guarded_connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        if not isinstance(database, int):
            candidate = Path(database).resolve()
            if candidate == production_database:
                pytest.fail("Ein Test hat versucht, die produktive DeckRelay-Datenbank zu öffnen.")
        return cast(sqlite3.Connection, original_connect(database, *args, **kwargs))

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    yield


@pytest.fixture
def temporary_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "party-player-test.db")
    migrate(database)
    return database


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def fake_file_availability() -> FakeFileAvailability:
    return FakeFileAvailability()


@pytest.fixture
def deterministic_random() -> random.Random:
    return random.Random(20260727)


@pytest.fixture
def fake_history() -> FakeHistory:
    return FakeHistory()
