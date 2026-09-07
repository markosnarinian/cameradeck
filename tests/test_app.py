import json
from unittest.mock import Mock

import numpy as np
import pytest
from PIL import Image, ImageFilter

from app import create_app
from camera import CameraDeck, focus_grid, validate_controls


@pytest.fixture
def deck(tmp_path):
    return CameraDeck(tmp_path)


@pytest.fixture
def client(deck):
    return create_app(deck).test_client()


def test_no_camera_status_and_capture(client):
    assert client.get("/api/status").json["ready"] is False
    assert client.post("/api/capture", json={}).status_code == 400
    assert client.get("/").status_code == 200


def test_authentication_and_origin(deck):
    client = create_app(deck, "test-password").test_client()
    for route in ["/api/status", "/api/media", "/stream.mjpg", "/media/test/original"]:
        assert client.get(route).status_code == 401
    assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
    assert (
        client.post("/api/login", json={"password": "test-password"}).status_code == 200
    )
    assert client.get("/api/status").status_code == 200
    assert (
        client.post(
            "/api/focus",
            json={"enabled": True},
            headers={"Origin": "https://evil.example"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/focus", json={"enabled": True}, headers={"Origin": "http://localhost"}
        ).status_code
        == 200
    )


def test_login_rate_limit(deck):
    client = create_app(deck, "password").test_client()
    for _ in range(10):
        assert client.post("/api/login", json={"password": "bad"}).status_code == 401
    assert client.post("/api/login", json={"password": "password"}).status_code == 429


def test_library_derivatives_ranges_and_delete(deck, client):
    item = deck._new("still")
    item["file"] = item["id"] + ".jpg"
    image = Image.new("RGB", (2400, 1600), "#778866")
    image.save(deck.root / item["file"])
    deck._finish(item, image)
    assert client.get("/api/media").json["total"] == 1
    assert client.get("/api/media?kind=video").json["total"] == 0
    assert client.get("/api/media?offset=40").json["items"] == []
    ident = item["id"]
    assert Image.open(deck.root / (ident + ".thumb.jpg")).width == 480
    assert Image.open(deck.root / (ident + ".preview.jpg")).width == 1600
    r = client.get(f"/media/{ident}/original", headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and len(r.data) == 10
    assert (
        "attachment"
        in client.get(f"/media/{ident}/original?download=1").headers[
            "Content-Disposition"
        ]
    )
    assert client.get("/api/media/../../etc/passwd").status_code == 404
    assert client.get(f"/media/{ident}/invalid").status_code == 404
    assert client.delete(f"/api/media/{ident}").status_code == 200
    assert not list(deck.root.iterdir())
    assert client.get(f"/media/{ident}/original").status_code == 404


def test_corrupt_sidecar_does_not_break_library(deck, client):
    (deck.root / "broken.json").write_text("{")
    assert client.get("/api/media").json["items"] == []


def spec(kind="Float", low=0, high=10, size=0, options=None):
    return dict(type=kind, min=low, max=high, size=size, options=options or {})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, 11, "2", True, None])
def test_invalid_numeric_controls(value):
    with pytest.raises(ValueError):
        validate_controls({"Gain": value}, {"Gain": spec()})


def test_controls_types_arrays_enums_and_crop():
    schema = {
        "Gain": spec(),
        "Enable": spec("Bool"),
        "Mode": spec("Integer32", options={"Auto": 0, "Manual": 1}),
        "ColourGains": spec(size=2),
        "ScalerCrop": spec("Rectangle"),
        "FrameDurationLimits": spec("Integer64", 1, 1000000, 2),
    }
    assert validate_controls({"ColourGains": [1.0, 2.0]}, schema) == {
        "ColourGains": (1.0, 2.0)
    }
    assert validate_controls({"ScalerCrop": [0, 0, 100, 100]}, schema)[
        "ScalerCrop"
    ] == (0, 0, 100, 100)
    for values in [
        {},
        {"Unknown": 1},
        {"Mode": 2},
        {"Mode": 1.5},
        {"Enable": 1},
        {"ColourGains": [1]},
        {"ScalerCrop": [-1, 0, 2, 2]},
        {"FrameDurationLimits": [200, 100]},
    ]:
        with pytest.raises(ValueError):
            validate_controls(values, schema)


def test_focus_grid_distinguishes_blur():
    rng = np.random.default_rng(10)
    image = Image.fromarray(rng.integers(0, 256, (270, 480), dtype=np.uint8))
    sharp = focus_grid(image)
    blurred = focus_grid(image.filter(ImageFilter.GaussianBlur(3)))
    assert len(sharp) == 9
    assert min(sharp) > max(blurred)
    assert focus_grid(Image.new("RGB", (480, 270), "gray")) == [0.0] * 9


def test_recording_blocks_reconfigure(deck):
    deck.recording = {"item": {"id": "ongoing"}}
    with pytest.raises(ValueError, match="Stop recording"):
        deck.open(0, "1080p", 15)


def test_storage_guard(deck, monkeypatch):
    monkeypatch.setattr("camera.shutil.disk_usage", lambda _: Mock(free=1024))
    with pytest.raises(ValueError, match="256 MB"):
        deck._new("still")


def test_stop_without_recording(client):
    assert client.post("/api/record/stop", json={}).status_code == 400
