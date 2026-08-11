from pathlib import Path
import tomllib

from party_player import __version__


def test_package_and_project_versions_match() -> None:
    project_file = Path(__file__).parents[1] / "pyproject.toml"
    project = tomllib.loads(project_file.read_text(encoding="utf-8"))

    assert __version__ == "1.0.0-beta.1"
    assert project["project"]["version"] == __version__
