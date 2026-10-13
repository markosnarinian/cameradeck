"""Photo contracts using synthetic requests only; never instantiate real hardware."""

import json
import queue
import sys
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from camera import BurstSampler, CameraDeck, Frames, yuv420_planes


def test_stride_padded_yuv_planes():
    array = np.arange(6 * 8, dtype=np.uint8).reshape(6, 8)
    y, u, v = yuv420_planes(array, 4, 4)
    assert y.shape == (4, 4) and u.shape == v.shape == (2, 2)
    assert u.tolist() == [[32, 33], [36, 37]]
    assert v.tolist() == [[40, 41], [44, 45]]


class Request:
    def __init__(self, timestamp=1_000_000_000):
        self.metadata = {"SensorTimestamp": timestamp, "ExposureTime": 1000}
        self.array = np.full((48, 64), 128, dtype=np.uint8)
        self.refs = 1

    def acquire(self):
        self.refs += 1

    def release(self):
        self.refs -= 1

    def get_metadata(self):
        return self.metadata

    def make_array(self, name):
        assert name == "main"
        return self.array


class Pipeline:
    sensor_resolution = (64, 32)
    sensor_modes = [{"size": (64, 32), "fps": 10}]
    camera_properties = {"Model": "synthetic"}
    camera_controls = {}
    camera_ctrl_info = {}

    def __init__(self, index=0):
        self.calls = []
        self.controls = {}
        self.request = Request()

    @staticmethod
    def global_camera_info():
        return [{"Num": 0}]

    def create_video_configuration(self, **values):
        return values

    def configure(self, config):
        self.config = config
        self.calls.append("configure")

    def start(self):
        self.calls.append("start")

    def start_encoder(self, encoder, name):
        assert name == "lores"
        self.calls.append("preview")

    def stop_encoder(self):
        self.calls.append("stop_encoder")

    def stop(self):
        self.calls.append("stop")

    def close(self):
        self.calls.append("close")

    def set_controls(self, values):
        self.calls.append(dict(values))
        self.controls.update(values)

    def stream_configuration(self, name):
        return {"size": self.sensor_resolution}

    def capture_request(self):
        return self.request


@pytest.fixture
def synthetic_modules(monkeypatch):
    monkeypatch.setitem(sys.modules, "picamera2", SimpleNamespace(Picamera2=Pipeline))
    monkeypatch.setitem(
        sys.modules,
        "picamera2.encoders",
        SimpleNamespace(JpegEncoder=lambda **kw: SimpleNamespace()),
    )
    monkeypatch.setitem(
        sys.modules,
        "picamera2.outputs",
        SimpleNamespace(FileOutput=lambda output: output),
    )
    monkeypatch.setitem(
        sys.modules,
        "libcamera",
        SimpleNamespace(
            ColorSpace=SimpleNamespace(Sycc=lambda: "sYCC"),
            Transform=lambda **kw: kw,
            controls=SimpleNamespace(),
        ),
    )


def test_continuous_full_sensor_capture_and_publication(tmp_path, synthetic_modules):
    published = []

    def publish(item):
        assert json.loads((tmp_path / (item["id"] + ".json")).read_text()) == item
        assert not (tmp_path / (item["id"] + ".json.tmp")).exists()
        published.append(item)

    deck = CameraDeck(tmp_path, media_published=publish)
    deck.frames.write(b"stale")
    deck.open(0, "1080p", 5, 180)
    p = deck.camera
    assert p.config["main"] == {"size": (64, 32), "format": "YUV420"}
    assert p.config["lores"]["size"] == (640, 360)
    assert p.config["colour_space"] == "sYCC"
    assert p.config["transform"] == {"hflip": True, "vflip": True}
    assert deck.frames.frame is None
    before = list(p.calls)
    item = deck.capture()
    assert p.calls == before  # no stop, mode switch, or reopen
    assert p.request.refs == 0
    assert item["rotation"] == 180
    assert item["metadata"] == p.request.metadata
    assert "focus" not in item
    with Image.open(tmp_path / item["file"]) as image:
        assert image.size == (64, 32)
    assert published == [item]
    assert CameraDeck(tmp_path).rotation == 180
    deck.close()


