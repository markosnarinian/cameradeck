"""Single-owner Picamera2 service. Camera operations are serialized, frames are shared."""

import io
import json
import math
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import numpy as np
from PIL import Image


def plain(value):
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def focus_grid(image):
    """3x3 variance-of-Laplacian on a normalized 480px luminance image."""
    image = image.convert("L")
    image.thumbnail((480, 360))
    a = np.asarray(image, dtype=np.float32)
    lap = a[1:-1, :-2] + a[1:-1, 2:] + a[:-2, 1:-1] + a[2:, 1:-1] - 4 * a[1:-1, 1:-1]
    return [
        round(float(cell.var()), 1)
        for row in np.array_split(lap, 3)
        for cell in np.array_split(row, 3, axis=1)
    ]


class Frames(io.BufferedIOBase):
    def __init__(self):
        super().__init__()
        self.condition = threading.Condition()
        self.frame = None
        self.sequence = 0
        self.updated = 0

    def write(self, data):
        with self.condition:
            self.frame = bytes(data)
            self.sequence += 1
            self.updated = time.monotonic()
            self.condition.notify_all()
        return len(data)


class CameraDeck:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.frames = Frames()
        self.camera = None
        self.recording = None
        self.metadata = {}
        self.focus = []
        self.focus_enabled = False
        self.error = None
        self.applied = {}
        self.shutdown = threading.Event()
        self.preview = None
        self.cameras = []
        self.index = 0
        self.profile = "720p"
        self.fps = 15
        self.rotation = 0
        self.schema = {}
        self.modes = []
        self.properties = {}
        self.output_error = None

    def start(self):
        try:
            from picamera2 import Picamera2

            self.cameras = plain(Picamera2.global_camera_info())
            if not self.cameras:
                raise RuntimeError(
                    "No camera detected. Check the ribbon cable and libcamera configuration, then retry."
                )
            self.open(self.cameras[0]["Num"], self.profile, self.fps)
        except Exception as exc:
            self.error = str(exc)
        self.recover_recordings()
        self.worker = threading.Thread(target=self._analyze, daemon=True)
        self.worker.start()

    def open(self, index, profile, fps, rotation=0):
        from libcamera import Transform, controls
        from picamera2 import Picamera2
        from picamera2.encoders import JpegEncoder
        from picamera2.outputs import FileOutput

        with self.lock:
            if self.recording:
                raise ValueError(
                    "Stop recording before changing camera or video settings."
                )
            if profile not in {"720p", "1080p"} or not 1 <= fps <= 30:
                raise ValueError("Choose 720p or 1080p and 1–30 fps.")
            if rotation not in {0, 180}:
                raise ValueError("Supported mounting rotations are 0° and 180°.")
            self.cameras = plain(Picamera2.global_camera_info())
            if not any(c["Num"] == index for c in self.cameras):
                raise ValueError("Camera is not available.")
            if self.camera:
                self.camera.stop_encoder()
                self.camera.stop()
                self.camera.close()
                self.camera = None
            self.schema = {}
            try:
                p = self.camera = Picamera2(index)
                self.modes = plain(p.sensor_modes)
                self.properties = plain(p.camera_properties)
                width, height = (1280, 720) if profile == "720p" else (1920, 1080)
                capable = [
                    m
                    for m in self.modes
                    if m["size"][0] >= width and m["size"][1] >= height
                ]
                maximum = max(m["fps"] for m in (capable or self.modes))
                fps = min(fps, maximum)
                config = p.create_video_configuration(
                    main={"size": (width, height), "format": "YUV420"},
                    lores={"size": (640, 360), "format": "YUV420"},
                    controls={"FrameRate": fps},
                    transform=Transform(hflip=rotation == 180, vflip=rotation == 180),
                    buffer_count=6,
                )
                p.configure(config)
                for name, (lo, hi, default) in p.camera_controls.items():
                    cid = p.camera_ctrl_info[name][0]
                    enum = getattr(controls, name + "Enum", None)
                    if enum is None and hasattr(controls, "draft"):
                        enum = getattr(controls.draft, name + "Enum", None)
                    self.schema[name] = dict(
                        min=plain(lo),
                        max=plain(hi),
                        default=plain(default),
                        type=str(cid.type).split(".")[-1],
                        size=cid.size,
                        options=(
                            {k: int(v) for k, v in enum.__members__.items()}
                            if enum
                            else {}
                        ),
                    )
                self.preview = JpegEncoder(q=70, num_threads=2)
                self.preview.frame_skip_count = max(1, round(fps / 10))
                self.preview.output = FileOutput(self.frames)
                self.applied = {}
                if "AfMode" in self.schema:
                    p.set_controls({"AfMode": controls.AfModeEnum.Continuous})
                    self.applied["AfMode"] = int(controls.AfModeEnum.Continuous)
                p.post_callback = self._metadata
                p.start()
                p.start_encoder(self.preview, name="lores")
                self.index, self.profile, self.fps = index, profile, fps
                self.rotation = rotation
                self.error = None
            except Exception as exc:
                self.error = str(exc)
                if self.camera:
                    self.camera.close()
                    self.camera = None
                raise

    def _metadata(self, request):
        self.metadata = plain(request.get_metadata())

    def _analyze(self):
        while not self.shutdown.wait(0.5):
            if not self.focus_enabled or not self.frames.frame:
                continue
            try:
                with Image.open(io.BytesIO(self.frames.frame)) as image:
                    scores = focus_grid(image)
                self.focus = scores
            except (ValueError, OSError):
                continue

    def require_camera(self):
        if self.camera is None:
            raise ValueError(self.error or "Camera is not ready.")

    def set_controls(self, values):
        with self.lock:
            self.require_camera()
            checked = validate_controls(values, self.schema)
            self.camera.set_controls(checked)
            self.applied.update(plain(checked))

    def _space(self):
        if shutil.disk_usage(self.root).free < 256 * 1024 * 1024:
            raise ValueError(
                "Less than 256 MB free. Download and remove captures before continuing."
            )

    def _new(self, kind):
        self._space()
        stamp = datetime.now(timezone.utc)
        ident = stamp.strftime("%Y%m%d_%H%M%S_") + uuid4().hex[:8]
        return dict(
            id=ident,
            kind=kind,
            created=stamp.isoformat(),
            camera=self.properties.get("Model"),
            settings=dict(self.applied),
            profile=self.profile,
            fps=self.fps,
            rotation=self.rotation,
        )

    def _finish(self, item, image):
        item["focus"] = focus_grid(image)
        if item["kind"] == "still":
            item["width"], item["height"] = image.size
        for suffix, size in [("thumb", (480, 320)), ("preview", (1600, 1200))]:
            copy = image.copy()
            copy.thumbnail(size)
            copy.convert("RGB").save(
                self.root / f"{item['id']}.{suffix}.jpg", quality=85
            )
        path = self.root / item["file"]
        item["bytes"] = path.stat().st_size
        temp = self.root / (item["id"] + ".json.tmp")
        temp.write_text(json.dumps(item))
        temp.replace(self.root / (item["id"] + ".json"))
        return item

    def capture(self):
        with self.lock:
            self.require_camera()
            item = self._new("still")
            item["file"] = item["id"] + ".jpg"
            path = self.root / item["file"]
            if self.recording:
                item["metadata"] = plain(self.camera.capture_file(str(path)))
                item["capture_mode"] = "video snapshot"
            else:
                self.camera.stop_encoder(self.preview)
                previous = self.camera.camera_configuration()
                try:
                    config = self.camera.create_still_configuration(
                        main={"size": self.camera.sensor_resolution},
                        buffer_count=2,
                        controls=self.applied,
                        transform=previous["transform"],
                    )
                    item["metadata"] = plain(
                        self.camera.switch_mode_and_capture_file(
                            config, str(path), delay=2
                        )
                    )
                    item["capture_mode"] = "full sensor"
                finally:
                    # Also restore preview after a failed file write or mode switch.
                    self.camera.stop()
                    self.camera.configure(previous)
                    self.camera.set_controls(self.applied)
                    self.camera.start()
                    self.camera.start_encoder(self.preview, name="lores")
            with Image.open(path) as image:
                return self._finish(item, image)

    def start_recording(self):
        from picamera2.encoders import H264Encoder
        from picamera2.outputs import PyavOutput

        with self.lock:
            self.require_camera()
            if self.recording:
                raise ValueError("A recording is already running.")
            item = self._new("video")
            item["file"] = item["id"] + ".mp4"
            item["width"], item["height"] = self.camera.stream_configuration("main")[
                "size"
            ]
            encoder = H264Encoder(
                bitrate=10_000_000, repeat=True, iperiod=max(1, round(self.fps))
            )
            output = PyavOutput(
                str(self.root / item["file"]),
                format="mp4",
                options={"movflags": "frag_keyframe+empty_moov+default_base_moof"},
            )
            self.output_error = None
            output.error_callback = self._output_failed
            (self.root / (item["id"] + ".pending.json")).write_text(json.dumps(item))
            self.camera.start_encoder(encoder, output, name="main")
            self.recording = dict(
                item=item,
                encoder=encoder,
                started=time.monotonic(),
                poster=self.frames.frame,
            )
            return item

    def stop_recording(self):
        with self.lock:
            if not self.recording:
                raise ValueError("No recording is running.")
            rec = self.recording
            self.camera.stop_encoder(rec["encoder"])
            self.recording = None
            item = rec["item"]
            item["duration"] = round(time.monotonic() - rec["started"], 2)
            item["metadata"] = self.metadata
            import av

            with av.open(str(self.root / item["file"])) as container:
                if container.duration:
                    item["duration"] = round(container.duration / av.time_base, 2)
            with Image.open(io.BytesIO(rec["poster"] or self.frames.frame)) as image:
                item = self._finish(item, image)
            (self.root / (item["id"] + ".pending.json")).unlink(missing_ok=True)
            return item

    def _output_failed(self, exc):
        self.output_error = str(exc)

    def recover_recordings(self):
        """Publish surviving fragments from interrupted recordings; never discard originals."""
        import av

        for path in self.root.glob("*.pending.json"):
            try:
                item = json.loads(path.read_text())
                with av.open(str(self.root / item["file"])) as container:
                    image = next(container.decode(video=0)).to_image()
                    item["duration"] = round(
                        (container.duration or 0) / av.time_base, 2
                    )
                item["recovered"] = True
                self._finish(item, image)
                path.unlink()
            except Exception as exc:
                self.error = f"An interrupted clip could not be recovered: {path.name}. Original preserved. {exc}"

    def status(self):
        rec = self.recording
        return dict(
            ready=self.camera is not None,
            error=self.error,
            cameras=self.cameras,
            index=self.index,
            profile=self.profile,
            fps=self.fps,
            rotation=self.rotation,
            frame_age=(
                round(time.monotonic() - self.frames.updated, 1)
                if self.frames.updated
                else None
            ),
            properties=self.properties,
            modes=self.modes,
            controls=self.schema,
            applied=self.applied,
            metadata=self.metadata,
            focus=self.focus if self.focus_enabled else [],
            focus_enabled=self.focus_enabled,
            recording=rec["item"]["id"] if rec else None,
            elapsed=round(time.monotonic() - rec["started"], 1) if rec else 0,
            free_bytes=shutil.disk_usage(self.root).free,
        )

    def close(self):
        self.shutdown.set()
        with self.lock:
            if self.recording:
                self.stop_recording()
            if self.camera:
                self.camera.stop_encoder()
                self.camera.stop()
                self.camera.close()
                self.camera = None


