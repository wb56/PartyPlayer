from party_player.external_process import ExternalProcessResult
from party_player.network_source_check import NetworkSourceChecker


class FakeRunner:
    def __init__(self, result: ExternalProcessResult) -> None:
        self.result = result
        self.arguments: tuple[str, ...] = ()
        self.timeout: float | None = None

    def run(self, arguments, *, timeout_seconds=None):
        self.arguments = tuple(str(item) for item in arguments)
        self.timeout = timeout_seconds
        return self.result


def result(return_code: int = 0, *, timed_out: bool = False) -> ExternalProcessResult:
    return ExternalProcessResult(("powershell.exe",), return_code, "", "", 0.01, timed_out)


def test_unc_check_is_non_recursive_literal_and_bounded() -> None:
    runner = FakeRunner(result())
    checker = NetworkSourceChecker(
        runner=runner,  # type: ignore[arg-type]
        powershell_executable="powershell.exe",
        timeout_seconds=1.25,
    )

    checked = checker(r"\\server\share")

    assert checked.reachable
    assert runner.timeout == 1.25
    assert "Test-Path -LiteralPath" in runner.arguments[-2]
    assert runner.arguments[-1] == r"\\server\share"
    assert "Recurse" not in runner.arguments[-2]


def test_timeout_is_reported_without_retry() -> None:
    runner = FakeRunner(result(-1, timed_out=True))

    checked = NetworkSourceChecker(
        runner=runner,  # type: ignore[arg-type]
        powershell_executable="powershell.exe",
    )(r"\\server\offline")

    assert not checked.reachable
    assert checked.timed_out
    assert "Zeitlimit" in checked.message


def test_local_paths_are_not_probed() -> None:
    runner = FakeRunner(result())

    checked = NetworkSourceChecker(
        runner=runner,  # type: ignore[arg-type]
        powershell_executable="powershell.exe",
    )(r"C:\Music")

    assert not checked.reachable
    assert runner.arguments == ()
