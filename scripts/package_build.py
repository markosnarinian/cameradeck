"""Include a verified UI in both distributions; rebuild only when needed."""

import subprocess
import sys
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        helper = Path(self.root) / "scripts/frontend.py"
        check = subprocess.run(
            [sys.executable, str(helper), "check"], capture_output=True
        )
        if check.returncode:
            subprocess.run([sys.executable, str(helper), "build"], check=True)
