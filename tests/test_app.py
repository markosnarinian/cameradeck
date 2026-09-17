import json
import io
import subprocess
import zipfile
from unittest.mock import Mock

import numpy as np
import pytest
from botocore.exceptions import ClientError
from PIL import Image, ImageFilter

from app import create_app
from camera import CameraDeck, focus_grid, validate_controls
from storage import S3Uploader


@pytest.fixture
def deck(tmp_path):
    return CameraDeck(tmp_path)


@pytest.fixture
def client(deck):
    return create_app(deck).test_client()


def make_media(deck, content=b"photo", ident=None):
    item = deck._new("still")
    if ident:
        item["id"] = ident
    item.update(file=item["id"] + ".jpg", bytes=len(content), width=1, height=1)
    (deck.root / item["file"]).write_bytes(content)
    (deck.root / (item["id"] + ".thumb.jpg")).write_bytes(b"thumb")
    (deck.root / (item["id"] + ".preview.jpg")).write_bytes(b"preview")
    (deck.root / (item["id"] + ".json")).write_text(json.dumps(item))
    return item


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
    assert client.get("/api/media?page=2").json["page"] == 1
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
    (deck.root / "20260907_100000_12345678.json").write_text("{")
    assert client.get("/api/media").json["items"] == []


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
            str(deck.root / pending["file"]),
        ],
        check=True,
    )
    # Simulate losing the last fragment and final index, not a graceful close.
    video = deck.root / pending["file"]
    data = video.read_bytes()
    last_fragment = data.rfind(b"moof") - 4
    assert last_fragment > data.find(b"moof")
    video.write_bytes(data[:last_fragment])
    deck.recover_recordings()
    assert not path.exists()
    item = client.get("/api/media").json["items"][0]
    assert item["recovered"] and 0.9 <= item["duration"] < 2
    assert (item["width"], item["height"]) == (320, 240)


def test_still_failure_restores_preview(deck):
    deck.camera = Mock()
    deck.camera.camera_configuration.return_value = {"transform": "normal"}
    deck.camera.switch_mode_and_capture_file.side_effect = OSError("disk write failed")
    deck.preview = Mock()
    with pytest.raises(OSError, match="disk write failed"):
        deck.capture()
    deck.camera.start.assert_called_once()
    deck.camera.start_encoder.assert_called_once_with(deck.preview, name="lores")


def test_request_validation_and_host(client):
    assert (
        client.get("/api/status", headers={"Host": "evil.example"}).status_code == 403
    )
    assert client.post("/api/focus", json={"enabled": "false"}).status_code == 400
    assert client.post("/api/configure", json=[]).status_code == 400
    assert client.post("/api/controls", data="Brightness=1").status_code == 400


def test_stream_limit_releases_slots(deck, client):
    deck.frames.write(b"test-jpeg")
    streams = [client.get("/stream.mjpg", buffered=False) for _ in range(4)]
    assert all(r.status_code == 200 for r in streams)
    assert client.get("/stream.mjpg").status_code == 503
    streams.pop().close()
    replacement = client.get("/stream.mjpg", buffered=False)
    assert replacement.status_code == 200
    replacement.close()
    for r in streams:
        r.close()


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
    first = make_media(deck, b"first")
    second = make_media(deck, b"second")
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
    assert not list(deck.root.glob("*.json"))
    assert (
        client.post("/api/media/delete", json={"ids": ["../../etc"]}).status_code == 400
    )


