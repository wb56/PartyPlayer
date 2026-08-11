from time import monotonic, sleep

import pytest

from party_player.crossfader_service import CrossfaderService
from party_player.deck_controller import DeckController
from party_player.ducking import DuckingController, db_to_linear
from party_player.audio.fake_backend import FakeAudioBackend


def wait_until(predicate: object, timeout: float = 1.0) -> None:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if callable(predicate) and predicate():
            return
        sleep(0.005)
    raise AssertionError("Ducking-Rampe erreichte den Zielwert nicht")


def test_db_conversion_is_bounded_to_attenuation() -> None:
    assert db_to_linear(-6.0) == pytest.approx(0.501187)
    assert db_to_linear(3.0) == 1.0
    assert db_to_linear(-100.0) == pytest.approx(0.001)


def test_attack_and_release_do_not_change_visible_mixer_values() -> None:
    deck_a = DeckController("A", FakeAudioBackend())
    deck_b = DeckController("B", FakeAudioBackend())
    mixer = CrossfaderService(deck_a, deck_b, position=0.25, master_volume=0.7)
    ducking = DuckingController(mixer.set_ducking_factor)

    ducking.attack(-6.0, 10)
    wait_until(lambda: ducking.factor == pytest.approx(db_to_linear(-6.0), abs=0.001))

    assert mixer.position == 0.25
    assert mixer.master_volume == 0.7
    assert deck_a.model.volume == 1.0
    assert deck_b.model.volume == 1.0
    assert mixer.effective_volumes() == pytest.approx(
        (
            mixer.factors()[0] * 0.7 * db_to_linear(-6.0),
            mixer.factors()[1] * 0.7 * db_to_linear(-6.0),
        ),
        abs=0.001,
    )

    ducking.release(10)
    wait_until(lambda: ducking.factor == pytest.approx(1.0))
    ducking.close()


def test_new_attack_replaces_release_without_intermediate_full_volume() -> None:
    values: list[float] = []
    ducking = DuckingController(values.append)
    ducking.attack(-12.0, 10)
    wait_until(lambda: ducking.factor < 0.3)
    ducking.release(200)
    sleep(0.03)
    before_replacement = ducking.factor

    ducking.attack(-8.0, 20)
    wait_until(lambda: ducking.factor == pytest.approx(db_to_linear(-8.0), abs=0.001))

    assert max(values[-3:]) < 1.0
    assert before_replacement < 1.0
    ducking.close()


def test_reset_and_close_guarantee_full_restoration() -> None:
    values: list[float] = []
    ducking = DuckingController(values.append)
    ducking.attack(-15.0, 10)
    wait_until(lambda: ducking.factor < 0.2)

    ducking.reset()
    assert ducking.factor == 1.0
    assert values[-1] == 1.0

    ducking.attack(-6.0, 10)
    wait_until(lambda: ducking.factor < 0.6)
    ducking.close()
    assert ducking.factor == 1.0
