"""Single-owner, continuous full-sensor photo pipeline with a shared JPEG preview."""

import io
import json
import math
import queue
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from PIL import Image


def plain(value):
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class Frames(io.BufferedIOBase):
    def __init__(self):
        super().__init__()
        self.condition = threading.Condition()
        self.frame = None
        self.sequence = 0
        self.updated = 0

    def reset(self):
        with self.condition:
            self.frame = None
            self.updated = 0
            self.condition.notify_all()

    def write(self, data):
        with self.condition:
            self.frame = bytes(data)
            self.sequence += 1
            self.updated = time.monotonic()
            self.condition.notify_all()
        return len(data)


def yuv420_planes(array, width, height):
    """Split stride-padded Picamera2 YUV420 into Y, U and V views."""
    y = array[:height, :width]
    halves = array.reshape(array.shape[0] * 2, array.shape[1] // 2)
    u = halves[2 * height : 2 * height + height // 2, : width // 2]
    v = halves[2 * height + height // 2 : 3 * height, : width // 2]
    return y, u, v


def fitted(size, box):
    scale = min(box[0] / size[0], box[1] / size[1], 1)
    return max(1, round(size[0] * scale)), max(1, round(size[1] * scale))


class BurstSampler:
    """Nonblocking rate selection; one queued request, with explicit skipped frames."""

    def __init__(self, rate, count=0):
        self.period = 1e9 / rate
        self.remaining = count or None
        self.mailbox = queue.Queue(maxsize=1)
        self.lock = threading.Lock()
        self.due = None
        self.selected = 0
        self.skipped = 0
        self.closed = False

    @property
    def done(self):
        return self.remaining == 0

    def offer(self, request, metadata):
        timestamp = metadata.get("SensorTimestamp")
        with self.lock:
            if self.closed or self.done or type(timestamp) is not int:
                return
            if self.due is not None and timestamp < self.due:
                return
            request.acquire()
            captured = datetime.now(timezone.utc)
            try:
                self.mailbox.put_nowait(
                    (request, plain(metadata), self.selected + 1, captured)
                )
            except queue.Full:
                request.release()
                self.skipped += 1
                return
            self.selected += 1
            if self.remaining:
                self.remaining -= 1
            late = self.due is None or timestamp - self.due >= self.period
            self.due = (timestamp if late else self.due) + self.period

    def close(self):
        with self.lock:
            self.closed = True

    def drain(self):
        while True:
            try:
                request = self.mailbox.get_nowait()[0]
            except queue.Empty:
                return
            request.release()


class CameraDeck:
    def __init__(self, root, media_published=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.media_published = media_published
        self.lock = threading.RLock()
        self.frames = Frames()
        self.camera = None
        self.metadata = {}
        self.metadata_updated = 0
        self.error = None
        self.applied = {}
        self.shutdown = threading.Event()
        self.preview = None
        self.cameras = []
        self.index = 0
        self.profile = "full sensor"
        self.fps = 30
        self._rotation_path = self.root / ".rotation"
        try:
            self.rotation = int(self._rotation_path.read_text().strip())
        except (OSError, ValueError):
            self.rotation = 0
        if self.rotation not in {0, 180}:
            self.rotation = 0
        self.schema = {}
        self.modes = []
        self.properties = {}
        self.sequence = None
        self.sequence_stop = threading.Event()
        self.sequence_worker = None
        self.recovery_worker = None
        self.sequence_settings = {
            "burst": {
                "rate": 5,
                "count": 0,
                "quality": 90,
                "threads": 3,
                "controls": {},
            }
        }
        self.burst = None
        self._offset_path = self.root / ".time_offset"
        try:
            self.time_offset = float(self._offset_path.read_text().strip())
        except (OSError, ValueError):
            self.time_offset = 0.0

    def save_offset(self, seconds):
        self.time_offset = float(seconds)
        self._offset_path.write_text(str(self.time_offset))

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
        if self.recovery_worker is None:
            self.recovery_worker = threading.Thread(
                target=self.recover_recordings, daemon=True
            )
            self.recovery_worker.start()

    def _burst_fps(self):
        if not self.modes:
            return None
        area = max(m["size"][0] * m["size"][1] for m in self.modes)
        return max(m["fps"] for m in self.modes if m["size"][0] * m["size"][1] == area)

    def open(self, index, profile, fps, rotation=None):
        with self.lock:
            self._sequence_guard()
            from libcamera import ColorSpace, Transform, controls
            from picamera2 import Picamera2
            from picamera2.encoders import JpegEncoder
            from picamera2.outputs import FileOutput

            # Legacy preview labels remain accepted, but never crop the photo stream.
            if profile not in {"full sensor", "720p", "1080p"}:
                raise ValueError("Choose full sensor.")
            if (
                type(fps) not in (int, float)
                or not math.isfinite(fps)
                or not 1 <= fps <= 30
            ):
                raise ValueError("Choose 1–30 fps.")
            rotation = self.rotation if rotation is None else rotation
            if rotation not in {0, 180}:
                raise ValueError("Supported mounting rotations are 0° and 180°.")
            self.cameras = plain(Picamera2.global_camera_info())
            if not any(c["Num"] == index for c in self.cameras):
                raise ValueError("Camera is not available.")
            previous_controls = dict(self.applied) if index == self.index else {}
            if self.camera:
                self.camera.stop_encoder()
                self.camera.stop()
                self.camera.close()
                self.camera = None
            self.frames.reset()
            self.metadata, self.metadata_updated = {}, 0
            self.schema = {}
            try:
                p = self.camera = Picamera2(index)
                self.modes = plain(p.sensor_modes)
                self.properties = plain(p.camera_properties)
                maximum = self._burst_fps()
                if maximum is not None:
                    fps = min(fps, maximum)
                config = p.create_video_configuration(
                    main={"size": p.sensor_resolution, "format": "YUV420"},
                    lores={"size": (640, 360), "format": "YUV420"},
                    controls={"FrameRate": fps},
                    transform=Transform(hflip=rotation == 180, vflip=rotation == 180),
                    colour_space=ColorSpace.Sycc(),
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
                self._apply_motion_defaults()
                if "AfMode" in self.schema:
                    self.set_controls({"AfMode": int(controls.AfModeEnum.Continuous)})
                if previous_controls:
                    self.set_controls(previous_controls)
                self.index, self.profile, self.fps, self.rotation = (
                    index,
                    "full sensor",
                    fps,
                    rotation,
                )
                p.post_callback = self._metadata
                p.start()
                p.start_encoder(self.preview, name="lores")
                temporary = self._rotation_path.with_suffix(".tmp")
                temporary.write_text(str(rotation))
                temporary.replace(self._rotation_path)
                self.error = None
            except Exception as exc:
                self.error = str(exc)
                if self.camera:
                    self.camera.close()
                    self.camera = None
                self.frames.reset()
                self.metadata, self.metadata_updated = {}, 0
                raise

    def _apply_motion_defaults(self):
        values = {
            name: True for name in ("AeEnable", "AwbEnable") if name in self.schema
        }
        for name in ("ExposureTimeMode", "AnalogueGainMode"):
            if name in self.schema:
                values[name] = self.schema[name]["options"].get("Auto", 0)
        spec = self.schema.get("AeExposureMode")
        if spec:
            for label in ("Short", "ExposureShort"):
                short = spec["options"].get(label)
                if short is not None and spec["min"] <= short <= spec["max"]:
                    values["AeExposureMode"] = short
                    break
        if values:
            self.set_controls(values)

    def _metadata(self, request):
        metadata = plain(request.get_metadata())
        self.metadata = metadata
        self.metadata_updated = time.monotonic()
        burst = self.burst
        if burst is not None:
            burst.offer(request, metadata)

    def require_camera(self):
        if self.camera is None or self.shutdown.is_set():
            raise ValueError(self.error or "Camera is not ready.")

    def _sequence_guard(self):
        if (
            self.sequence
            and self.sequence["active"]
            and threading.current_thread() is not self.sequence_worker
        ):
            raise ValueError("Stop the capture sequence before changing the camera.")

    def set_controls(self, values):
        with self.lock:
            self._sequence_guard()
            self.require_camera()
            checked = validate_controls(values, self.schema)
            self.camera.set_controls(checked)
            self.applied.update(
                plain({k: v for k, v in checked.items() if k != "AfTrigger"})
            )
            # Requested manual numbers are not the actual readings once auto is enabled.
            for mode, field in (
                ("ExposureTimeMode", "ExposureTime"),
                ("AnalogueGainMode", "AnalogueGain"),
            ):
                auto = self.schema.get(mode, {}).get("options", {}).get("Auto", 0)
                if self.applied.get(mode) == auto or (
                    mode not in self.schema and self.applied.get("AeEnable") is True
                ):
                    self.applied.pop(field, None)
            if self.applied.get("AwbEnable") is True:
                self.applied.pop("ColourGains", None)

    def start_sequence(self, mode, settings):
        with self.lock:
            self._sequence_guard()
            self.require_camera()
            if mode != "burst" or not isinstance(settings, dict):
                raise ValueError("Choose burst with a settings object.")
            if set(settings) - set(self.sequence_settings["burst"]):
                raise ValueError("Unknown sequence setting.")
            settings = {**self.sequence_settings["burst"], **settings}
            ceiling = min(30, self.fps, self._burst_fps() or 30)
            for name, low, high, integer in (
                ("rate", 1 / 86400, ceiling, False),
                ("count", 0, 100000, True),
                ("quality", 1, 100, True),
                ("threads", 1, 4, True),
            ):
                value = settings[name]
                if (
                    type(value) not in (int, float)
                    or not math.isfinite(value)
                    or not low <= value <= high
                    or (integer and type(value) is not int)
                ):
                    raise ValueError(
                        f"{name} must be {'an integer' if integer else 'a number'} between {low} and {high}."
                    )
            values = settings["controls"]
            if not isinstance(values, dict):
                raise ValueError("Burst controls must be a JSON object.")
            checked = validate_controls(values, self.schema) if values else {}
            # Changing cadence behind the sampler would make its requested rate misleading.
            if set(checked) & {"FrameRate", "FrameDurationLimits"}:
                raise ValueError("Set stream fps before starting a burst.")
            saved = dict(self.applied)
            # Resolve restoration before applying any temporary overrides.
            restore = {
                k: saved.get(k, self.schema[k]["default"])
                for k in checked
                if k != "AfTrigger"
            }
            restore.update(saved)
            for auto_mode, field in (
                ("ExposureTimeMode", "ExposureTime"),
                ("AnalogueGainMode", "AnalogueGain"),
            ):
                auto = self.schema.get(auto_mode, {}).get("options", {}).get("Auto", 0)
                if saved.get(auto_mode) == auto or (
                    auto_mode not in self.schema and saved.get("AeEnable") is True
                ):
                    restore.pop(field, None)
            if saved.get("AwbEnable") is True:
                restore.pop("ColourGains", None)
            if restore:
                try:
                    validate_controls(restore, self.schema)
                except ValueError as exc:
                    raise ValueError(f"Cannot restore burst controls: {exc}") from exc
            self._space()
            self.sequence_settings["burst"] = plain(settings)
            self.sequence_stop.clear()
            self.sequence = dict(
                id=uuid4().hex,
                mode="burst",
                active=True,
                completed=0,
                total=settings["count"],
                current=plain(checked),
                results=[],
                error=None,
                settings=plain(settings),
                rate=None,
                skipped=0,
                selected=0,
                publish_rate=None,
            )
            self.sequence_worker = threading.Thread(
                target=self._run_sequence, args=(checked, saved, restore), daemon=True
            )
            self.sequence_worker.start()
            return plain(self.sequence)

    def stop_sequence(self):
        self.sequence_stop.set()
        return {"ok": True}

    def _run_sequence(self, values, saved, restore):
        run = self.sequence
        try:
            if values:
                self.set_controls(values)
            self._run_burst()
        except Exception as exc:
            run["error"] = str(exc)
        finally:
            with self.lock:
                try:
                    if not self.shutdown.is_set():
                        if restore:
                            self.set_controls(restore)
                        self.applied = saved
                except Exception as exc:
                    run["error"] = (
                        run["error"] or ""
                    ) + f" Could not restore camera: {exc}"
                run["active"] = False

    def _run_burst(self):
        run = self.sequence
        settings = run["settings"]
        size = tuple(self.camera.stream_configuration("main")["size"])
        sampler = BurstSampler(settings["rate"], settings["count"])
        progress = threading.Lock()
        span, failures = [], []
        started = time.monotonic()

        def encode():
            while True:
                try:
                    request, metadata, number, captured = sampler.mailbox.get(
                        timeout=0.1
                    )
                except queue.Empty:
                    if sampler.closed and sampler.mailbox.empty():
                        return
                    continue
                try:
                    try:
                        array = request.make_array("main").copy()
                    finally:
                        request.release()
                    item = self._new("still", captured)
                    item["sequence"] = dict(
                        id=run["id"], mode="burst", number=number, settings=settings
                    )
                    item = self._encode_photo(
                        item, array, size, metadata, settings["quality"]
                    )
                    timestamp = metadata["SensorTimestamp"]
                    with progress:
                        run["completed"] += 1
                        run["results"] = [
                            {k: item[k] for k in ("id", "metadata", "settings")}
                        ]
                        span[:] = (
                            [min(span[0], timestamp), max(span[1], timestamp)]
                            if span
                            else [timestamp, timestamp]
                        )
                        if span[1] > span[0]:
                            run["rate"] = round(
                                (run["completed"] - 1) * 1e9 / (span[1] - span[0]), 2
                            )
                        run["publish_rate"] = round(
                            run["completed"] / (time.monotonic() - started), 2
                        )
                except Exception as exc:
                    failures.append(exc)
                    # Keep draining selected requests, but expose every failure.

        workers = [
            threading.Thread(target=encode, daemon=True)
            for _ in range(settings["threads"])
        ]
        for worker in workers:
            worker.start()
        self.burst = sampler
        try:
            while not (
                sampler.done
                or failures
                or self.sequence_stop.is_set()
                or self.shutdown.is_set()
            ):
                self.sequence_stop.wait(0.1)
                run["skipped"], run["selected"] = sampler.skipped, sampler.selected
        finally:
            sampler.close()
            self.burst = None
            for worker in workers:
                worker.join()
            sampler.drain()
            run["skipped"], run["selected"] = sampler.skipped, sampler.selected
        if failures:
            raise RuntimeError(
                f"{len(failures)} selected photo(s) failed: {failures[0]}"
            )

    def _space(self):
        if shutil.disk_usage(self.root).free < 256 * 1024 * 1024:
            raise ValueError(
                "Less than 256 MB free. Download and remove captures before continuing."
            )

    def _new(self, kind, captured=None):
        self._space()
        stamp = (captured or datetime.now(timezone.utc)) + timedelta(
            seconds=self.time_offset
        )
        ident = (
            stamp.strftime("%Y%m%d_%H%M%S_")
            + f"{stamp.microsecond // 1000:03d}"
            + uuid4().hex[:5]
        )
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
        if item["kind"] == "still" and "width" not in item:
            item["width"], item["height"] = image.size
        for suffix, size in [("thumb", (480, 320)), ("preview", (1600, 1200))]:
            copy = image.copy()
            copy.thumbnail(size)
            copy.convert("RGB").save(
                self.root / f"{item['id']}.{suffix}.jpg", quality=85
            )
        item["bytes"] = (self.root / item["file"]).stat().st_size
        temp = self.root / (item["id"] + ".json.tmp")
        temp.write_text(json.dumps(item))
        temp.replace(self.root / (item["id"] + ".json"))
        if self.media_published is not None:
            self.media_published(item)
        return item

    def _encode_photo(self, item, array, size, metadata, quality):
        from simplejpeg import encode_jpeg_yuv_planes

        width, height = size
        jpeg = encode_jpeg_yuv_planes(
            *yuv420_planes(array, width, height), quality=quality
        )
        item.update(
            file=item["id"] + ".jpg",
            capture_mode="full sensor",
            width=width,
            height=height,
            metadata=metadata,
        )
        (self.root / item["file"]).write_bytes(jpeg)
        with Image.open(io.BytesIO(jpeg)) as image:
            image.draft("RGB", fitted(size, (1600, 1200)))
            return self._finish(item, image)

    def capture(self):
        with self.lock:
            self._sequence_guard()
            self.require_camera()
            self._space()
            request = self.camera.capture_request()
            captured = datetime.now(timezone.utc)
            try:
                metadata = plain(request.get_metadata())
                array = request.make_array("main").copy()
                size = tuple(self.camera.stream_configuration("main")["size"])
            finally:
                request.release()
            return self._encode_photo(
                self._new("still", captured), array, size, metadata, 90
            )

    def recover_recordings(self):
        """Background file-only recovery; pending files and originals survive failures."""
        import av

        for path in self.root.glob("*.pending.json"):
            if self.shutdown.is_set():
                return
            try:
                item = json.loads(path.read_text())
                with av.open(str(self.root / item["file"])) as container:
                    image = next(container.decode(video=0)).to_image()
                    item["duration"] = round(
                        (container.duration or 0) / av.time_base, 2
                    )
                if self.shutdown.is_set():
                    return
                item["recovered"] = True
                self._finish(item, image)
                path.unlink()
            except Exception as exc:
                self.error = f"An interrupted clip could not be recovered: {path.name}. Original preserved. {exc}"

    def status(self):
        now = time.monotonic()
        return dict(
            ready=self.camera is not None and not self.shutdown.is_set(),
            error=self.error,
            cameras=self.cameras,
            index=self.index,
            profile=self.profile,
            fps=self.fps,
            rotation=self.rotation,
            properties=plain(self.properties),
            controls=plain(self.schema.copy()),
            applied=plain(self.applied.copy()),
            metadata=plain(self.metadata),
            metadata_age=(
                round(now - self.metadata_updated, 2) if self.metadata_updated else None
            ),
            frame_age=(
                round(now - self.frames.updated, 2) if self.frames.updated else None
            ),
            sequence=plain(self.sequence),
            sequence_settings=plain(self.sequence_settings),
            burst_fps=self._burst_fps(),
            free_bytes=shutil.disk_usage(self.root).free,
        )

    def close(self):
        self.shutdown.set()
        self.sequence_stop.set()
        if self.sequence_worker:
            self.sequence_worker.join()
        if self.recovery_worker:
            # Decode may be stuck in a third-party demuxer: do not block server teardown.
            self.recovery_worker.join(timeout=2)
        with self.lock:
            if self.camera:
                self.camera.stop_encoder()
                self.camera.stop()
                self.camera.close()
                self.camera = None
            self.frames.reset()
            self.metadata, self.metadata_updated = {}, 0


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