def test_release_before_encoding_and_failure(tmp_path, monkeypatch):
    deck = CameraDeck(tmp_path)
    deck.camera = Pipeline()
    request = deck.camera.request

    def fail(*args):
        assert request.refs == 0
        assert not np.shares_memory(args[1], request.array)
        raise OSError("encode failed")

    monkeypatch.setattr(deck, "_encode_photo", fail)
    with pytest.raises(OSError, match="encode failed"):
        deck.capture()


def test_freshness_and_status(tmp_path):
    deck = CameraDeck(tmp_path)
    assert deck.status()["metadata_age"] is None
    deck._metadata(Request())
    deck.frames.write(b"jpeg")
    state = deck.status()
    assert state["metadata_age"] >= 0
    assert state["frame_age"] >= 0
    assert not {"recording", "elapsed", "focus", "focus_enabled"} & state.keys()
    assert state["sequence_settings"] == {
        "burst": {"rate": 5, "count": 0, "quality": 90, "threads": 3, "controls": {}}
    }
    deck.frames.reset()
    assert deck.frames.frame is None and deck.status()["frame_age"] is None


@pytest.mark.parametrize(
    "mode,settings",
    [
        ("road", {}),
        ("stills", {}),
        ("test", {}),
        ("burst", {"rate": 6}),
        ("burst", {"count": True}),
        ("burst", {"quality": 0}),
        ("burst", {"threads": 5}),
    ],
)
def test_sequence_validation(tmp_path, mode, settings):
    deck = CameraDeck(tmp_path)
    deck.camera = Pipeline()
    deck.fps = 5
    deck.modes = Pipeline.sensor_modes
    with pytest.raises(ValueError):
        deck.start_sequence(mode, settings)
    assert deck.sequence is None


def test_sampler_bounded_metadata_and_references():
    sampler = BurstSampler(5, 2)
    first, second = Request(), Request(1_200_000_000)
    sampler.offer(first, first.metadata)
    first.metadata["ExposureTime"] = 9000
    sampler.offer(second, second.metadata)
    assert sampler.skipped == 1 and second.refs == 1
    request, metadata, number, captured = sampler.mailbox.get_nowait()
    assert metadata["ExposureTime"] == 1000 and number == 1
    assert captured.tzinfo is not None
    request.release()
    sampler.offer(second, second.metadata)
    assert sampler.done
    sampler.close()
    sampler.drain()
    assert first.refs == second.refs == 1


def test_burst_reuses_stream_and_drains_selected(tmp_path):
    deck = CameraDeck(tmp_path)
    p = deck.camera = Pipeline()
    run = deck.start_sequence("burst", {"count": 2})
    deadline = time.monotonic() + 3
    while deck.burst is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert deck.burst is not None
    with pytest.raises(ValueError, match="Stop"):
        deck.capture()
    requests = [Request(), Request(1_200_000_000)]
    for request in requests:
        while deck.burst.mailbox.full() and time.monotonic() < deadline:
            time.sleep(0.01)
        deck._metadata(request)
    deck.sequence_worker.join(timeout=3)
    assert not deck.sequence_worker.is_alive()
    assert deck.sequence["completed"] == 2
    assert deck.sequence["error"] is None
    assert deck.sequence["rate"] == 5
    assert all(request.refs == 1 for request in requests)
    assert not p.calls
    assert len(list(tmp_path.glob("*.json"))) == 2
    deck.close()


def test_burst_saves_final_photo_queued_after_worker_timeout(tmp_path, monkeypatch):
    timed_out, resume, closed = threading.Event(), threading.Event(), threading.Event()

    class PausingQueue(queue.Queue):
        def get(self, block=True, timeout=None):
            try:
                return super().get(block, timeout)
            except queue.Empty:
                if block and not timed_out.is_set():
                    timed_out.set()
                    assert resume.wait(3)
                raise

    class Sampler(BurstSampler):
        def __init__(self, rate, count=0):
            super().__init__(rate, count)
            self.mailbox = PausingQueue(maxsize=1)

        def close(self):
            super().close()
            closed.set()

    monkeypatch.setattr("camera.BurstSampler", Sampler)
    published = []
    deck = CameraDeck(tmp_path, media_published=published.append)
    deck.camera = Pipeline()
    request = Request()
    try:
        deck.start_sequence("burst", {"count": 1, "threads": 1})
        assert timed_out.wait(3)
        deck._metadata(request)
        assert closed.wait(3)
        resume.set()
        deck.sequence_worker.join(timeout=3)
        assert not deck.sequence_worker.is_alive()
        assert deck.sequence["error"] is None
        assert deck.sequence["selected"] == deck.sequence["completed"] == 1
        assert len(published) == 1
        with Image.open(tmp_path / published[0]["file"]) as image:
            assert image.size == (64, 32)
        assert request.refs == 1
    finally:
        resume.set()
        deck.close()


