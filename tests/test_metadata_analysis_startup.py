import sys
from types import ModuleType

from party_player import __main__ as application_entry


def test_application_entry_calls_freeze_support_before_composition(monkeypatch) -> None:
    calls: list[str] = []

    class ApplicationFake:
        def __init__(self) -> None:
            calls.append("compose")

        def run(self) -> None:
            calls.append("run")

    app_module = ModuleType("party_player.app")
    app_module.PartyPlayerApplication = ApplicationFake  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "party_player.app", app_module)
    monkeypatch.setattr(
        application_entry.multiprocessing, "freeze_support", lambda: calls.append("freeze")
    )
    monkeypatch.setattr(application_entry.sys, "argv", ["DeckRelay.exe"])

    application_entry.main()

    assert calls == ["freeze", "compose", "run"]
