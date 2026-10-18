"""Read-only Raspberry Pi thermal and firmware health, independent of the camera."""

import re
import subprocess
import threading
import time
from pathlib import Path


class PiHealth:
    def __init__(self):
        self.lock = threading.Lock()
        self.expires = 0
        self.reading = None

    def status(self):
        # All browser sessions share one sample, at most once every five seconds.
        with self.lock:
            if time.monotonic() < self.expires:
                return self.reading
            temperature = None
            flags = None
            try:
                zone = Path("/sys/class/thermal/thermal_zone0")
                if zone.joinpath("type").read_text().strip() == "cpu-thermal":
                    temperature = int(zone.joinpath("temp").read_text()) / 1000
            except (OSError, ValueError):
                pass
            try:
                output = subprocess.run(
                    ["/usr/bin/vcgencmd", "get_throttled"],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=0.5,
                ).stdout.strip()
                if match := re.fullmatch(r"throttled=0x([0-9a-fA-F]+)", output):
                    flags = int(match[1], 16)
            except (OSError, subprocess.SubprocessError):
                pass
            names = (
                "Undervoltage",
                "Frequency capped",
                "Throttled",
                "Temperature limit",
            )
            self.reading = dict(
                temperature_c=temperature,
                current=(
                    [name for bit, name in enumerate(names) if flags & (1 << bit)]
                    if flags is not None
                    else None
                ),
                since_boot=(
                    [
                        name
                        for bit, name in enumerate(names)
                        if flags & (1 << (bit + 16))
                    ]
                    if flags is not None
                    else None
                ),
            )
            self.expires = time.monotonic() + 5
            return self.reading
