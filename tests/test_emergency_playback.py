from pathlib import Path
from threading import Event
from time import monotonic, sleep

from party_player.audio.fake_backend import FakeAudioBackend
from party_player.crossfader_service import CrossfaderService
from party_player.cue_points import ResolvedTrackBoundaries
from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.deck_controller import DeckController
from party_player.emergency_playlist import EmergencyMediaType, LocalEmergencyPlaylistService
from party_player.emergency_playback import EmergencyPlaybackService
from party_player.emergency_state import DeckHealth, EmergencyStateService, EmergencySystemState
from party_player.file_availability import FileAvailabilityService
from party_player.models import Track
from party_player.loudness import ResolvedLoudnessSettings
from party_player.repositories.track_repository import TrackRepository


def _playlist(tmp_path: Path) -> LocalEmergencyPlaylistService:
    audio = tmp_path / "emergency.mp3"
    audio.write_bytes(b"emergency audio")
    database = Database(tmp_path / "emergency.db")
    migrate(database)
    tracks = TrackRepository(database)
    track = tracks.upsert_file(str(audio), "Notfalltitel", "", "", 180.0)
    return LocalEmergencyPlaylistService(tracks, FileAvailabilityService(), [track.id])


def _typed_playlist(tmp_path: Path) -> LocalEmergencyPlaylistService:
    database = Database(tmp_path / "typed-emergency.db")
    migrate(database)
    tracks = TrackRepository(database)
    media_ids: dict[EmergencyMediaType, list[int]] = {}
    for index, media_type in enumerate(EmergencyMediaType, start=1):
        audio = tmp_path / f"{media_type.value.lower()}.mp3"
        audio.write_bytes(media_type.value.encode())
        track = tracks.upsert_file(str(audio), media_type.value, "", "", 30.0 + index)
        media_ids[media_type] = [track.id]
    return LocalEmergencyPlaylistService(
        tracks,
        FileAvailabilityService(),
        media_ids[EmergencyMediaType.PRIMARY],
        media_track_ids=media_ids,
    )


def test_optional_primary_preload_is_loaded_and_guaranteed_silent(tmp_path: Path) -> None:
    deck_a = DeckController("A", FakeAudioBackend())
    deck_b = DeckController("B", FakeAudioBackend())
    service = EmergencyPlaybackService(
        _playlist(tmp_path),
        EmergencyStateService(),
        deck_a,
        deck_b,
        CrossfaderService(deck_a, deck_b),
    )

    result = service.preload_primary_silently()

    assert result.success
    assert result.state == "PRELOADED_SILENT"
    assert deck_a.model.loaded_track is None
    assert deck_b.model.loaded_track is None
    assert not deck_a.backend.is_playing()
    assert not deck_b.backend.is_playing()


def test_replaced_silent_preload_can_never_start_automatically(tmp_path: Path) -> None:
    deck_a = DeckController("A", FakeAudioBackend())
    deck_b = DeckController("B", FakeAudioBackend())
    service = EmergencyPlaybackService(
        _playlist(tmp_path),
        EmergencyStateService(),
        deck_a,
        deck_b,
        CrossfaderService(deck_a, deck_b),
    )
    assert service.prepare_primary().success
    deck_a.load(Track(99, "other.mp3", "Other", "", "", 30), validate_file=False)

    result = service.activate_prepared()

    assert not result.success
    assert result.error_code == "EMERGENCY_PRELOAD_STALE"
    assert not deck_a.backend.is_playing()


def test_immediate_replace_mutes_bad_output_before_loading_and_fades_in(
    tmp_path: Path,
) -> None:
    deck_a = DeckController("A", FakeAudioBackend(duration=180))
    deck_b = DeckController("B", FakeAudioBackend(duration=180))
    deck_a.load(Track(99, "bad.mp3", "Bad", "", "", 180), validate_file=False)
    deck_a.play()
    service = EmergencyPlaybackService(
        _playlist(tmp_path),
        EmergencyStateService(),
        deck_a,
        deck_b,
        CrossfaderService(deck_a, deck_b),
        handover_duration_seconds=0.1,
    )

    result = service.immediate_replace("A")

    assert result.success
    assert not deck_a.backend.is_playing()
    assert deck_a.emergency_muted
    assert deck_b.backend.is_playing()
    assert deck_b.model.loaded_track is not None
    assert deck_b.model.loaded_track.title == "Notfalltitel"
    assert deck_b.fade_level < 1.0
    deadline = monotonic() + 1.0
    while deck_b.fade_level < 1.0 and monotonic() < deadline:
        sleep(0.01)
    assert deck_b.fade_level == 1.0


