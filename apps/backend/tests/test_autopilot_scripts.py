"""Tests for the autopilot launchd shell scripts, run against a fake launchctl.

The fake keeps one marker file per loaded label; ``bootout`` removes it unless a
``stuck-<label>`` marker says the agent refuses to unload.
"""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
LABELS = ["com.fplcopilot.server", "com.fplcopilot.refresh", "com.fplcopilot.deadline"]

FAKE_LAUNCHCTL = """#!/usr/bin/env bash
state="$FAKE_LAUNCHD_STATE"
label="${2##*/}"
case "$1" in
  print) [ -e "$state/loaded-$label" ] ;;
  bootout) [ -e "$state/stuck-$label" ] || rm -f "$state/loaded-$label" ;;
  *) exit 0 ;;
esac
"""


@pytest.fixture
def fake_env(tmp_path):
    """A HOME with all three plists installed and a fake launchctl on PATH."""
    home = tmp_path / "home"
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    for label in LABELS:
        (agents / f"{label}.plist").write_text("<plist/>")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    launchctl = bindir / "launchctl"
    launchctl.write_text(FAKE_LAUNCHCTL)
    launchctl.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_LAUNCHD_STATE": str(state),
        "LAUNCHD_WAIT_POLLS": "2",
        "LAUNCHD_POLL_SLEEP": "0",
    }
    return env, agents, state


def _uninstall(env):
    return subprocess.run(
        ["bash", str(SCRIPTS / "uninstall-autopilot.sh")],
        env=env, capture_output=True, text=True, check=False,
    )


def test_uninstall_unloads_and_removes_all_agents(fake_env):
    env, agents, state = fake_env
    for label in LABELS:
        (state / f"loaded-{label}").touch()
    result = _uninstall(env)
    assert result.returncode == 0, result.stderr
    assert "Autopilot off." in result.stdout
    assert list(agents.iterdir()) == []


def test_uninstall_handles_agents_that_were_never_loaded(fake_env):
    env, agents, _ = fake_env
    result = _uninstall(env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("not loaded") == len(LABELS)
    assert list(agents.iterdir()) == []


def test_uninstall_fails_when_an_agent_stays_loaded(fake_env):
    """Regression: an unload timeout used to be swallowed and reported as success."""
    env, agents, state = fake_env
    for label in LABELS:
        (state / f"loaded-{label}").touch()
    (state / "stuck-com.fplcopilot.refresh").touch()
    result = _uninstall(env)
    assert result.returncode == 1
    assert "Autopilot off." not in result.stdout
    assert "com.fplcopilot.refresh is still loaded" in result.stderr
    # The stuck agent keeps its plist; the others are still processed and removed.
    assert sorted(p.name for p in agents.iterdir()) == ["com.fplcopilot.refresh.plist"]
