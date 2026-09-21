"""Single-owner Picamera2 service. Camera operations are serialized, frames are shared."""

import io
import json
import math
import random
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
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
        self.profile = "1080p"
        self.fps = 30
        self.rotation = 0
        self.schema = {}
        self.modes = []
        self.properties = {}
        self.output_error = None
        self.sequence = None
        self.sequence_stop = threading.Event()
        self.sequence_worker = None
        self.sequence_settings = {
            "stills": {"interval": 1, "count": 0, "controls": {}},
            "test": {
                "shutters": [1000, 2000, 4000, 8000],
                "gains": [2, 4, 8],
                "settle": 2,
                "samples": 3,
            },
            "road": {
                "reference_shutter_us": 4000,
                "reference_gain": 2,
                "comparison_shutter_us": 2000,
                "blocks": 8,
                "roi": [0.15, 0.45, 0.7, 0.45],
                "seed": None,
            },
        }
        self.road_condition = threading.Condition()
        self.road_armed = False
        self.road_request = None
        self.road_drops = 0
        self._offset_path = self.root / ".time_offset"
        self.time_offset = self._load_offset()

    def _load_offset(self):
        try:
            return float(self._offset_path.read_text().strip())
        except (OSError, ValueError):
            return 0.0

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
        self.recover_recordings()
        self.worker = threading.Thread(target=self._analyze, daemon=True)
        self.worker.start()

    def open(self, index, profile, fps, rotation=0):
        with self.lock:
            self._sequence_guard()
            if self.recording:
                raise ValueError(
                    "Stop recording before changing camera or video settings."
                )
            from libcamera import Transform, controls
            from picamera2 import Picamera2
            from picamera2.encoders import JpegEncoder
            from picamera2.outputs import FileOutput

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
                self._apply_motion_defaults()
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

    def _apply_motion_defaults(self):
        """Prefer short AE exposures without fixing brightness as daylight changes."""
        values = {
            name: True for name in ("AeEnable", "AwbEnable") if name in self.schema
        }
        for name in ("ExposureTimeMode", "AnalogueGainMode"):
            if name in self.schema:
                values[name] = 0  # libcamera Auto; do not pin either shutter or gain.
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
        self.metadata = plain(request.get_metadata())
        with self.road_condition:
            if not self.road_armed:
                return
            request.acquire()
            previous = self.road_request
            self.road_request = request
            if previous is not None:
                self.road_drops += 1
            self.road_condition.notify_all()
        if previous is not None:
            previous.release()

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
            self._sequence_guard()
            self.require_camera()
            checked = validate_controls(values, self.schema)
            self.camera.set_controls(checked)
            self.applied.update(plain(checked))

    def _sequence_guard(self):
        if (
            self.sequence
            and self.sequence["active"]
            and threading.current_thread() is not self.sequence_worker
        ):
            raise ValueError("Stop the capture sequence before changing the camera.")

    def _manual_exposure(self, shutter, gain):
        values = {"ExposureTime": shutter, "AnalogueGain": gain}
        for name in ("ExposureTimeMode", "AnalogueGainMode"):
            if name in self.schema:
                values[name] = self.schema[name]["options"].get("Manual", 1)
            elif "AeEnable" in self.schema:
                values["AeEnable"] = False
            else:
                raise ValueError(
                    "This camera does not advertise manual exposure control."
                )
        return validate_controls(values, self.schema)

    def start_sequence(self, mode, settings):
        with self.lock:
            self._sequence_guard()
            self.require_camera()
            if self.recording:
                raise ValueError("Stop recording before starting a capture sequence.")
            if (
                not isinstance(mode, str)
                or mode not in self.sequence_settings
                or not isinstance(settings, dict)
            ):
                raise ValueError("Choose stills, test, or road with a settings object.")
            if set(settings) - set(self.sequence_settings[mode]):
                raise ValueError("Unknown sequence setting.")
            settings = {**self.sequence_settings[mode], **settings}

            def number(name, low, high, integer=False):
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

            if mode == "stills":
                number("interval", 1, 86400)
                number("count", 0, 10000, True)
                values = settings["controls"]
                if not isinstance(values, dict):
                    raise ValueError("Still controls must be a JSON object.")
                plan = [validate_controls(values, self.schema) if values else {}]
                total = settings["count"]
            elif mode == "test":
                number("settle", 0.5, 30)
                number("samples", 1, 5, True)
                for name in ("shutters", "gains"):
                    values = settings[name]
                    if (
                        not isinstance(values, list)
                        or not 1 <= len(values) <= 8
                        or any(
                            type(v) not in (int, float)
                            or not math.isfinite(v)
                            or v <= 0
                            for v in values
                        )
                    ):
                        raise ValueError(f"{name}: supply 1–8 positive numbers.")
                plan = [
                    self._manual_exposure(s, g)
                    for s in settings["shutters"]
                    for g in settings["gains"]
                ]
                total = len(plan) * settings["samples"]
            else:
                if self.fps < 10:
                    raise ValueError(
                        "Road experiments require a video rate of at least 10 fps."
                    )
                for name in (
                    "reference_shutter_us",
                    "reference_gain",
                    "comparison_shutter_us",
                ):
                    number(name, 0.01, 1_000_000)
                number("blocks", 4, 12, True)
                if settings["blocks"] % 2:
                    raise ValueError("blocks must be an even number between 4 and 12.")
                roi = settings["roi"]
                if (
                    not isinstance(roi, list)
                    or len(roi) != 4
                    or any(
                        type(v) not in (int, float) or not math.isfinite(v) for v in roi
                    )
                    or roi[0] < 0
                    or roi[1] < 0
                    or roi[2] <= 0
                    or roi[3] <= 0
                    or roi[0] + roi[2] > 1
                    or roi[1] + roi[3] > 1
                ):
                    raise ValueError("roi must be normalized [x, y, width, height].")
                seed = settings["seed"]
                if seed is None:
                    seed = int.from_bytes(uuid4().bytes[:4], "big")
                if type(seed) is not int or not 0 <= seed <= 0xFFFFFFFF:
                    raise ValueError("seed must be an integer from 0 to 4294967295.")
                settings["seed"] = seed
                product = settings["reference_shutter_us"] * settings["reference_gain"]
                comparison_gain = product / settings["comparison_shutter_us"]
                if (
                    comparison_gain == settings["reference_gain"]
                    and settings["comparison_shutter_us"]
                    == settings["reference_shutter_us"]
                ):
                    raise ValueError(
                        "Road experiment arms must use different settings."
                    )
                frame_budget = 1_000_000 / self.fps
                if (
                    max(
                        settings["reference_shutter_us"],
                        settings["comparison_shutter_us"],
                    )
                    > frame_budget
                ):
                    raise ValueError(
                        f"Shutter must not exceed the {frame_budget:.0f} µs frame period."
                    )
                arms = {
                    "A": self._manual_exposure(
                        settings["reference_shutter_us"], settings["reference_gain"]
                    ),
                    "B": self._manual_exposure(
                        settings["comparison_shutter_us"], comparison_gain
                    ),
                }
                orders = ["ABBA"] * (settings["blocks"] // 2) + ["BAAB"] * (
                    settings["blocks"] // 2
                )
                random.Random(seed).shuffle(orders)
                plan = {"arms": arms, "orders": orders}
                total = settings["blocks"] * 4
            self._space()
            self.sequence_settings[mode] = plain(settings)
            self.sequence_stop.clear()
            self.sequence = dict(
                id=uuid4().hex,
                mode=mode,
                active=True,
                completed=0,
                total=total,
                current={},
                results=[],
                error=None,
                settings=plain(settings),
            )
            saved = (
                self.index,
                self.profile,
                self.fps,
                self.rotation,
                dict(self.applied),
            )
            self.sequence_worker = threading.Thread(
                target=self._run_sequence, args=(plan, saved), daemon=True
            )
            self.sequence_worker.start()
            return dict(self.sequence)

    def stop_sequence(self):
        # Do not wait for the camera lock: a full-resolution capture may be in progress.
        self.sequence_stop.set()
        with self.road_condition:
            self.road_condition.notify_all()
        return {"ok": True}

    def _run_sequence(self, plan, saved):
        run = self.sequence
        settings = run["settings"]
        try:
            if run["mode"] == "road":
                self._run_road(plan)
            else:
                while not self.sequence_stop.is_set() and not self.shutdown.is_set():
                    started = time.monotonic()
                    index = run["completed"]
                    values = (
                        plan[index // settings["samples"]]
                        if run["mode"] == "test"
                        else plan[0]
                    )
                    with self.lock:
                        run["current"] = plain(values)
                        if values:
                            self.set_controls(values)
                    if run["mode"] == "test" and self.sequence_stop.wait(
                        settings["settle"]
                    ):
                        break
                    if self.sequence_stop.is_set() or self.shutdown.is_set():
                        break
                    item = self.capture()
                    with self.lock:
                        run["completed"] += 1
                        result = {
                            k: item[k] for k in ("id", "metadata", "focus", "settings")
                        }
                        run["results"] = (
                            (run["results"] + [result])
                            if run["mode"] == "test"
                            else [result]
                        )
                    if run["total"] and run["completed"] >= run["total"]:
                        break
                    if run["mode"] == "stills":
                        if self.sequence_stop.wait(
                            max(0, settings["interval"] - (time.monotonic() - started))
                        ):
                            break
        except Exception as exc:
            run["error"] = str(exc)
        finally:
            self._road_disarm()
            with self.lock:
                try:
                    # Reconfigure to restore automatic defaults as well as explicitly set controls.
                    self.open(*saved[:4])
                    if saved[4]:
                        self.set_controls(saved[4])
                except Exception as exc:
                    run["error"] = (
                        run["error"] or ""
                    ) + f" Could not restore camera: {exc}"
                run["active"] = False

    def _road_disarm(self):
        with self.road_condition:
            self.road_armed = False
            request = self.road_request
            self.road_request = None
            self.road_condition.notify_all()
        if request is not None:
            request.release()

    def _road_next(self, last_timestamp, timeout):
        deadline = time.monotonic() + timeout
        while not self.sequence_stop.is_set() and not self.shutdown.is_set():
            with self.road_condition:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        "Camera produced no fresh road-experiment frame."
                    )
                self.road_condition.wait_for(
                    lambda: self.road_request is not None
                    or self.sequence_stop.is_set()
                    or self.shutdown.is_set(),
                    timeout=remaining,
                )
                request = self.road_request
                self.road_request = None
            if request is None:
                continue
            try:
                metadata = plain(request.get_metadata())
                timestamp = metadata.get("SensorTimestamp")
                if type(timestamp) not in (int, float) or timestamp <= last_timestamp:
                    continue
                width, height = self.camera.stream_configuration("main")["size"]
                array = np.asarray(request.make_array("main"))[:height, :width].copy()
                return timestamp, metadata, array
            finally:
                request.release()
        raise InterruptedError

    @staticmethod
    def _road_matches(metadata, controls):
        exposure = metadata.get("ExposureTime")
        gain = metadata.get("AnalogueGain")
        target_exposure = controls["ExposureTime"]
        target_gain = controls["AnalogueGain"]
        return (
            type(exposure) in (int, float)
            and type(gain) in (int, float)
            and abs(exposure - target_exposure) <= max(50, target_exposure * 0.03)
            and abs(gain - target_gain) <= max(0.05, target_gain * 0.05)
        )

    def _road_slot(self, arm, controls, changed, last_timestamp):
        started = time.monotonic()
        stable_since = None
        consecutive = 0
        discarded = 0
        frames = []
        while len(frames) < 3:
            if time.monotonic() - started > 3:
                raise TimeoutError(
                    f"Arm {arm} controls did not settle within 3 seconds."
                )
            timestamp, metadata, array = self._road_next(last_timestamp, 1)
            last_timestamp = timestamp
            if not self._road_matches(metadata, controls):
                stable_since = None
                consecutive = 0
                discarded += 1
                continue
            consecutive += 1
            stable_since = stable_since or timestamp
            settled = not changed or (
                consecutive >= 3 and timestamp - stable_since >= 200_000_000
            )
            if not settled:
                discarded += 1
                continue
            frames.append((array, metadata))
        return (
            frames,
            last_timestamp,
            {
                "duration_seconds": round(time.monotonic() - started, 3),
                "discarded": discarded,
                "matching_frames": consecutive,
            },
        )

    def _write_manifest(self, path, manifest):
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(plain(manifest)))
        temporary.replace(path)

    def _run_road(self, plan):
        run = self.sequence
        settings = run["settings"]
        experiment_id = uuid4().hex
        directory = self.root / "experiments" / experiment_id
        frames_dir = directory / "frames"
        frames_dir.mkdir(parents=True)
        width, height = self.camera.stream_configuration("main")["size"]
        configured = self.camera.camera_configuration()
        colour_space = configured.get("colour_space")
        negotiated_range = getattr(colour_space, "range", None)
        range_name = str(negotiated_range).lower()
        y_range = "full" if "full" in range_name else "limited"
        estimated_block = width * height * 12
        if shutil.disk_usage(self.root).free < 256 * 1024 * 1024 + estimated_block:
            raise ValueError("Not enough free space for one road-experiment block.")
        manifest = {
            "id": experiment_id,
            "schema_version": 1,
            "state": "active",
            "created": datetime.now(timezone.utc).isoformat(),
            "settings": settings,
            "schedule": plan["orders"],
            "pipeline": {
                "camera": self.properties.get("Model"),
                "profile": self.profile,
                "fps": self.fps,
                "rotation": self.rotation,
                "width": width,
                "height": height,
                "format": "YUV420",
                "colour_space": plain(colour_space),
                "y_range": y_range,
                "y_range_source": (
                    "negotiated colour space"
                    if negotiated_range is not None
                    else "Picamera2 YUV420 video default"
                ),
                "applied_controls": dict(self.applied),
            },
            "arms": {
                arm: {
                    "ExposureTime": values["ExposureTime"],
                    "AnalogueGain": values["AnalogueGain"],
                }
                for arm, values in plan["arms"].items()
            },
            "blocks": [],
            "drops": 0,
        }
        run["experiment_id"] = experiment_id
        run["schedule"] = plan["orders"]
        self._write_manifest(directory / "manifest.json", manifest)
        self.road_drops = 0
        last_timestamp = -1
        previous_arm = None
        frame_number = 0
        try:
            # Lock colour and electronic focus where the camera exposes reliable values.
            locks = {}
            if "AwbEnable" in self.schema:
                if (
                    "ColourGains" not in self.schema
                    or "ColourGains" not in self.metadata
                ):
                    raise ValueError(
                        "Road experiment cannot lock automatic white balance."
                    )
                locks.update(AwbEnable=False, ColourGains=self.metadata["ColourGains"])
            if "AfMode" in self.schema:
                manual = self.schema["AfMode"]["options"].get("Manual")
                if manual is None or "LensPosition" not in self.metadata:
                    raise ValueError("Road experiment cannot lock automatic focus.")
                locks.update(AfMode=manual, LensPosition=self.metadata["LensPosition"])
            if locks:
                with self.lock:
                    self.set_controls(locks)

            with self.road_condition:
                self.road_armed = True
            for block_index, order in enumerate(plan["orders"]):
                if self.sequence_stop.is_set() or self.shutdown.is_set():
                    break
                if (
                    shutil.disk_usage(self.root).free
                    < 256 * 1024 * 1024 + estimated_block
                ):
                    raise ValueError(
                        "Not enough free space for another road-experiment block."
                    )
                block = {"index": block_index, "order": order, "slots": []}
                buffered = []
                for slot_index, arm in enumerate(order):
                    controls = plan["arms"][arm]
                    changed = arm != previous_arm
                    if changed:
                        with self.lock:
                            run["current"] = {
                                "arm": arm,
                                "ExposureTime": controls["ExposureTime"],
                                "AnalogueGain": controls["AnalogueGain"],
                            }
                            self.set_controls(controls)
                    frames, last_timestamp, settling = self._road_slot(
                        arm, controls, changed, last_timestamp
                    )
                    slot = {
                        "index": slot_index,
                        "arm": arm,
                        "requested_controls": {
                            "ExposureTime": controls["ExposureTime"],
                            "AnalogueGain": controls["AnalogueGain"],
                        },
                        "settling": settling,
                        "frames": [],
                    }
                    for array, metadata in frames:
                        name = f"{frame_number:06d}.pgm"
                        frame_number += 1
                        buffered.append((name, array))
                        slot["frames"].append(
                            {
                                "file": "frames/" + name,
                                "width": width,
                                "height": height,
                                "metadata": metadata,
                            }
                        )
                    block["slots"].append(slot)
                    previous_arm = arm
                    run["completed"] += 1
                for name, array in buffered:
                    (frames_dir / name).write_bytes(
                        f"P5\n{width} {height}\n255\n".encode() + array.tobytes()
                    )
                manifest["blocks"].append(block)
                manifest["drops"] = self.road_drops
                self._write_manifest(directory / "manifest.json", manifest)
            manifest["state"] = (
                "cancelled"
                if self.sequence_stop.is_set() or self.shutdown.is_set()
                else "complete"
            )
        except InterruptedError:
            manifest["state"] = "cancelled"
        except Exception:
            manifest["state"] = "failed"
            raise
        finally:
            manifest["drops"] = self.road_drops
            manifest["completed_slots"] = run["completed"]
            manifest["finished"] = datetime.now(timezone.utc).isoformat()
            self._write_manifest(directory / "manifest.json", manifest)

    def _space(self):
        if shutil.disk_usage(self.root).free < 256 * 1024 * 1024:
            raise ValueError(
                "Less than 256 MB free. Download and remove captures before continuing."
            )

    def _new(self, kind):
        self._space()
        stamp = datetime.now(timezone.utc) + timedelta(seconds=self.time_offset)
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
            self._sequence_guard()
            self.require_camera()
            item = self._new("still")
            if self.sequence and self.sequence["active"]:
                item["sequence"] = {
                    "id": self.sequence["id"],
                    "mode": self.sequence["mode"],
                    "number": self.sequence["completed"] + 1,
                    "settings": self.sequence["settings"],
                }
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
        with self.lock:
            self._sequence_guard()
            self.require_camera()
            if self.recording:
                raise ValueError("A recording is already running.")
            from picamera2.encoders import H264Encoder
            from picamera2.outputs import PyavOutput

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
            sequence=self.sequence,
            sequence_settings=self.sequence_settings,
            elapsed=round(time.monotonic() - rec["started"], 1) if rec else 0,
            free_bytes=shutil.disk_usage(self.root).free,
        )

    def close(self):
        self.shutdown.set()
        self.sequence_stop.set()
        if self.sequence_worker:
            self.sequence_worker.join()
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