def test_cue_loudness_and_clip_protection_are_applied_while_still_muted(
    tmp_path: Path,
) -> None:
    backend = FakeAudioBackend(duration=180)
    backend.runtime_clip_protection_supported = True
    deck_a = DeckController("A", backend)
    deck_b = DeckController("B", FakeAudioBackend())
    service = EmergencyPlaybackService(
        _playlist(tmp_path),
        EmergencyStateService(),
        deck_a,
        deck_b,
        CrossfaderService(deck_a, deck_b),
        cue_provider=lambda _track: ResolvedTrackBoundaries(
            4.25, 170.0, 1.0, "MANUAL", "MANUAL", "MANUAL"
        ),
        loudness_provider=lambda _track: ResolvedLoudnessSettings(
            2.0, 2.0, 1.25, "MANUAL", False, "TRACK", False, 0.0
        ),
    )

    prepared = service.prepare_primary()

    assert prepared.success
    assert prepared.cue_in == 4.25
    assert prepared.effective_gain_db == 2.0
    assert prepared.clip_protection_enabled
    assert deck_a.model.position == 4.25
    assert deck_a.model.cue_in == 4.25
    assert deck_a.normalization_factor == 1.25
    assert backend.runtime_clip_protection == (True, -1.0)
    assert deck_a.transition_muted and deck_a.emergency_muted
    assert not backend.is_playing()


def test_positive_emergency_gain_is_removed_when_backend_has_no_clip_protection(
    tmp_path: Path,
) -> None:
    deck_a = DeckController("A", FakeAudioBackend())
    deck_b = DeckController("B", FakeAudioBackend())
    service = EmergencyPlaybackService(
        _playlist(tmp_path),
        EmergencyStateService(),
        deck_a,
        deck_b,
        CrossfaderService(deck_a, deck_b),
        loudness_provider=lambda _track: ResolvedLoudnessSettings(
            6.0, 3.0, 1.4, "FALLBACK", True, "TRACK", True, -1.0
        ),
    )

    result = service.prepare_primary()

    assert result.success
    assert result.effective_gain_db == 0.0
    assert deck_a.normalization_factor == 1.0
    assert deck_a.emergency_muted


def test_blocked_history_sink_cannot_delay_confirmed_emergency_playback(
    tmp_path: Path,
) -> None:
    release = Event()
    callback_entered = Event()

    def blocked_history(_result, _media_type, _track) -> None:
        callback_entered.set()
        release.wait(timeout=2)

    deck_a = DeckController("A", FakeAudioBackend())
    deck_b = DeckController("B", FakeAudioBackend())
    service = EmergencyPlaybackService(
        _playlist(tmp_path),
        EmergencyStateService(),
        deck_a,
        deck_b,
        CrossfaderService(deck_a, deck_b),
        playback_started=blocked_history,
        playback_confirmation_seconds=0.1,
    )
    assert service.prepare_primary().success
    started = monotonic()

    result = service.activate_prepared()

    assert result.success
    assert monotonic() - started < 0.5
    assert callback_entered.wait(timeout=0.5)
    assert deck_a.backend.is_playing()
    release.set()


def test_only_break_music_may_be_started_as_loop(tmp_path: Path) -> None:
    playlist = _typed_playlist(tmp_path)
    deck_a = DeckController("A", FakeAudioBackend())
    deck_b = DeckController("B", FakeAudioBackend())
    service = EmergencyPlaybackService(
        playlist,
        EmergencyStateService(),
        deck_a,
        deck_b,
        CrossfaderService(deck_a, deck_b),
    )

    rejected = service.prepare_media(EmergencyMediaType.JINGLE, loop=True)
    accepted = service.prepare_media(EmergencyMediaType.BREAK_MUSIC, loop=True)

    assert rejected.error_code == "LOOP_NOT_ALLOWED"
    assert accepted.success
    assert deck_a.model.loaded_track is not None
    assert deck_a.model.loaded_track.title == "BREAK_MUSIC"


