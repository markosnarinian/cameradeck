#!/usr/bin/env python3
"""Build the locked frontend while stopped, or check it without Node at startup."""

import argparse
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def source_hash(root):
    frontend = root / "frontend"
    files = [
        p
        for p in frontend.iterdir()
        if p.is_file() and not p.name.endswith(".tsbuildinfo")
    ]
    files += [p for p in (frontend / "src").rglob("*") if p.is_file()]
    files += [p for p in (frontend / "licenses").rglob("*") if p.is_file()]
    files += [root / ".node-version"]
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(str(path.relative_to(root)).encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def bundle_hashes(bundle):
    return {
        str(path.relative_to(bundle)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(bundle.rglob("*"))
        if path.is_file() and path.name != ".build.json"
    }


def check(root=ROOT):
    bundle = root / "static" / "dist"
    try:
        stamp = json.loads((bundle / ".build.json").read_text())
        valid = (
            (bundle / "index.html").is_file()
            and stamp["source"] == source_hash(root)
            and stamp["files"] == bundle_hashes(bundle)
        )
    except (OSError, ValueError, KeyError, TypeError):
        valid = False
    if not valid:
        raise RuntimeError(
            "Frontend missing, stale or damaged. While CameraDeck is stopped, "
            "run: python3 scripts/frontend.py build"
        )


def build(root=ROOT):
    frontend = root / "frontend"
    node = (root / ".node-version").read_text().strip()
    npm = json.loads((frontend / "package.json").read_text())[
        "packageManager"
    ].removeprefix("npm@")
    for command, expected in (("node", "v" + node), ("npm", npm)):
        actual = subprocess.check_output([command, "--version"], text=True).strip()
        if actual != expected:
            raise RuntimeError(
                f"Expected {command} {expected}, found {actual}. See README.md."
            )
    source = source_hash(root)
    # npm ci may fail or a build may clear its output before failing. Neither
    # may touch the installed bundle. Keep staging on the same filesystem.
    subprocess.run(["npm", "ci", "--include=dev"], cwd=frontend, check=True)
    static = root / "static"
    static.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".frontend-", dir=static) as temporary:
        stage = Path(temporary) / "dist"
        subprocess.run(
            ["npm", "run", "build", "--", "--outDir", str(stage)],
            cwd=frontend,
            check=True,
        )
        if not (stage / "index.html").is_file() or source != source_hash(root):
            raise RuntimeError(
                "Missing build output or source changed during build; retry while stopped."
            )
        notices = ["Third-party frontend licenses\n"]
        packages = json.loads((frontend / "package-lock.json").read_text())["packages"]
        for name, package in sorted(packages.items()):
            if not name or package.get("dev"):
                continue
            directory = frontend / name
            licenses = [
                path
                for path in sorted(directory.iterdir())
                if path.is_file()
                and path.name.lower().startswith(
                    ("license", "licence", "notice", "copying")
                )
            ]
            if not licenses:
                licenses = [frontend / "licenses" / (directory.name + ".txt")]
            notices.append(
                f"\n{name} {package['version']} ({package.get('license', '')})\n"
            )
            notices.extend(path.read_text() for path in licenses)
        (stage / "THIRD_PARTY_LICENSES.txt").write_text("\n".join(notices))
        (stage / ".build.json").write_text(
            json.dumps(
                {
                    "source": source,
                    "files": bundle_hashes(stage),
                }
            )
            + "\n"
        )
        installed = static / "dist"
        previous = Path(temporary) / "previous"
        if installed.exists():
            installed.rename(previous)
        try:
            stage.rename(installed)
        except OSError:
            if previous.exists():
                previous.rename(installed)
            raise
    check(root)
    print("Frontend built and verified; safe to start CameraDeck.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "check"))
    args = parser.parse_args()
    try:
        (build if args.action == "build" else check)()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"{exc}\nCameraDeck was not started or restarted.\n")
