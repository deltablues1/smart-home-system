"""
No unit may run the assistant as root.

deploy/rpi/setup.sh copies every *.service into /etc/systemd/system, and two of
the five had no User= at all — systemd's default is root — plus a WorkingDirectory
of /opt/google-clause, a path that does not exist on the Pi. The live units are
correct; the repo would have overwritten them on the next setup.sh run.

Run with:
    pytest tests/unit/test_systemd_units.py -v
"""

from pathlib import Path

import pytest

UNITS = sorted(
    (Path(__file__).resolve().parents[2] / "deploy" / "rpi" / "systemd").glob("*.service")
)
APP_DIR = "/home/raspberrypijarvis/google_claude"


def _directives(unit: Path) -> dict:
    out = {}
    for line in unit.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#") and not line.startswith("["):
            key, _, value = line.partition("=")
            out.setdefault(key, []).append(value)
    return out


def test_there_are_units_to_check():
    assert UNITS, "no systemd units found"


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
class TestEveryUnit:
    def test_declares_a_non_root_user(self, unit):
        users = _directives(unit).get("User", [])
        assert users, f"{unit.name} has no User= and would run as root"
        assert users[0] != "root"

    def test_points_at_the_path_the_pi_uses(self, unit):
        directives = _directives(unit)
        assert directives["WorkingDirectory"][0] == APP_DIR
        assert directives["EnvironmentFile"][0].startswith(APP_DIR)

    def test_runs_the_project_virtualenv(self, unit):
        assert _directives(unit)["ExecStart"][0].startswith(f"{APP_DIR}/.venv/bin/python")
