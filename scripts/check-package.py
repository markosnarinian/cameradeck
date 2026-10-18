"""Smoke-test an installed wheel, without a checkout, Node, or camera hardware.

Run with the installation's Python: python -I scripts/check-package.py
"""

import io
import os
import re
import subprocess
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path

from PIL import Image

import cameradeck
from cameradeck.app import create_app
from cameradeck.camera import CameraDeck
from cameradeck.scripts.frontend import check


def main():
    check()
    notices = (
        Path(cameradeck.__file__).parent / "static/dist/THIRD_PARTY_LICENSES.txt"
    ).read_text()
    assert "node_modules/react " in notices and "MIT License" in notices
    assert "Apache License" in notices
    command = Path(sys.executable).parent / "cameradeck"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        environment = {**os.environ, "XDG_DATA_HOME": str(root / "data")}
        environment.pop("CAMERADECK_PASSWORD", None)
        help_text = subprocess.check_output(
            [str(command), "--help"], cwd=root, env=environment, text=True
        )
        assert str(root / "data/cameradeck/media") in help_text
        refused = subprocess.run(
            [str(command), "--host", "0.0.0.0"],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
        )
        assert refused.returncode == 2
        assert "Network access requires CAMERADECK_PASSWORD" in refused.stderr
        assert not (root / "data").exists()

        deck = CameraDeck(root / "media")
        image = io.BytesIO()
        Image.new("RGB", (16, 12), "green").save(image, "JPEG")
        deck.frames.write(image.getvalue())
        app = create_app(deck, "package-test-password")
        try:
            assert app.extensions["catalog"].finished.wait(10)
            client = app.test_client()
            assert client.get("/api/status").status_code == 401
            for route in ("/", "/library", "/transfer"):
                response = client.get(route)
                assert response.status_code == 200
                assets = re.findall(rb'(?:src|href)="(/assets/[^\"]+)"', response.data)
                assert assets
                for asset in assets:
                    bundled = client.get(asset.decode())
                    assert bundled.status_code == 200 and bundled.data
            assert (
                client.post(
                    "/api/login", json={"password": "package-test-password"}
                ).status_code
                == 200
            )
            assert client.get("/api/session").json["authenticated"]
            assert client.get("/api/status").status_code == 200
            assert client.get("/api/media").json["items"] == []
            preview = client.get("/api/preview")
            assert preview.status_code == 200
            assert preview.mimetype == "image/jpeg"
            assert preview.data == image.getvalue()
        finally:
            app.extensions["rsync"].close()
            app.extensions["catalog"].close()
            deck.close()
    print(
        f"PASS: installed CameraDeck {version('cameradeck')}: CLI, assets, auth, API, preview"
    )


if __name__ == "__main__":
    main()
