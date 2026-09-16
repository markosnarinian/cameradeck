"""Narrow host power control used by CameraDeck."""

import logging
import subprocess
import threading


class PowerManager:
    COMMANDS = {
        "shutdown": ("/usr/bin/systemctl", "poweroff"),
        "reboot": ("/usr/bin/systemctl", "reboot"),
    }

    def __init__(self, executor=None, delay=1):
        self.executor = executor or self._execute
        self.delay = delay
        self.pending = False
        self.lock = threading.Lock()

    @staticmethod
    def _execute(command):
        subprocess.run(command, check=True, timeout=15)

    def schedule(self, action):
        if action not in self.COMMANDS:
            raise ValueError("Choose shutdown or reboot.")
        with self.lock:
            if self.pending:
                raise RuntimeError("A power action is already pending.")
            self.pending = True
        command = self.COMMANDS[action]

        def run():
            try:
                self.executor(command)
            except Exception:
                logging.exception("Power action failed")
                with self.lock:
                    self.pending = False

        threading.Timer(self.delay, run).start()