def test_restore_auto_without_reopen(tmp_path, monkeypatch):
    deck = CameraDeck(tmp_path)
    deck.camera = Pipeline()
    deck.schema = {
        "AeEnable": dict(
            type="Bool", size=0, min=False, max=True, default=True, options={}
        ),
        "ExposureTime": dict(
            type="Integer32", size=0, min=1, max=100000, default=None, options={}
        ),
    }
    deck.set_controls({"AeEnable": True})
    monkeypatch.setattr(deck, "_run_burst", lambda: None)
    deck.start_sequence(
        "burst", {"controls": {"AeEnable": False, "ExposureTime": 2000}}
    )
    deck.sequence_worker.join(timeout=2)
    assert deck.applied == {"AeEnable": True}
    assert deck.camera.calls[-1]["AeEnable"] is True
    assert "configure" not in deck.camera.calls


@pytest.mark.parametrize("previous", [None, 20000])
def test_temporary_controls_require_a_valid_restore_value(tmp_path, previous):
    deck = CameraDeck(tmp_path)
    pipeline = deck.camera = Pipeline()
    deck.schema = {
        "AeFlickerMode": dict(
            type="Integer32",
            size=0,
            min=0,
            max=1,
            default=0,
            options={"FlickerOff": 0, "FlickerManual": 1},
        ),
        "AeFlickerPeriod": dict(
            type="Integer32", size=0, min=1, max=100000, default=None, options={}
        ),
    }
    deck.set_controls({"AeFlickerMode": 0})
    if previous is not None:
        deck.set_controls({"AeFlickerPeriod": previous})
    original = dict(pipeline.controls)
    settings = {
        "count": 1,
        "threads": 1,
        "controls": {"AeFlickerMode": 1, "AeFlickerPeriod": 10000},
    }
    try:
        if previous is None:
            with pytest.raises(ValueError, match="restore.*AeFlickerPeriod"):
                deck.start_sequence("burst", settings)
            assert deck.status()["sequence"] is None
        else:
            deck.start_sequence("burst", settings)
            deadline = time.monotonic() + 3
            while deck.burst is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert deck.burst is not None
            assert pipeline.controls["AeFlickerPeriod"] == 10000
            deck._metadata(Request())
            deck.sequence_worker.join(timeout=3)
            assert not deck.sequence_worker.is_alive()
            run = deck.status()["sequence"]
            assert run["completed"] == 1 and run["error"] is None
        assert deck.status()["applied"] == original
        assert pipeline.controls == original
    finally:
        deck.close()


def test_recovery_failure_preserves_originals(tmp_path, monkeypatch):
    deck = CameraDeck(tmp_path)
    pending = tmp_path / "old.pending.json"
    pending.write_text(json.dumps({"id": "old", "file": "old.mp4"}))
    original = tmp_path / "old.mp4"
    original.write_bytes(b"broken original")

    def fail(*args):
        raise OSError("invalid clip")

    monkeypatch.setitem(sys.modules, "av", SimpleNamespace(open=fail))
    deck.recover_recordings()
    assert pending.exists() and original.read_bytes() == b"broken original"
    assert "Original preserved" in deck.error
    deck.shutdown.set()
    deck.recover_recordings()
    assert pending.exists()


def test_startup_recovery_is_background(tmp_path, synthetic_modules, monkeypatch):
    deck = CameraDeck(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def recover():
        entered.set()
        release.wait(timeout=3)

    monkeypatch.setattr(deck, "recover_recordings", recover)
    try:
        deck.start()
        assert entered.wait(timeout=1)
        assert deck.status()["ready"]
        assert deck.recovery_worker.is_alive()
    finally:
        release.set()
        deck.close()
    assert not deck.recovery_worker.is_alive()