def test_temporary_jingle_keeps_outgoing_deck_and_returns_after_finish(
    tmp_path: Path,
) -> None:
    playlist = _typed_playlist(tmp_path)
    incoming_backend = FakeAudioBackend(duration=30)
    deck_a = DeckController("A", incoming_backend)
    deck_b = DeckController("B", FakeAudioBackend(duration=180))
    deck_b.load(Track(99, "current.mp3", "Current", "", "", 180), validate_file=False)
    deck_b.play()
    mixer = CrossfaderService(deck_a, deck_b, position=1.0)
    service = EmergencyPlaybackService(
        playlist,
        EmergencyStateService(),
        deck_a,
        deck_b,
        mixer,
        handover_duration_seconds=0.1,
    )

    assert service.prepare_media(EmergencyMediaType.JINGLE).success
    assert service.activate_prepared().success
    deadline = monotonic() + 1.0
    while mixer.position > 0.0 and monotonic() < deadline:
        sleep(0.01)
    assert mixer.position == 0.0
    assert deck_b.backend.is_playing()

    incoming_backend.finished = True
    while (mixer.position < 1.0 or incoming_backend.is_playing()) and monotonic() < deadline:
        sleep(0.01)
    assert mixer.position == 1.0
    assert not incoming_backend.is_playing()
    assert deck_b.backend.is_playing()


def test_safe_handover_keeps_audible_deck_until_emergency_playback_is_confirmed(
    tmp_path: Path,
) -> None:
    playlist = _playlist(tmp_path)
    deck_a = DeckController("A", FakeAudioBackend(duration=180))
    deck_b = DeckController("B", FakeAudioBackend(duration=180))
    deck_b.load(
        Track(99, str(tmp_path / "current.mp3"), "Current", "", "", 180),
        validate_file=False,
    )
    deck_b.play()
    mixer = CrossfaderService(deck_a, deck_b, position=1.0)
    state = EmergencyStateService()
    service = EmergencyPlaybackService(
        playlist, state, deck_a, deck_b, mixer, handover_duration_seconds=0.1
    )

    prepared = service.prepare_primary()

    assert prepared.success and prepared.state == "PREPARED"
    assert prepared.deck_id == "A"
    assert deck_b.backend.is_playing()
    assert not deck_a.backend.is_playing()
    assert mixer.position == 1.0

    activated = service.activate_prepared()

    assert activated.success and activated.state == "PLAYING"
    assert deck_a.backend.is_playing()
    assert deck_b.backend.is_playing()
    assert mixer.position > 0.0
    deadline = monotonic() + 1.0
    while mixer.position > 0.0 and monotonic() < deadline:
        sleep(0.01)
    assert mixer.position == 0.0
    while deck_b.backend.is_playing() and monotonic() < deadline:
        sleep(0.01)
    assert not deck_b.backend.is_playing()
    assert state.snapshot().system == EmergencySystemState.EMERGENCY_ACTIVE


def test_safe_handover_moves_monotonically_and_stops_outgoing_only_after_ramp(
    tmp_path: Path,
) -> None:
    playlist = _playlist(tmp_path)
    deck_a = DeckController("A", FakeAudioBackend(duration=180))
    deck_b = DeckController("B", FakeAudioBackend(duration=180))
    deck_a.load(Track(98, "current.mp3", "Current", "", "", 180), validate_file=False)
    deck_a.play()
    mixer = CrossfaderService(deck_a, deck_b, position=0.0)
    state = EmergencyStateService()
    service = EmergencyPlaybackService(
        playlist, state, deck_a, deck_b, mixer, handover_duration_seconds=0.15
    )
    assert service.prepare_primary().deck_id == "B"

    assert service.activate_prepared().success
    samples = [mixer.position]
    assert deck_a.backend.is_playing()
    deadline = monotonic() + 1.0
    while mixer.position < 1.0 and monotonic() < deadline:
        sleep(0.01)
        samples.append(mixer.position)

    assert mixer.position == 1.0
    assert all(current >= previous for previous, current in zip(samples, samples[1:]))
    assert not deck_a.backend.is_playing()
    assert deck_b.backend.is_playing()


