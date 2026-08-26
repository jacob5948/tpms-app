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
    for option in ("--stash", "--no-rollback", "--service", "--prefix"):
        assert option in usage, option


def test_deploy_script_documents_every_option_it_accepts():
    text = (Path(__file__).resolve().parents[1] / "scripts" / "deploy.sh").read_text()
    usage = text.split("USAGE")[1]
    for option in ("--prefix", "--user", "--pip"):
        assert option in usage, option


def test_update_asks_systemd_where_the_service_runs():
    """The service runs from /opt/tpms, not from the checkout. An update that
    pulls without deploying there updates nothing the service can see -- which
    is exactly the bug this asserts against."""
    text = (Path(__file__).resolve().parents[1] / "scripts" / "update-pi.sh").read_text()
    assert "WorkingDirectory" in text
    assert "deploy.sh" in text


def test_deploy_copies_the_tree_without_touching_runtime_state(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    prefix = tmp_path / "prefix"
    # A stub venv: deploy.sh skips building one when python is already there,
    # and a real venv would put a minute on the test suite.
    (prefix / ".venv" / "bin").mkdir(parents=True)
    (prefix / ".venv" / "bin" / "python").touch(mode=0o755)
    (prefix / "tpms" / "web").mkdir(parents=True)
    (prefix / "tpms" / "web" / "ghost.py").write_text("deleted upstream\n")
    (prefix / "config.yaml").write_text("port: 9999\n")
    (prefix / "tpms.db").write_text("state\n")

    subprocess.run(
        [str(repo / "scripts" / "deploy.sh"), "--prefix", str(prefix)],
        check=True, cwd=repo, capture_output=True,
    )

    # Code arrives, and code deleted upstream does not survive to be imported.
    assert (prefix / "tpms" / "web" / "app.py").exists()
    assert not (prefix / "tpms" / "web" / "ghost.py").exists()
    # Runtime state is not ours to overwrite.
    assert (prefix / "config.yaml").read_text() == "port: 9999\n"
    assert (prefix / "tpms.db").read_text() == "state\n"
    # The stamp is what lets the updater spot a stale prefix.
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert (prefix / ".deployed").read_text().strip() == head
