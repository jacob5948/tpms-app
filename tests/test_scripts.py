"""The shell scripts are the least-exercised code in the project: they run on
a Pi, by hand, usually when something is already wrong. At minimum they must
parse, and their options must agree with their own usage text."""

import subprocess
from pathlib import Path

import pytest

SCRIPTS = sorted((Path(__file__).resolve().parents[1] / "scripts").glob("*.sh"))


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script_parses(script):
    subprocess.run(["bash", "-n", str(script)], check=True)


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script_is_executable(script):
    assert script.stat().st_mode & 0o111


def test_update_script_documents_every_option_it_accepts():
    text = (Path(__file__).resolve().parents[1] / "scripts" / "update-pi.sh").read_text()
    usage = text.split("USAGE")[1]
    for option in ("--stash", "--no-rollback", "--service"):
        assert option in usage, option