def test_prepare_rejects_when_no_healthy_inactive_deck_exists(tmp_path: Path) -> None:
    playlist = _playlist(tmp_path)
    deck_a = DeckController("A", FakeAudioBackend(duration=180))
    deck_b = DeckController("B", FakeAudioBackend(duration=180))
    mixer = CrossfaderService(deck_a, deck_b)
    state = EmergencyStateService()
    state.set_deck_health("A", DeckHealth.FAILED)
    state.set_deck_health("B", DeckHealth.STALLED)
    service = EmergencyPlaybackService(playlist, state, deck_a, deck_b, mixer)

    result = service.prepare_primary()

    assert not result.success
    assert result.error_code == "NO_HEALTHY_INACTIVE_DECK"
    assert deck_a.model.loaded_track is None
    assert deck_b.model.loaded_track is None


def test_emergency_prepare_timeout_returns_without_unmuting_late_deck(
    tmp_path: Path,
) -> None:
    entered, release = Event(), Event()

    class SlowPrepareBackend(FakeAudioBackend):
        def prepare(self, file_path: Path) -> object:
            entered.set()
            release.wait(timeout=2)
            return super().prepare(file_path)

    playlist = _playlist(tmp_path)
    deck_a = DeckController("A", SlowPrepareBackend())
    deck_b = DeckController("B", FakeAudioBackend())
    service = EmergencyPlaybackService(
        playlist,
        EmergencyStateService(),
        deck_a,
        deck_b,
        CrossfaderService(deck_a, deck_b),
        media_load_timeout_seconds=0.05,
    )

    started = monotonic()
    result = service.prepare_primary()

    assert monotonic() - started < 0.5
    assert entered.is_set()
    assert result.error_code == "EMERGENCY_PREPARE_TIMEOUT"
    assert deck_a.transition_muted
    assert not deck_a.backend.is_playing()
    release.set()


def test_emergency_start_timeout_stays_muted_and_consumes_attempt(
    tmp_path: Path,
) -> None:
    entered, release = Event(), Event()

    class SlowPlayBackend(FakeAudioBackend):
        def play(self) -> None:
            entered.set()
            release.wait(timeout=2)
            super().play()

    playlist = _playlist(tmp_path)
    deck_a = DeckController("A", SlowPlayBackend())
    deck_b = DeckController("B", FakeAudioBackend())
    service = EmergencyPlaybackService(
        playlist,
        EmergencyStateService(),
        deck_a,
        deck_b,
        CrossfaderService(deck_a, deck_b),
        playback_start_timeout_seconds=0.05,
    )
    assert service.prepare_primary().success

    result = service.activate_prepared()

    assert entered.is_set()
    assert result.error_code == "EMERGENCY_START_TIMEOUT"
    assert deck_a.transition_muted
    release.set()


def test_emergency_start_attempts_are_bounded(tmp_path: Path) -> None:
    class BrokenPlayBackend(FakeAudioBackend):
        def play(self) -> None:
            raise RuntimeError("Start fehlgeschlagen")

    playlist = _playlist(tmp_path)
    deck_a = DeckController("A", BrokenPlayBackend())
    deck_b = DeckController("B", FakeAudioBackend())
    service = EmergencyPlaybackService(
        playlist,
        EmergencyStateService(),
        deck_a,
        deck_b,
        CrossfaderService(deck_a, deck_b),
        maximum_start_attempts=3,
    )
    assert service.prepare_primary().success

    results = [service.activate_prepared() for _attempt in range(4)]

    assert all(not result.success for result in results)
    assert results[2].error_code == "EMERGENCY_START_FAILED"
    assert results[3].error_code == "EMERGENCY_START_ATTEMPTS_EXHAUSTED"