def test_upload_route_settings_and_validation(deck):
    media = make_media(deck)
    uploader = Mock()
    uploader.upload.return_value = {
        "status": "uploaded",
        "key": "sha256/ab/hash.jpg",
        "error": None,
    }
    client = create_app(
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


def test_power_actions_are_fixed_confirmed_and_block_recording(deck):
    manager = Mock()
    client = create_app(deck, power_manager=manager).test_client()
    assert (
        client.post(
            "/api/system/power", json={"action": "shutdown", "confirm": "wrong"}
        ).status_code
        == 400
    )
    response = client.post(
        "/api/system/power", json={"action": "reboot", "confirm": "ReBoOt"}
    )
    assert response.status_code == 202
    manager.schedule.assert_called_once_with("reboot")
    deck.recording = {"item": {"id": "ongoing"}}
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
    first = make_media(deck, b"identical")
    second = make_media(deck, b"identical")
    s3 = FakeS3()
    uploader = S3Uploader(deck.root, client_factory=lambda _: s3)
    first_result = uploader.upload(
        deck.root / first["file"],
        first["id"],
        endpoint_url="https://example.org",
        bucket="archive",
    )
    second_result = uploader.upload(
        deck.root / second["file"],
        second["id"],
        endpoint_url="https://example.org",
        bucket="archive",
    )
    restarted = S3Uploader(deck.root, client_factory=lambda _: s3).upload(
        deck.root / second["file"],
        second["id"],
        endpoint_url="https://example.org",
        bucket="archive",
    )
    assert first_result.status == "uploaded"
    assert second_result.status == restarted.status == "deduplicated"
    assert s3.puts == 1


def test_s3_ambiguous_attempt_is_never_sent_twice(deck):
    media = make_media(deck, b"ambiguous")
    s3 = FakeS3(fail_put=True)
    uploader = S3Uploader(deck.root, client_factory=lambda _: s3)
    first = uploader.upload(
        deck.root / media["file"],
        media["id"],
        endpoint_url="https://example.org",
        bucket="archive",
    )
    second = S3Uploader(deck.root, client_factory=lambda _: s3).upload(
        deck.root / media["file"],
        media["id"],
        endpoint_url="https://example.org",
        bucket="archive",
    )
    assert first.status == second.status == "uncertain"
    assert s3.puts == 1


def test_s3_overwritten_file_gets_a_new_content_key(deck):
    media = make_media(deck, b"first version")
    s3 = FakeS3()
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
    assert first.key != second.key
    assert s3.puts == 2


@pytest.fixture
def sequence_deck(deck):
    deck.schema = {
        "ExposureTime": spec("Integer32", 100, 10000),
        "AnalogueGain": spec("Float", 1, 8),
        "AeEnable": spec("Bool"),
    }
    deck.camera = Mock()
    deck.camera.camera_configuration.return_value = {"transform": None}
    deck.camera.sensor_resolution = (80, 60)

    def photo(config, path, delay):
        Image.new("RGB", (80, 60), "gray").save(path)
        return {
            "ExposureTime": deck.applied.get("ExposureTime", 6000),
            "AnalogueGain": deck.applied.get("AnalogueGain", 1),
        }

    deck.camera.switch_mode_and_capture_file.side_effect = photo
    deck.open = Mock(side_effect=lambda *args: deck.applied.clear())
    yield deck
    deck.stop_sequence()
    if deck.sequence_worker:
        deck.sequence_worker.join(5)
        assert not deck.sequence_worker.is_alive()


def test_periodic_stills_publish_limit_and_restore(sequence_deck):
    deck = sequence_deck
    deck.applied = {"ExposureTime": 7000, "AeEnable": False}
    deck.start_sequence(
        "stills", {"interval": 1, "count": 2, "controls": {"ExposureTime": 1300}}
    )
    deck.sequence_worker.join(4)
    assert not deck.sequence["active"]
    assert deck.sequence["completed"] == 2
    assert deck.sequence["error"] is None
    items = [json.loads(p.read_text()) for p in deck.root.glob("*.json")]
    assert len(items) == 2
    assert {i["sequence"]["number"] for i in items} == {1, 2}
    assert all(i["metadata"]["ExposureTime"] == 1300 for i in items)
    assert all(i["capture_mode"] == "full sensor" for i in items)
    assert deck.applied == {"ExposureTime": 7000, "AeEnable": False}
    deck.open.assert_called_once_with(0, "1080p", 30, 0)
    assert deck.sequence_settings["test"]["samples"] == 3


@pytest.mark.parametrize("modern", [False, True])
def test_test_sweep_all_pairs_metadata_and_restore(sequence_deck, modern):
    deck = sequence_deck
    if modern:
        del deck.schema["AeEnable"]
        for name in ("ExposureTimeMode", "AnalogueGainMode"):
            deck.schema[name] = spec(
                "Integer32", 0, 1, options={"Auto": 0, "Manual": 1}
            )
    deck.start_sequence(
        "test",
        {"shutters": [700, 2300], "gains": [1.5, 3], "settle": 0.5, "samples": 2},
    )
    deck.sequence_worker.join(7)
    assert deck.sequence["error"] is None
    assert not deck.sequence["active"]
    results = deck.sequence["results"]
    assert [
        (r["metadata"]["ExposureTime"], r["metadata"]["AnalogueGain"]) for r in results
    ] == [
        (700, 1.5),
        (700, 1.5),
        (700, 3),
        (700, 3),
        (2300, 1.5),
        (2300, 1.5),
        (2300, 3),
        (2300, 3),
    ]
    if modern:
        assert all(
            r["settings"]["ExposureTimeMode"] == r["settings"]["AnalogueGainMode"] == 1
            for r in results
        )
    else:
        assert all(r["settings"]["AeEnable"] is False for r in results)
    assert deck.applied == {}


def test_sequence_stop_interrupts_settle_and_blocks_conflicts(sequence_deck):
    deck = sequence_deck
    client = create_app(deck, power_manager=Mock()).test_client()
    assert (
        client.post(
            "/api/sequence/start", json={"mode": "test", "settings": {"settle": 30}}
        ).status_code
        == 200
    )
    for path, data in [
        ("/api/sequence/start", {"mode": "stills"}),
        ("/api/capture", {}),
        ("/api/record/start", {}),
        ("/api/controls", {"ExposureTime": 1500}),
    ]:
        assert client.post(path, json=data).status_code == 400
    # Use the real guard; open itself is mocked only to avoid importing Pi hardware.
    with pytest.raises(ValueError, match="Stop the capture sequence"):
        CameraDeck.open(deck, 0, "720p", 15)
    assert (
        client.post(
            "/api/system/power", json={"action": "reboot", "confirm": "reboot"}
        ).status_code
        == 409
    )
    assert client.post("/api/sequence/stop", json={}).status_code == 200
    deck.sequence_worker.join(2)
    assert not deck.sequence["active"]
    assert deck.sequence["completed"] == 0
    assert deck.sequence["error"] is None


@pytest.mark.parametrize(
    "mode,settings",
    [
        ([], {}),
        ("invalid", {}),
        ("stills", {"interval": 0}),
        ("stills", {"count": True}),
        ("stills", {"count": 1.5}),
        ("stills", {"interval": float("nan")}),
        ("stills", {"controls": []}),
        ("stills", {"controls": {"Unknown": 1}}),
        ("test", {"shutters": [99]}),
        ("test", {"gains": [9]}),
        ("test", {"shutters": [500.5]}),
        ("test", {"gains": []}),
        ("test", {"samples": 6}),
        ("test", {"settle": 0}),
        ("test", {"gains": [True]}),
    ],
)
def test_sequence_rejects_invalid_before_camera_changes(sequence_deck, mode, settings):
    deck = sequence_deck
    response = (
        create_app(deck)
        .test_client()
        .post("/api/sequence/start", json={"mode": mode, "settings": settings})
    )
    assert response.status_code == 400
    assert deck.sequence is None
    deck.camera.set_controls.assert_not_called()


@pytest.mark.parametrize("failure", ["capture", "storage"])
def test_sequence_failure_stops_and_restores(sequence_deck, failure):
    deck = sequence_deck
    deck.applied = {"AeEnable": True}
    if failure == "capture":
        deck.camera.switch_mode_and_capture_file.side_effect = OSError("write failed")
    else:
        deck._space = Mock(side_effect=[None, ValueError("256 MB")])
    deck.start_sequence("stills", {"count": 3})
    deck.sequence_worker.join(2)
    assert not deck.sequence["active"]
    assert deck.sequence["completed"] == 0
    assert ("write failed" if failure == "capture" else "256 MB") in deck.sequence[
        "error"
    ]
    assert deck.applied == {"AeEnable": True}
    assert not list(deck.root.glob("*.json"))


@pytest.mark.parametrize("capture_time,delay", [(0.25, 1.75), (3.5, 0)])
def test_periodic_cadence_and_stop_during_interval(
    sequence_deck, monkeypatch, capture_time, delay
):
    deck = sequence_deck
    monkeypatch.setattr(
        "camera.time.monotonic", Mock(side_effect=[10, 10 + capture_time])
    )
    # End an unlimited run at its first interval, without sleeping in the test.
    deck.sequence_stop.wait = Mock(return_value=True)
    deck.start_sequence("stills", {"interval": 2, "count": 0})
    deck.sequence_worker.join(2)
    assert deck.sequence["error"] is None
    assert deck.sequence["completed"] == 1
    deck.sequence_stop.wait.assert_called_once_with(delay)
    assert not deck.sequence["active"]


def test_recording_blocks_sequence_and_close_stops_worker(sequence_deck):
    deck = sequence_deck
    deck.recording = {"item": {"id": "ongoing"}}
    with pytest.raises(ValueError, match="Stop recording"):
        deck.start_sequence("stills", {})
    deck.recording = None
    deck.start_sequence("test", {"settle": 30})
    deck.close()
    assert not deck.sequence_worker.is_alive()
    assert not deck.sequence["active"]
    assert deck.sequence["completed"] == 0
    assert deck.camera is None


@pytest.mark.parametrize("ceiling,expected", [(15.75, 15.75), (60, 30)])
def test_truck_defaults_applied_on_camera_open(deck, monkeypatch, ceiling, expected):
    import enum
    import sys
    from types import SimpleNamespace

    exposure = enum.IntEnum("AeExposureModeEnum", {"Normal": 0, "Short": 1, "Long": 2})
    camera = Mock()
    camera.sensor_modes = [{"size": (1920, 1080), "fps": ceiling}]
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
            Transform=Mock(), controls=SimpleNamespace(AeExposureModeEnum=exposure)
        ),
    )
    monkeypatch.setitem(sys.modules, "picamera2", SimpleNamespace(Picamera2=picamera))
    monkeypatch.setitem(
        sys.modules, "picamera2.encoders", SimpleNamespace(JpegEncoder=Mock())
    )
    monkeypatch.setitem(
        sys.modules, "picamera2.outputs", SimpleNamespace(FileOutput=Mock())
    )

    deck.open(deck.index, deck.profile, deck.fps)
    config = camera.create_video_configuration.call_args.kwargs
    assert config["main"]["size"] == (1920, 1080)
    assert config["controls"] == {"FrameRate": expected}
    assert deck.fps == expected
    camera.set_controls.assert_called_once_with(
        {"AeEnable": True, "AwbEnable": True, "AeExposureMode": 1}
    )
    assert deck.applied == {"AeEnable": True, "AwbEnable": True, "AeExposureMode": 1}
    assert deck.sequence_settings["stills"] == {
        "interval": 1,
        "count": 0,
        "controls": {},
    }
    assert deck.sequence_settings["test"] == {
        "shutters": [1000, 2000, 4000, 8000],
        "gains": [2, 4, 8],
        "settle": 2,
        "samples": 3,
    }


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
