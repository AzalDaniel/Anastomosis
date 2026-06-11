from typer.testing import CliRunner

import anastomosis
from anastomosis.cli import app

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert anastomosis.__version__ in result.output


def test_info_runs() -> None:
    result = runner.invoke(app, ["info"])
    assert result.exit_code == 0
    assert "anastomosis" in result.output
