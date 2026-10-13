"""Photo-only HTTP contracts and surviving media/storage compatibility."""

import io
import json
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
from botocore.exceptions import ClientError
from PIL import Image

from app import ID, create_app
from camera import BurstSampler, CameraDeck, validate_controls, yuv420_planes
from storage import S3Uploader


@pytest.fixture
def deck(tmp_path):
    return CameraDeck(tmp_path)


@pytest.fixture(autouse=True)
def app_factory():
    """Wait for startup imports, and close every catalog including restarted apps."""
    apps = []

    def factory(*args, **kwargs):
        app = create_app(*args, **kwargs)
        apps.append(app)
        assert app.extensions["catalog"].finished.wait(10)
        return app

    yield factory
    for app in apps:
        app.extensions["catalog"].close()


@pytest.fixture
def client(deck, app_factory):
    return app_factory(deck).test_client()


def make_media(deck, content=b"photo", ident=None):
    item = deck._new("still")
    if ident:
        item["id"] = ident
    item.update(file=item["id"] + ".jpg", bytes=len(content), width=1, height=1)
    (deck.root / item["file"]).write_bytes(content)
    (deck.root / (item["id"] + ".thumb.jpg")).write_bytes(b"thumb")
    (deck.root / (item["id"] + ".preview.jpg")).write_bytes(b"preview")
    (deck.root / (item["id"] + ".json")).write_text(json.dumps(item))
    if deck.media_published:
        deck.media_published(item)
    return item


def test_no_camera_status_and_capture(client):
    assert client.get("/api/status").json["ready"] is False
    assert client.post("/api/capture", json={}).status_code == 400
    for route in ("/", "/library", "/transfer"):
        response = client.get(route)
        assert response.status_code == 200
        assert b"/assets/" in response.data


def test_network_startup_preserves_existing_password(tmp_path, monkeypatch):
    from app import main

    monkeypatch.setattr(
        sys, "argv", ["app.py", "--host", "0.0.0.0", "--media", str(tmp_path)]
    )
    monkeypatch.setenv("CAMERADECK_PASSWORD", "short")
    monkeypatch.setattr(CameraDeck, "start", lambda self: None)
    monkeypatch.setattr("signal.signal", lambda *args: None)
    responses = []

    def serve(app, **kwargs):
        client = app.test_client()
        responses.append(
            client.post("/api/login", json={"password": "short"}).status_code
        )
        responses.append(client.get("/api/status").status_code)

    monkeypatch.setattr("waitress.serve", serve)
    main()
    assert responses == [200, 200]
    monkeypatch.delenv("CAMERADECK_PASSWORD")
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


