"""Supervised rsync push of the media directory, started from the web UI."""

import os
import re
import shlex
import signal
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

# Keep in step with the Mac pull command on the Transfer page and in README.md.
EXCLUDES = ("*.preview.jpg", "*.thumb.jpg", ".*", "*.tmp", "*.pending.*")
SSH = "ssh -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=4"
REMOTE = re.compile(
    r"(?:[A-Za-z0-9._-]+@)?(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\]):(?!:)(.+)", re.S
)
PROGRESS = re.compile(
    r"\s*(\S+)\s+(\d+)%\s+(\S+/s)\s+(\d+:\d{2}:\d{2})"
    r"(?:\s+\(xfr#(\d+), (ir|to)-chk=(\d+)/(\d+)\))?"
)
EXIT_MESSAGES = {
    1: "rsync rejected an option.",
    2: "The destination rsync is incompatible.",
    3: "Source or destination paths could not be selected.",
    5: "rsync could not start the remote side.",
    10: "Socket I/O error.",
    11: "File I/O error. Check destination space and permissions.",
    12: "The connection to the destination failed. Check SSH key access, the host key and Remote Login.",
    23: "Some files were not transferred. See the log.",
    30: "Timed out waiting for the destination.",
    35: "Timed out waiting for the destination.",
    127: "rsync is not installed on the destination.",
    255: "SSH failed. Check the host, key access and that the host key is known.",
}


class Busy(RuntimeError):
    pass


def public_key():
    for name in ("id_ed25519.pub", "id_ecdsa.pub", "id_rsa.pub"):
        path = Path.home() / ".ssh" / name
        if path.is_file():
            return path.read_text().strip()
    return None


class RsyncTransfer:
    def __init__(self, root, rsync="rsync"):
        self.root = Path(root)
        self.rsync = rsync
        self.saved = self.root / ".rsync-destination"
        self.lock = threading.Lock()
        self.process = None
        self.worker = None
        self.state = "idle"
        self.job = {}
        try:
            self.destination = self.saved.read_text().strip()
        except OSError:
            self.destination = ""

    @property
    def active(self):
        return self.state in {"running", "stopping"}

    def validate(self, destination):
        if not isinstance(destination, str) or not destination.strip():
            raise ValueError("Enter an rsync destination.")
        destination = destination.strip()
        if len(destination) > 1024 or any(ord(c) < 32 for c in destination):
            raise ValueError("Destination contains unsupported characters.")
        if destination.startswith("-"):
            raise ValueError("Destination must not start with '-'.")
        if destination.startswith("/"):
            path = Path(destination).resolve()
            if not path.is_dir():
                raise ValueError("Local destination must be an existing directory.")
            if path == self.root.resolve() or self.root.resolve() in path.parents:
                raise ValueError("Destination must be outside the media directory.")
        elif not REMOTE.fullmatch(destination):
            raise ValueError(
                "Use user@host:/path for SSH, or an absolute path on the Pi."
            )
        return destination

    def command(self, destination):
        return [
            self.rsync,
            "-r",
            "--whole-file",
            "--info=progress2,stats2",
            "--human-readable",
            "--ignore-existing",
            "--outbuf=N",
            "--timeout=120",
            "-e",
            SSH,
            *(f"--exclude={pattern}" for pattern in EXCLUDES),
            f"{self.root}/",
            destination,
        ]

    def start(self, destination):
        destination = self.validate(destination)
        with self.lock:
            if self.active:
                raise Busy("An rsync transfer is already running.")
            argv = self.command(destination)
            env = {**os.environ, "LC_ALL": "C"}
            try:
                # A separate session lets Stop reach ssh as well as rsync.
                self.process = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    env=env,
                    start_new_session=True,
                )
            except OSError as exc:
                raise RuntimeError(f"Cannot start rsync: {exc}") from exc
            self.destination = destination
            try:
                self.saved.write_text(destination + "\n")
            except OSError:
                pass
            self.state = "running"
            self.job = dict(
                destination=destination,
                command=shlex.join(argv),
                started=datetime.now(timezone.utc).isoformat(),
                monotonic=time.monotonic(),
                finished=None,
                elapsed=0,
                exit_code=None,
                message="",
                progress=None,
                log=deque(maxlen=200),
                cancelled=False,
            )
            self.worker = threading.Thread(
                target=self._supervise, args=(self.process, self.job), daemon=True
            )
            self.worker.start()
        return self.status()

    def _record(self, job, line):
        match = PROGRESS.match(line)
        if match:
            transferred, percent, rate, eta, files, check, left, total = match.groups()
            job["progress"] = dict(
                transferred=transferred,
                percent=int(percent),
                rate=rate,
                eta=eta,
                files=int(files) if files else None,
                remaining=int(left) if left else None,
                total=int(total) if total else None,
                scanning=check == "ir",
            )
        elif line.strip():
            job["log"].append(line.rstrip())

    def _supervise(self, process, job):
        pending = b""
        while chunk := os.read(process.stdout.fileno(), 65536):
            parts = re.split(rb"[\r\n]", pending + chunk)
            pending = parts.pop()
            with self.lock:
                for part in parts:
                    self._record(job, part.decode(errors="replace"))
        code = process.wait()
        process.stdout.close()
        with self.lock:
            if pending:
                self._record(job, pending.decode(errors="replace"))
            job["exit_code"] = code
            job["finished"] = datetime.now(timezone.utc).isoformat()
            job["elapsed"] = round(time.monotonic() - job["monotonic"])
            if job["cancelled"]:
                self.state, job["message"] = "cancelled", "Stopped before finishing."
            elif code == 0:
                self.state, job["message"] = "succeeded", "Transfer complete."
            elif code == 24:
                self.state = "succeeded"
                job["message"] = "Transfer complete; some files vanished during it."
            else:
                self.state = "failed"
                job["message"] = EXIT_MESSAGES.get(
                    code, f"rsync exited with code {code}."
                )
            self.process = None

    def stop(self):
        with self.lock:
            if self.state != "running" or not self.process:
                return self.status_locked()
            self.job["cancelled"] = True
            self.state = "stopping"
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            return self.status_locked()

    def status_locked(self):
        job = {key: value for key, value in self.job.items() if key != "monotonic"}
        if job:
            job["log"] = list(job["log"])
            if self.active:
                job["elapsed"] = round(time.monotonic() - self.job["monotonic"])
        return dict(
            state=self.state,
            active=self.active,
            destination=self.destination,
            source=f"{self.root}/",
            job=job or None,
        )

    def status(self):
        with self.lock:
            return self.status_locked()

    def close(self, timeout=5):
        self.stop()
        worker = self.worker
        if worker:
            worker.join(timeout)
            with self.lock:
                if self.process:
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