def validate_controls(values, schema):
    if not isinstance(values, dict) or not values:
        raise ValueError("Supply a non-empty controls object.")
    result = {}
    for name, value in values.items():
        if name not in schema:
            raise ValueError(f"Unsupported control: {name}")
        spec = schema[name]
        kind, size = spec["type"], spec["size"]
        if kind in {"Rectangle", "Size"}:
            length = 4 if kind == "Rectangle" else 2
            rectangles = value if size else [value]
            if not isinstance(rectangles, list) or not rectangles:
                raise ValueError(f"{name}: expected coordinates.")
            for rect in rectangles:
                if (
                    not isinstance(rect, list)
                    or len(rect) != length
                    or any(type(v) is not int or v < 0 for v in rect)
                ):
                    raise ValueError(
                        f"{name}: expected {length} non-negative integers per region."
                    )
            result[name] = [tuple(r) for r in rectangles] if size else tuple(value)
            continue
        vals = value if size else [value]
        if size and (not isinstance(vals, list) or (size > 0 and len(vals) != size)):
            raise ValueError(f"{name}: expected an array of {size} values.")
        for i, v in enumerate(vals):
            if kind == "Bool":
                if type(v) is not bool:
                    raise ValueError(f"{name}: expected true or false.")
                continue
            if type(v) not in (int, float) or not math.isfinite(v):
                raise ValueError(f"{name}: expected a finite number.")
            if kind.startswith("Integer") and type(v) is not int:
                raise ValueError(f"{name}: expected an integer.")
            for bound, compare in [
                ("min", lambda a, b: a < b),
                ("max", lambda a, b: a > b),
            ]:
                b = spec[bound]
                b = b[min(i, len(b) - 1)] if isinstance(b, list) else b
                if isinstance(b, (float, int)) and compare(v, b):
                    raise ValueError(
                        f"{name}: outside supported range {spec['min']}…{spec['max']}."
                    )
            if spec["options"] and v not in spec["options"].values():
                raise ValueError(f"{name}: invalid option.")
        if name == "FrameDurationLimits" and value[0] > value[1]:
            raise ValueError("Minimum frame duration must not exceed maximum.")
        result[name] = tuple(value) if size else value
    return result