def test_authentication_and_origin(deck, app_factory):
    client = app_factory(deck, "test-password").test_client()
    assert client.get("/api/session").json == {"authenticated": False, "required": True}
    for route in ["/api/status", "/api/media", "/stream.mjpg", "/media/test/original"]:
        assert client.get(route).status_code == 401
    for route in ("/", "/library", "/transfer"):
        assert client.get(route).status_code == 200
    assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
    assert (
        client.post("/api/login", json={"password": "test-password"}).status_code == 200
    )
    assert client.get("/api/session").json == {"authenticated": True, "required": True}
    cookie = client.get_cookie("session")
    restarted = app_factory(CameraDeck(deck.root), "test-password").test_client()
    restarted.set_cookie("session", cookie.value)
    assert restarted.get("/api/status").status_code == 200
    changed = app_factory(CameraDeck(deck.root), "changed-password").test_client()
    changed.set_cookie("session", cookie.value)
    assert changed.get("/api/status").status_code == 401
    assert (
        client.post(
            "/api/controls", json={}, headers={"Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    # Same-origin requests reach validation rather than the origin guard.
    assert (
        client.post(
            "/api/controls", json={}, headers={"Origin": "http://localhost"}
        ).status_code
        == 400
    )
    assert restarted.post("/api/logout", json={}).status_code == 200
    assert restarted.get("/api/session").json["authenticated"] is False


def test_login_rate_limit(deck, app_factory):
    client = app_factory(deck, "password").test_client()
    for _ in range(10):
        assert client.post("/api/login", json={"password": "bad"}).status_code == 401
    assert client.post("/api/login", json={"password": "password"}).status_code == 429


def test_removed_routes_and_public_assets(client):
    for route in (
        "/api/focus",
        "/api/record/start",
        "/api/record/stop",
        "/api/experiments",
        "/missing",
    ):
        assert client.get(route).status_code == 404
        assert client.post(route, json={}).status_code == 404
    html = client.get("/")
    assert html.status_code == 200
    assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', html.get_data(as_text=True))
    assert assets
    for asset in assets:
        assert client.get(asset).status_code == 200


def test_request_validation_and_host(client):
    assert (
        client.get("/api/status", headers={"Host": "evil.example"}).status_code == 403
    )
    assert client.post("/api/configure", json=[]).status_code == 400
    assert client.post("/api/controls", data="Brightness=1").status_code == 400
    assert client.get("/api/session").json == {"authenticated": True, "required": False}


def test_preview_requires_fresh_frame(deck, client, monkeypatch):
    assert client.get("/api/preview").status_code == 503
    deck.frames.write(b"test-jpeg")
    response = client.get("/api/preview")
    assert response.status_code == 200
    assert response.mimetype == "image/jpeg" and response.data == b"test-jpeg"
    monkeypatch.setattr("app.time.monotonic", lambda: float("inf"))
    assert client.get("/api/preview").status_code == 503


def test_stream_limit_releases_slots(deck, client):
    deck.frames.write(b"test-jpeg")
    streams = [client.get("/stream.mjpg", buffered=False) for _ in range(4)]
    try:
        assert all(r.status_code == 200 for r in streams)
        assert client.get("/stream.mjpg").status_code == 503
        streams.pop().close()
        replacement = client.get("/stream.mjpg", buffered=False)
        assert replacement.status_code == 200
        replacement.close()
    finally:
        for response in streams:
            response.close()


def test_library_derivatives_ranges_and_delete(deck, client):
    item = deck._new("still")
    item["file"] = item["id"] + ".jpg"
    image = Image.new("RGB", (2400, 1600), "#778866")
    image.save(deck.root / item["file"])
    deck._finish(item, image)
    assert client.get("/api/media").json["total"] == 1
    assert client.get("/api/media?kind=video").json["total"] == 0
    assert client.get("/api/media?page=2").json["page"] == 1
    ident = item["id"]
    assert Image.open(deck.root / (ident + ".thumb.jpg")).width == 480
    assert Image.open(deck.root / (ident + ".preview.jpg")).width == 1600
    response = client.get(f"/media/{ident}/original", headers={"Range": "bytes=0-9"})
    assert response.status_code == 206 and len(response.data) == 10
    for variant in ("original", "metadata"):
        assert (
            "attachment"
            in client.get(f"/media/{ident}/{variant}?download=1").headers[
                "Content-Disposition"
            ]
        )
    assert client.get("/api/media/../../etc/passwd").status_code == 404
    assert client.get(f"/media/{ident}/invalid").status_code == 404
    assert client.delete(f"/api/media/{ident}").status_code == 200
    assert not list(deck.root.glob(ident + ".*"))
    assert client.get("/api/media").json["total"] == 0
    assert client.get(f"/media/{ident}/original").status_code == 404


def test_catalog_startup_and_publication_do_not_read_sidecars_on_requests(
    deck, app_factory, monkeypatch
):
    first = make_media(deck, ident="20260907_100000_12345678")
    make_media(deck, ident="20260907_100001_12345678")
    client = app_factory(deck).test_client()
    third = make_media(deck, ident="20260907_100002_12345678")
    assert client.get(f'/api/media/{first["id"]}').status_code == 200
    assert client.get(f'/api/media/{third["id"]}').status_code == 200

    def forbidden(*args, **kwargs):
        pytest.fail("Library requests must query the catalog, not scan/read sidecars")

    monkeypatch.setattr(type(deck.root), "glob", forbidden)
    monkeypatch.setattr(type(deck.root), "read_text", forbidden)
    for query in ("", "?kind=still", "?page=2&per_page=1"):
        response = client.get("/api/media" + query)
        assert response.json["total"] == 3
        assert response.json["indexing"] is False
        assert response.json["index_error"] is None


def test_library_remains_available_during_background_import(deck, monkeypatch):
    import builtins

    make_media(deck)
    make_media(deck)
    entered, release, responded = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    original_open = builtins.open
    reads = []

    def slow_sidecar(path, *args, **kwargs):
        if str(path).endswith(".json"):
            reads.append(path)
            if len(reads) == 2:
                entered.set()
                release.wait(5)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", slow_sidecar)
    app = create_app(deck)
    result = []

    def request_page():
        result.append(app.test_client().get("/api/media").json)
        responded.set()

    request_thread = threading.Thread(target=request_page)
    try:
        assert entered.wait(2)
        request_thread.start()
        assert responded.wait(
            1
        ), "A background file read must not block library requests"
        assert result[0]["indexing"] is True
        assert result[0]["total"] == 0
        deleted = type(deck.root)(reads[0]).stem
        assert app.test_client().delete("/api/media/" + deleted).status_code == 200
        release.set()
        assert app.extensions["catalog"].finished.wait(2)
        remaining = app.test_client().get("/api/media").json
        assert remaining["total"] == 1
        assert remaining["items"][0]["id"] != deleted
    finally:
        release.set()
        if request_thread.ident is not None:
            request_thread.join(5)
        app.extensions["catalog"].close()


@pytest.mark.parametrize("corruption", ["{", [], {}])
def test_catalog_reconciles_changed_and_corrupt_sidecars_on_restart(
    deck, app_factory, corruption
):
    item = make_media(deck)
    first = app_factory(deck)
    assert first.test_client().get("/api/media").json["total"] == 1
    first.extensions["catalog"].close()
    item["width"] = 42
    sidecar = deck.root / (item["id"] + ".json")
    sidecar.write_text(json.dumps(item))
    second = app_factory(CameraDeck(deck.root))
    assert second.test_client().get(f'/api/media/{item["id"]}').json["width"] == 42
    second.extensions["catalog"].close()
    sidecar.write_text(
        corruption
        if isinstance(corruption, str)
        else json.dumps({**item, "kind": corruption})
    )
    restarted = CameraDeck(deck.root)
    valid = make_media(restarted)
    third = app_factory(restarted)
    listing = third.test_client().get("/api/media").json
    assert listing["indexing"] is False and listing["index_error"] is None
    assert listing["total"] == 1 and listing["items"] == [valid]
    assert sidecar.exists() and (deck.root / item["file"]).read_bytes() == b"photo"


def test_pending_recording_is_not_published(deck, client):
    pending = deck._new("video")
    (deck.root / (pending["id"] + ".pending.json")).write_text(json.dumps(pending))
    assert client.get("/api/media").json["items"] == []


def test_unrecoverable_recording_preserved(deck):
    pending = deck._new("video")
    pending["file"] = pending["id"] + ".mp4"
    path = deck.root / (pending["id"] + ".pending.json")
    path.write_text(json.dumps(pending))
    (deck.root / pending["file"]).write_bytes(b"interrupted")
    deck.recover_recordings()
    assert "could not be recovered" in deck.error
    assert path.exists() and (deck.root / pending["file"]).exists()


def test_interrupted_clip_recovery(deck, client):
    pending = deck._new("video")
    pending.update(file=pending["id"] + ".mp4", width=320, height=240)
    path = deck.root / (pending["id"] + ".pending.json")
    path.write_text(json.dumps(pending))
    video = deck.root / pending["file"]
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=green:s=320x240:r=15",
            "-t",
            "2",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-g",
            "15",
            "-movflags",
            "frag_keyframe+empty_moov+default_base_moof",
            str(video),
        ],
        check=True,
    )
    data = video.read_bytes()
    last_fragment = data.rfind(b"moof") - 4
    assert last_fragment > data.find(b"moof")
    video.write_bytes(data[:last_fragment])
    deck.recover_recordings()
    assert not path.exists()
    item = client.get("/api/media").json["items"][0]
    assert item["recovered"] and 0.9 <= item["duration"] < 2
    assert (item["width"], item["height"]) == (320, 240)


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


def test_storage_guard(deck, monkeypatch):
    monkeypatch.setattr("camera.shutil.disk_usage", lambda _: Mock(free=1024))
    with pytest.raises(ValueError, match="256 MB"):
        deck._new("still")


def test_page_pagination_validation_and_clamping(deck, client):
    for index in range(41):
        make_media(deck, ident=f"20260916_1200{index // 10}{index % 10}_{index:08x}")
    first = client.get("/api/media?page=1&per_page=40").json
    second = client.get("/api/media?page=2&per_page=40").json
    assert (first["total"], first["pages"], len(first["items"])) == (41, 2, 40)
    assert (second["page"], len(second["items"])) == (2, 1)
    assert client.get("/api/media?page=99").json["page"] == 2
    assert client.get("/api/media?page=0").status_code == 400
    assert client.get("/api/media?kind=unknown").status_code == 400


def test_bulk_download_and_delete(deck, client):
    import zipfile

    first, second = make_media(deck, b"first"), make_media(deck, b"second")
    response = client.post(
        "/api/media/download", json={"ids": [first["id"], second["id"]]}
    )
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        assert archive.read(f'{first["id"]}/{first["file"]}') == b"first"
        assert archive.read(f'{second["id"]}/{second["id"]}.json')
    deleted = client.post(
        "/api/media/delete", json={"ids": [first["id"], second["id"], first["id"]]}
    ).json
    assert deleted["deleted"] == [first["id"], second["id"]]
    for item in (first, second):
        assert not list(deck.root.glob(item["id"] + ".*"))
    assert (
        client.post("/api/media/delete", json={"ids": ["../../etc"]}).status_code == 400
    )
    assert (
        client.post(
            "/api/media/delete", json={"ids": ["20260907_100000_12345678"] * 101}
        ).status_code
        == 400
    )


def test_upload_route_settings_and_validation(deck, app_factory):
    media = make_media(deck)
    uploader = Mock()
    uploader.upload.return_value = {
        "status": "uploaded",
        "key": "sha256/ab/hash.jpg",
        "error": None,
    }
    client = app_factory(
        deck, uploader=uploader, s3_endpoint="http://192.168.1.10:3900"
    ).test_client()
    assert client.get("/api/settings").json == {
        "s3_endpoint": "http://192.168.1.10:3900"
    }
    response = client.post(
        "/api/media/upload",
        json={
            "ids": [media["id"]],
            "endpoint": "https://objects.example.org",
            "bucket": "camera-archive",
            "prefix": "trip/one",
        },
    )
    assert response.json["results"][0]["status"] == "uploaded"
    uploader.upload.assert_called_once()
    assert (
        client.post(
            "/api/media/upload",
            json={
                "ids": [media["id"]],
                "endpoint": "http://example.org",
                "bucket": "ok-bucket",
            },
        ).status_code
        == 400
    )


def test_power_actions_are_fixed_confirmed_and_block_sequence(deck, app_factory):
    manager = Mock()
    client = app_factory(deck, power_manager=manager).test_client()
    assert (
        client.post(
            "/api/system/power", json={"action": "shutdown", "confirm": "wrong"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/system/power", json={"action": "reboot", "confirm": "ReBoOt"}
        ).status_code
        == 202
    )
    manager.schedule.assert_called_once_with("reboot")
    deck.sequence = {"active": True}
    assert (
        client.post(
            "/api/system/power", json={"action": "shutdown", "confirm": "shutdown"}
        ).status_code
        == 409
    )


class FakeS3:
    def __init__(self, fail_put=False):
        self.objects = {}
        self.puts = 0
        self.fail_put = fail_put

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        body, metadata = self.objects[(Bucket, Key)]
        return {"ContentLength": len(body), "Metadata": metadata, "ETag": "etag"}

    def put_object(self, Bucket, Key, Body, ContentLength, Metadata, IfNoneMatch):
        self.puts += 1
        if self.fail_put:
            raise TimeoutError("response lost")
        assert IfNoneMatch == "*"
        self.objects[(Bucket, Key)] = (Body.read(), Metadata)


def test_s3_uploader_deduplicates_copies_and_persists(deck):
    first, second = make_media(deck, b"identical"), make_media(deck, b"identical")
    s3 = FakeS3()
    uploader = S3Uploader(deck.root, client_factory=lambda _: s3)
    results = [
        uploader.upload(
            deck.root / item["file"],
            item["id"],
            endpoint_url="https://example.org",
            bucket="archive",
        )
        for item in (first, second)
    ]
    restarted = S3Uploader(deck.root, client_factory=lambda _: s3).upload(
        deck.root / second["file"],
        second["id"],
        endpoint_url="https://example.org",
        bucket="archive",
    )
    assert results[0].status == "uploaded"
    assert results[1].status == restarted.status == "deduplicated"
    assert s3.puts == 1


def test_s3_ambiguous_attempt_is_never_sent_twice(deck):
    media, s3 = make_media(deck, b"ambiguous"), FakeS3(fail_put=True)
    results = [
        S3Uploader(deck.root, client_factory=lambda _: s3).upload(
            deck.root / media["file"],
            media["id"],
            endpoint_url="https://example.org",
            bucket="archive",
        )
        for _ in range(2)
    ]
    assert all(result.status == "uncertain" for result in results)
    assert s3.puts == 1


def test_s3_overwritten_file_gets_a_new_content_key(deck):
    media, s3 = make_media(deck, b"first version"), FakeS3()
    uploader = S3Uploader(deck.root, client_factory=lambda _: s3)
    first = uploader.upload(
        deck.root / media["file"],
        media["id"],
        endpoint_url="https://example.org",
        bucket="archive",
    )
    (deck.root / media["file"]).write_bytes(b"second version")
    second = uploader.upload(
        deck.root / media["file"],
        media["id"],
        endpoint_url="https://example.org",
        bucket="archive",
    )
    assert first.status == second.status == "uploaded"
    assert first.key != second.key and s3.puts == 2


@pytest.mark.parametrize("saved", [None, "", "bad", "90", "180", "0"])
def test_saved_orientation_defaults_and_validation(tmp_path, saved):
    if saved is not None:
        (tmp_path / ".rotation").write_text(saved)
    assert CameraDeck(tmp_path).status()["rotation"] == (180 if saved == "180" else 0)


def test_capture_ids_list_in_capture_order_within_a_second(deck):
    start = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)
    ids = [
        deck._new("still", start + timedelta(milliseconds=ms))["id"]
        for ms in (999, 250, 5)
    ]
    assert all(ID.fullmatch(ident) for ident in ids)
    assert sorted(ids) == ids[::-1]


@pytest.fixture
def camera_stack(monkeypatch):
    import enum

    exposure = enum.IntEnum("AeExposureModeEnum", {"Normal": 0, "Short": 1, "Long": 2})
    camera = Mock()
    camera.sensor_resolution = (1920, 1080)
    camera.sensor_modes = [{"size": (1920, 1080), "fps": 60}]
    camera.camera_properties = {"Model": "test sensor"}
    camera.camera_controls = {
        "AeEnable": (False, True, True),
        "AwbEnable": (False, True, True),
        "AeExposureMode": (0, 2, 0),
    }
    camera.camera_ctrl_info = {
        name: (
            SimpleNamespace(
                type="Integer32" if name == "AeExposureMode" else "Bool", size=0
            ),
        )
        for name in camera.camera_controls
    }
    picamera = Mock(return_value=camera)
    picamera.global_camera_info.return_value = [{"Num": 0, "Model": "test sensor"}]
    monkeypatch.setitem(
        sys.modules,
        "libcamera",
        SimpleNamespace(
            Transform=lambda **kwargs: SimpleNamespace(**kwargs),
            ColorSpace=SimpleNamespace(Sycc=lambda: "sycc"),
            controls=SimpleNamespace(AeExposureModeEnum=exposure),
        ),
    )
    monkeypatch.setitem(sys.modules, "picamera2", SimpleNamespace(Picamera2=picamera))
    monkeypatch.setitem(
        sys.modules, "picamera2.encoders", SimpleNamespace(JpegEncoder=Mock())
    )
    monkeypatch.setitem(
        sys.modules, "picamera2.outputs", SimpleNamespace(FileOutput=Mock())
    )
    return camera


def test_truck_defaults_applied_on_camera_open(deck, camera_stack):
    deck.open(deck.index, deck.profile, deck.fps)
    config = camera_stack.create_video_configuration.call_args.kwargs
    assert config["main"] == {"size": (1920, 1080), "format": "YUV420"}
    assert config["controls"] == {"FrameRate": deck.fps}
    camera_stack.set_controls.assert_called_once_with(
        {"AeEnable": True, "AwbEnable": True, "AeExposureMode": 1}
    )
    assert deck.applied == {"AeEnable": True, "AwbEnable": True, "AeExposureMode": 1}
    assert deck.sequence_settings["burst"]["rate"] == 5


def test_orientation_persists_across_sessions_and_restart(
    deck, client, camera_stack, app_factory
):
    assert client.post("/api/configure", json={"rotation": 180}).json["rotation"] == 180
    fresh_client = app_factory(deck).test_client()
    assert fresh_client.get("/api/status").json["rotation"] == 180
    assert (
        fresh_client.post("/api/controls", json={"AeEnable": False}).status_code == 200
    )
    assert (
        fresh_client.post("/api/configure", json={"profile": "720p"}).json["rotation"]
        == 180
    )
    assert fresh_client.get("/api/status").json["applied"]["AeEnable"] is False
    restarted = CameraDeck(deck.root)
    restarted.open(0, "full sensor", 5)
    try:
        assert restarted.status()["rotation"] == 180
        transform = camera_stack.create_video_configuration.call_args.kwargs[
            "transform"
        ]
        assert transform.hflip and transform.vflip
        camera_stack.start.side_effect = RuntimeError("Camera failed to start")
        with pytest.raises(RuntimeError, match="Camera failed to start"):
            restarted.open(0, "full sensor", 5, 0)
        assert CameraDeck(deck.root).status()["rotation"] == 180
        camera_stack.start.side_effect = None
        restarted.open(0, "full sensor", 5, 0)
        assert CameraDeck(deck.root).status()["rotation"] == 0
        transform = camera_stack.create_video_configuration.call_args.kwargs[
            "transform"
        ]
        assert not transform.hflip and not transform.vflip
    finally:
        restarted.close()


@pytest.mark.parametrize(
    "short_name,maximum", [("Short", 2), ("ExposureShort", 2), ("Short", 0), (None, 2)]
)
def test_motion_defaults_modern_modes_and_unsupported_short(deck, short_name, maximum):
    deck.camera = Mock()
    deck.schema = {
        "ExposureTimeMode": spec("Integer32", 0, 1, options={"Auto": 0, "Manual": 1}),
        "AnalogueGainMode": spec("Integer32", 0, 1, options={"Auto": 0, "Manual": 1}),
    }
    if short_name:
        deck.schema["AeExposureMode"] = spec(
            "Integer32", 0, maximum, options={"Normal": 0, short_name: 1}
        )
    deck._apply_motion_defaults()
    expected = {"ExposureTimeMode": 0, "AnalogueGainMode": 0}
    if short_name and maximum >= 1:
        expected["AeExposureMode"] = 1
    deck.camera.set_controls.assert_called_once_with(expected)
    assert "ExposureTime" not in deck.applied and "AnalogueGain" not in deck.applied


def test_yuv420_planes_skip_stride_padding():
    y = np.arange(48, dtype=np.uint8).reshape(6, 8)
    u = np.arange(100, 112, dtype=np.uint8).reshape(3, 4)
    v = np.arange(200, 212, dtype=np.uint8).reshape(3, 4)
    padded = []
    for plane in (y, u, v):
        buffer = np.full((plane.shape[0], plane.shape[1] * 2), 7, dtype=np.uint8)
        buffer[:, : plane.shape[1]] = plane
        padded.append(buffer.ravel())
    actual = yuv420_planes(np.concatenate(padded).reshape(9, 16), 8, 6)
    for result, expected in zip(actual, (y, u, v)):
        assert np.array_equal(result, expected)


class SyntheticRequest:
    def __init__(self):
        self.references = 0

    def acquire(self):
        self.references += 1

    def release(self):
        self.references -= 1


def test_burst_sampler_keeps_average_rate_then_skips_and_counts():
    period = 1e9 / 15.75
    sampler = BurstSampler(4)
    taken = []
    for index in range(158):
        sampler.offer(SyntheticRequest(), {"SensorTimestamp": round(index * period)})
        if not sampler.mailbox.empty():
            request, _, number, _ = sampler.mailbox.get_nowait()
            request.release()
            taken.append(index)
            assert number == len(taken)
    assert len(taken) == 40
    assert {b - a for a, b in zip(taken, taken[1:])} <= {3, 4}
    busy = BurstSampler(30, count=2)
    first, second, third, late = (SyntheticRequest() for _ in range(4))
    busy.offer(first, {"SensorTimestamp": 0})
    busy.offer(second, {"SensorTimestamp": 10**9})
    assert busy.skipped == 1 and first.references == 1 and second.references == 0
    busy.mailbox.get_nowait()[0].release()
    busy.offer(SyntheticRequest(), {"SensorTimestamp": "invalid"})
    busy.offer(third, {"SensorTimestamp": 2 * 10**9})
    assert busy.done and busy.selected == 2
    busy.offer(late, {"SensorTimestamp": 3 * 10**9})
    assert late.references == 0
    busy.close()
    busy.drain()
    assert third.references == 0


@pytest.fixture
def burst_deck(deck):
    deck.camera = Mock()
    deck.camera.sensor_resolution = (80, 60)
    deck.camera.stream_configuration.return_value = {"size": (80, 60)}
    deck.modes = [{"size": [40, 30], "fps": 60}, {"size": [80, 60], "fps": 15.75}]
    deck.fps = 15.75
    deck.schema = {
        "AeEnable": {**spec("Bool"), "default": True},
        "ExposureTime": {**spec("Integer32", 100, 10000), "default": 1000},
        "AnalogueGain": {**spec("Float", 1, 8), "default": 1},
    }
    yield deck
    deck.stop_sequence()
    if deck.sequence_worker:
        deck.sequence_worker.join(5)
        assert not deck.sequence_worker.is_alive()


def feed_frames(deck):
    requests = []
    deadline = time.monotonic() + 5
    while deck.burst is None and deck.sequence["active"]:
        assert time.monotonic() < deadline
        time.sleep(0.001)
    index = 0
    while deck.sequence["active"]:
        assert time.monotonic() < deadline
        burst = deck.burst
        if burst is None or not burst.mailbox.empty():
            time.sleep(0.001)
            continue
        request = SyntheticRequest()
        request.get_metadata = lambda i=index: {
            "SensorTimestamp": round(i * 1e9 / 15.75),
            "ExposureTime": 1000,
            "AnalogueGain": 4.0,
        }
        request.make_array = lambda name: np.full((90, 96), 128, dtype=np.uint8)
        requests.append(request)
        deck._metadata(request)
        index += 1
    deck.sequence_worker.join(5)
    assert not deck.sequence_worker.is_alive()
    assert all(request.references == 0 for request in requests)
    return requests


@pytest.mark.parametrize("rotation", [0, 180])
def test_burst_streams_full_resolution_photos_and_restores(burst_deck, rotation):
    deck = burst_deck
    deck.rotation = rotation
    deck.applied = {"ExposureTime": 1000, "AeEnable": False}
    deck.start_sequence(
        "burst",
        {
            "rate": 4,
            "count": 3,
            "quality": 70,
            "threads": 2,
            "controls": {"AnalogueGain": 4},
        },
    )
    feed_frames(deck)
    run = deck.sequence
    assert run["error"] is None and not run["active"]
    assert run["completed"] == 3 and run["skipped"] == 0
    assert run["rate"] == round(2e9 / round(8 * 1e9 / 15.75), 2)
    items = sorted(
        (json.loads(path.read_text()) for path in deck.root.glob("*.json")),
        key=lambda item: item["sequence"]["number"],
    )
    assert [item["sequence"]["number"] for item in items] == [1, 2, 3]
    assert [item["metadata"]["SensorTimestamp"] for item in items] == [
        round(n * 1e9 / 15.75) for n in (0, 4, 8)
    ]
    for item in items:
        assert item["capture_mode"] == "full sensor" and item["rotation"] == rotation
        assert (item["width"], item["height"], item["fps"]) == (80, 60, 15.75)
        with Image.open(deck.root / item["file"]) as image:
            assert image.size == (80, 60)
        assert (deck.root / (item["id"] + ".thumb.jpg")).exists()
        assert (deck.root / (item["id"] + ".preview.jpg")).exists()
    assert deck.applied == {"ExposureTime": 1000, "AeEnable": False}
    deck.camera.create_video_configuration.assert_not_called()
    deck.camera.stop.assert_not_called()


def test_continuous_photos_accept_daily_interval(burst_deck):
    deck = burst_deck
    deck.start_sequence("burst", {"rate": 1 / 86400, "count": 1})
    feed_frames(deck)
    assert deck.sequence["error"] is None and deck.sequence["completed"] == 1
    items = [json.loads(path.read_text()) for path in deck.root.glob("*.json")]
    assert len(items) == 1
    assert items[0]["sequence"]["settings"]["rate"] == 1 / 86400


def test_burst_failure_stops_releases_frames_and_restores(burst_deck):
    deck = burst_deck
    deck.applied = {"AeEnable": True}
    deck._space = Mock(side_effect=[None, ValueError("256 MB")])
    deck.start_sequence("burst", {"threads": 1})
    assert feed_frames(deck)
    assert not deck.sequence["active"] and "256 MB" in deck.sequence["error"]
    assert deck.sequence["completed"] == 0
    assert not list(deck.root.glob("*.json"))
    assert deck.applied == {"AeEnable": True}
