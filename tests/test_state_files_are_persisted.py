"""Every persistent state file must be pointed into the /var/data
volume by deploy/compose.yaml.

The config defaults are RELATIVE ("conf/x.json"), so any key without a
matching environment line in compose resolves under the container's
workdir /app - which is baked into the image and is NOT a volume. The
file is then silently destroyed on every restart and redeploy. Nothing
errors, no warning is logged; the store just quietly forgets.

Found live 2026-09-24: position_open_times.json was being written
perfectly well to /app/conf, with correct entries, while the feature
that depends on it - closing option positions carried past their own
end-of-day close - could never work, because that store's entire
purpose is surviving a restart. The same audit found strategy_tuning
had been doing it unnoticed for much longer.

This is a wiring mistake that no unit test of the store itself can
catch, because the store works correctly either way.
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMPOSE = REPO / "deploy" / "compose.yaml"
SETTINGS = REPO / "src" / "webull_bot" / "config_sections"

# status_file and command_file are covered too - they are just mounted
# at different paths (/var/data and /var/commands respectively).
EXPECTED_VOLUME_ROOTS = ("/var/data", "/var/commands")


def _declared_state_keys() -> dict[str, str]:
    """Config keys whose default is a relative path to a state file."""
    found: dict[str, str] = {}
    for path in SETTINGS.glob("*.py"):
        for match in re.finditer(
            r"^\s{4}(\w+_file)\s*:\s*str\s*=\s*[\"']([^\"']+)[\"']",
            path.read_text(encoding="utf-8"),
            re.MULTILINE,
        ):
            key, default = match.group(1), match.group(2)
            if default.startswith("/"):
                continue  # already absolute, cannot land outside a volume
            found[key] = default
    return found


class StateFilesArePersistedTests(unittest.TestCase):
    def test_every_relative_state_file_is_mapped_into_a_volume(self):
        compose = COMPOSE.read_text(encoding="utf-8")
        missing = []
        outside = []
        for key, default in sorted(_declared_state_keys().items()):
            env_name = key.upper()
            match = re.search(
                rf"^\s*{env_name}:\s*(\S+)\s*$", compose, re.MULTILINE
            )
            if match is None:
                missing.append(f"{key} (default {default!r})")
                continue
            value = match.group(1)
            if not value.startswith(EXPECTED_VOLUME_ROOTS):
                outside.append(f"{key} -> {value}")
        self.assertEqual(
            missing,
            [],
            "These state files have no environment line in "
            "deploy/compose.yaml, so they resolve under /app and are "
            "DESTROYED on every restart:\n  "
            + "\n  ".join(missing),
        )
        self.assertEqual(
            outside,
            [],
            "These state files are mapped outside a mounted volume:\n  "
            + "\n  ".join(outside),
        )

    def test_the_audit_actually_finds_the_known_keys(self):
        """Guards the regex itself - a scanner that silently matches
        nothing would make the test above vacuously pass.
        """
        keys = _declared_state_keys()
        for expected in (
            "wash_sale_state_file",
            "daily_pnl_state_file",
            "position_open_times_state_file",
            "strategy_tuning_state_file",
        ):
            self.assertIn(expected, keys)


if __name__ == "__main__":
    unittest.main()
