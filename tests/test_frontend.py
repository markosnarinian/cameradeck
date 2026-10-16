"""Production build/install failure safety, using real npm and disposable sources."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def checkout(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts/frontend.py", tmp_path / "scripts/frontend.py")
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "src").mkdir()
    (frontend / "src/main.ts").write_text("first source")
    node = subprocess.check_output(["node", "--version"], text=True).strip()
    npm = subprocess.check_output(["npm", "--version"], text=True).strip()
    (tmp_path / ".node-version").write_text(node.removeprefix("v"))
    package = {
        "name": "build-fixture",
        "version": "1.0.0",
        "packageManager": f"npm@{npm}",
        "scripts": {"build": "node build.mjs"},
    }
    (frontend / "package.json").write_text(json.dumps(package))
    (frontend / "package-lock.json").write_text(
        json.dumps(
            {
                "name": "build-fixture",
                "version": "1.0.0",
                "lockfileVersion": 3,
                "packages": {"": {"name": "build-fixture", "version": "1.0.0"}},
            }
        )
    )
    (frontend / "build.mjs").write_text("""
import fs from 'node:fs';
const output = process.argv.at(-1);
fs.mkdirSync(output, {recursive: true});
fs.writeFileSync(output + '/index.html', fs.readFileSync('src/main.ts'));
""")
    return tmp_path


def run(root, action):
    return subprocess.run(
        [sys.executable, "scripts/frontend.py", action],
        cwd=root,
        env={**os.environ, "npm_config_audit": "false", "npm_config_fund": "false"},
        capture_output=True,
        text=True,
    )


def contents(root):
    return {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_clean_build_update_and_startup_guard(checkout):
    assert run(checkout, "check").returncode == 1
    result = run(checkout, "build")
    assert result.returncode == 0, result.stderr
    assert run(checkout, "check").returncode == 0
    bundle = checkout / "static/dist"
    assert (bundle / "index.html").read_text() == "first source"
    (checkout / "frontend/src/main.ts").write_text("updated source")
    assert run(checkout, "check").returncode == 1
    # The app's actual entry point must refuse startup before opening a camera.
    shutil.copy(ROOT / "app.py", checkout / "app.py")
    result = subprocess.run(
        [sys.executable, str(checkout / "app.py")],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
    )
    assert (
        result.returncode != 0 and "Frontend missing, stale or damaged" in result.stderr
    )
    assert not (checkout / "media").exists()
    result = run(checkout, "build")
    assert result.returncode == 0, result.stderr
    assert run(checkout, "check").returncode == 0
    assert (bundle / "index.html").read_text() == "updated source"
    (bundle / "index.html").write_text("damaged")
    assert run(checkout, "check").returncode == 1


@pytest.mark.parametrize("failure", ["node", "npm", "ci", "build"])
def test_failed_build_preserves_installed_bundle(checkout, failure):
    result = run(checkout, "build")
    assert result.returncode == 0, result.stderr
    bundle = checkout / "static/dist"
    previous = contents(bundle)
    frontend = checkout / "frontend"
    if failure == "node":
        (checkout / ".node-version").write_text("0.0.0")
    elif failure == "npm":
        package = json.loads((frontend / "package.json").read_text())
        package["packageManager"] = "npm@0.0.0"
        (frontend / "package.json").write_text(json.dumps(package))
    elif failure == "ci":
        (frontend / "package-lock.json").unlink()
    else:
        with (frontend / "build.mjs").open("a") as script:
            script.write("\nprocess.exit(2);\n")
    result = run(checkout, "build")
    assert result.returncode != 0, result.stdout
    assert contents(bundle) == previous
    assert run(checkout, "check").returncode == 1
    assert not list((checkout / "static").glob(".frontend-*"))
