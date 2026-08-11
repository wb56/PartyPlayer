from time import monotonic

from party_player.diagnostic_scenario import DiagnosticScenario


def test_database_delay_is_active_only_inside_explicit_scenario() -> None:
    scenario = DiagnosticScenario()
    assert scenario.inject_database_delay() == 0

    scenario.begin("database_delay", 20)
    started = monotonic()
    assert scenario.inject_database_delay() == 20
    elapsed = monotonic() - started
    scenario.end()

    assert elapsed >= 0.012
    assert scenario.inject_database_delay() == 0


def test_negative_control_detects_synchronous_database_delay() -> None:
    """An old-style inline delay must measurably block its calling thread."""
    scenario = DiagnosticScenario()
    scenario.begin("database_delay", 100)

    started = monotonic()
    scenario.inject_database_delay()

    assert monotonic() - started >= 0.070
