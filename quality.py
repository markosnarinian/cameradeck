"""Offline analysis for CameraDeck road exposure experiments.

The module deliberately depends only on NumPy and Pillow.  ``analyze_run`` is
the programmatic entry point; the command line writes the same returned report.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from statistics import median
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageOps

# A positive delta always means that B is better.  Keep this table as the one
# source of truth when adding metrics.
METRIC_DIRECTIONS = {
    "luminance": 0,  # descriptive; closeness to midrange is handled separately
    "darkness": -1,
    "clipping": -1,
    "detail": 1,
    "noise": -1,
    "smear": -1,
}

MIN_BLOCKS = 6
REQUIRED_METADATA = (
    "ExposureTime",
    "AnalogueGain",
    "DigitalGain",
    "SensorTimestamp",
    "FrameDuration",
)


def _finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _roi_box(roi: Any, width: int, height: int) -> tuple[int, int, int, int]:
    if isinstance(roi, dict):
        vals = [roi.get(k) for k in ("x", "y", "width", "height")]
    elif isinstance(roi, (list, tuple)) and len(roi) == 4:
        vals = list(roi)
    else:
        vals = [0.0, 0.35, 1.0, 0.65]  # road-biased default
    if not all(_finite_number(x) for x in vals):
        raise ValueError("invalid normalized roi")
    x, y, w, h = map(float, vals)
    if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > 1.000001 or y + h > 1.000001:
        raise ValueError("invalid normalized roi")
    left, top = int(round(x * width)), int(round(y * height))
    right, bottom = int(round((x + w) * width)), int(round((y + h) * height))
    if right - left < 8 or bottom - top < 8:
        raise ValueError("roi is too small")
    return left, top, right, bottom


def _smooth(a: np.ndarray) -> np.ndarray:
    p = np.pad(a, 1, mode="reflect")
    return (
        p[:-2, :-2]
        + 2 * p[:-2, 1:-1]
        + p[:-2, 2:]
        + 2 * p[1:-1, :-2]
        + 4 * p[1:-1, 1:-1]
        + 2 * p[1:-1, 2:]
        + p[2:, :-2]
        + 2 * p[2:, 1:-1]
        + p[2:, 2:]
    ) / 16.0


def frame_metrics(image: np.ndarray, y_range: str = "full") -> dict[str, float | None]:
    """Measure one already-cropped luminance image."""
    a = np.asarray(image, dtype=np.float32)
    if a.ndim != 2 or min(a.shape) < 8:
        raise ValueError("luminance frame must be a 2-D image at least 8x8")
    lo, hi = (16.0, 235.0) if y_range == "limited" else (0.0, 255.0)
    if y_range not in ("full", "limited"):
        raise ValueError("pipeline.y_range must be 'full' or 'limited'")
    scale = hi - lo
    norm = np.clip((a - lo) / scale, 0, 1)
    darkness = float(np.mean(a <= lo + 0.04 * scale))
    clipping = float(np.mean(a >= hi - 0.02 * scale))

    s = _smooth(norm)
    gx = s[:, 2:] - s[:, :-2]
    gy = s[2:, :] - s[:-2, :]
    grad = np.hypot(gx[1:-1], gy[:, 1:-1])

    # High-pass residual.  Select genuinely flat, unsaturated 8x8 cells, so
    # texture/edges do not masquerade as noise.
    residual = (
        norm[1:-1, 1:-1]
        - (norm[:-2, 1:-1] + norm[2:, 1:-1] + norm[1:-1, :-2] + norm[1:-1, 2:]) / 4
    )
    valid = (grad < 0.05) & (norm[1:-1, 1:-1] > 0.05) & (norm[1:-1, 1:-1] < 0.95)
    samples: list[np.ndarray] = []
    cells = 0
    for yy in range(0, valid.shape[0] - 7, 8):
        for xx in range(0, valid.shape[1] - 7, 8):
            mask = valid[yy : yy + 8, xx : xx + 8]
            if mask.mean() >= 0.8:
                samples.append(residual[yy : yy + 8, xx : xx + 8][mask])
                cells += 1
    coverage = cells * 64 / max(1, valid.size)
    noise = None
    if samples and sum(x.size for x in samples) >= 64 and coverage >= 0.02:
        values = np.concatenate(samples)
        center = np.median(values)
        estimate = float(1.4826 * np.median(np.abs(values - center)))
        # A perfectly featureless synthetic frame carries no evidence that the
        # high-frequency estimator is working, so report it as unavailable.
        noise = estimate if estimate > 1e-6 else None
    # Correct gradient energy for the amount expected from independent noise.
    detail = float(max(0.0, np.mean(grad * grad) - (2.0 * (noise or 0.0) ** 2)))
    return {
        "luminance": float(np.percentile(norm, 50)),
        "luminance_p10": float(np.percentile(norm, 10)),
        "luminance_p90": float(np.percentile(norm, 90)),
        "darkness": darkness,
        "clipping": clipping,
        "detail": detail,
        "noise": noise,
        "noise_coverage": float(coverage),
    }


def _small(a: np.ndarray, size: tuple[int, int] = (64, 48)) -> np.ndarray:
    im = Image.fromarray(np.asarray(a, dtype=np.uint8), "L")
    return (
        np.asarray(im.resize(size, Image.Resampling.BILINEAR), dtype=np.float32) / 255
    )


def _structure(a: np.ndarray) -> np.ndarray:
    x = _small(a, (32, 24))
    x -= x.mean()
    sd = x.std()
    return (x / sd if sd > 0.01 else x).ravel()


def _distance(a: np.ndarray, b: np.ndarray) -> float:
    if a.std() < 0.01 or b.std() < 0.01:
        return 0.0
    return float(1 - np.corrcoef(a, b)[0, 1])


def _motion(
    images: list[np.ndarray], timestamps: list[float], exposure_us: float
) -> tuple[float | None, float]:
    """Return predicted smear pixels and fraction of textured frame pairs."""
    shifts: list[float] = []
    pairs = 0
    for before, after, t0, t1 in zip(images, images[1:], timestamps, timestamps[1:]):
        dt = (t1 - t0) / 1e9
        if dt <= 0:
            continue
        a, b = _small(before), _small(after)
        texture = float(
            np.mean(np.hypot(np.diff(a, axis=1)[:-1], np.diff(a, axis=0)[:, :-1]))
        )
        if texture < 0.012:
            continue
        best = (float("inf"), 0, 0)
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                y0, y1 = max(0, dy), min(a.shape[0], a.shape[0] + dy)
                x0, x1 = max(0, dx), min(a.shape[1], a.shape[1] + dx)
                err = float(
                    np.mean(
                        np.abs(
                            a[y0:y1, x0:x1] - b[y0 - dy : y1 - dy, x0 - dx : x1 - dx]
                        )
                    )
                )
                best = min(best, (err, dx, dy))
        if best[0] < 0.18:
            # Convert from 64-wide matching image to source pixels.
            velocity = math.hypot(best[1], best[2]) * (before.shape[1] / 64) / dt
            shifts.append(velocity * exposure_us / 1e6)
            pairs += 1
    coverage = pairs / max(1, len(images) - 1)
    return (float(median(shifts)) if shifts and coverage >= 0.5 else None, coverage)


def _requested_exposure(controls: dict[str, Any]) -> tuple[float, float] | None:
    e = controls.get("ExposureTime")
    ag = controls.get("AnalogueGain")
    if not all(_finite_number(x) and x > 0 for x in (e, ag)):
        return None
    return float(e), float(ag)


def _analyze_slot(
    run: Path, slot: dict[str, Any], roi: Any, y_range: str
) -> tuple[dict[str, Any] | None, list[str]]:
    reasons: list[str] = []
    arm = slot.get("arm")
    if arm not in ("A", "B"):
        reasons.append("invalid arm")
    frames = slot.get("frames")
    if not isinstance(frames, list) or len(frames) < 2:
        return None, reasons + ["slot needs at least two frames"]
    requested = slot.get("requested_controls", slot.get("requested", {}))
    if not isinstance(requested, dict):
        requested = {}
    target = _requested_exposure(requested)
    images, timestamps, exposures, durations, effective_exposures, metrics = (
        [],
        [],
        [],
        [],
        [],
        [],
    )
    for n, frame in enumerate(frames):
        if not isinstance(frame, dict):
            reasons.append(f"frame {n}: invalid record")
            continue
        md = frame.get("metadata")
        missing = [
            key
            for key in REQUIRED_METADATA
            if not isinstance(md, dict) or not _finite_number(md.get(key))
        ]
        if missing:
            reasons.append(f"frame {n}: missing metadata {','.join(missing)}")
            continue
        actual_arm = md.get("Arm", md.get("arm"))
        if actual_arm is not None and actual_arm != arm:
            reasons.append(f"frame {n}: actual arm mismatch")
        effective = md["ExposureTime"] * md["AnalogueGain"] * md["DigitalGain"]
        if effective <= 0 or target is None:
            reasons.append(f"frame {n}: invalid requested/effective exposure")
        elif abs(md["ExposureTime"] - target[0]) > max(50, target[0] * 0.03) or abs(
            md["AnalogueGain"] - target[1]
        ) > max(0.05, target[1] * 0.05):
            reasons.append(f"frame {n}: requested control mismatch")
        try:
            path = run / frame["file"]
            with Image.open(path) as im:
                a = np.asarray(im.convert("L"))
            if a.shape != (int(frame["height"]), int(frame["width"])):
                reasons.append(f"frame {n}: dimensions mismatch")
                continue
            box = _roi_box(roi, a.shape[1], a.shape[0])
            crop = a[box[1] : box[3], box[0] : box[2]]
            images.append(crop)
            timestamps.append(float(md["SensorTimestamp"]))
            exposures.append(float(md["ExposureTime"]))
            durations.append(float(md["FrameDuration"]))
            effective_exposures.append(float(effective))
            metrics.append(frame_metrics(crop, y_range))
        except (KeyError, OSError, ValueError, TypeError) as exc:
            reasons.append(f"frame {n}: unreadable ({exc})")
    if len(timestamps) != len(set(timestamps)):
        reasons.append("duplicate timestamps")
    if timestamps != sorted(timestamps):
        reasons.append("non-monotonic timestamps")
    if durations and max(durations) > min(durations) * 1.03:
        reasons.append("frame duration drift")
    if reasons or len(images) < 2:
        return None, sorted(set(reasons))
    smear, motion_coverage = _motion(images, timestamps, float(median(exposures)))
    result: dict[str, Any] = {
        "arm": arm,
        "index": slot.get("index"),
        "smear": smear,
        "motion_coverage": motion_coverage,
        "effective_exposure": float(median(effective_exposures)),
    }
    for key in (
        "luminance",
        "darkness",
        "clipping",
        "detail",
        "noise",
        "noise_coverage",
    ):
        vals = [m[key] for m in metrics if m[key] is not None]
        result[key] = float(median(vals)) if vals else None
    result["structure"] = np.mean([_structure(x) for x in images], axis=0)
    result["thumbnail"] = images[len(images) // 2]
    return result, []


def _arm_aggregate(slots: list[dict[str, Any]], arm: str) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for key in METRIC_DIRECTIONS:
        vals = [s[key] for s in slots if s["arm"] == arm and s.get(key) is not None]
        out[key] = float(median(vals)) if vals else None
    return out


def _delta(a: float | None, b: float | None, direction: int) -> float | None:
    if a is None or b is None or direction == 0:
        return None
    return float((b - a) * direction)


def analyze_run(
    run_dir: str | Path, output_dir: str | Path | None = None
) -> dict[str, Any]:
    run = Path(run_dir)
    report: dict[str, Any] = {
        "schema_version": 1,
        "run_id": None,
        "metric_directions": METRIC_DIRECTIONS,
        "blocks": [],
        "accepted_blocks": 0,
        "rejected_blocks": 0,
        "conclusion": "insufficient evidence",
        "reasons": [],
    }
    try:
        manifest = json.loads((run / "manifest.json").read_text())
    except (OSError, ValueError) as exc:
        report["reasons"] = [f"invalid manifest: {exc}"]
        if output_dir is not None:
            _write_outputs(report, Path(output_dir), [])
        return report
    report["run_id"] = manifest.get("id")
    if manifest.get("schema_version") != 1:
        report["reasons"].append("unsupported schema_version")
    roi = (
        manifest.get("settings", {}).get("roi")
        if isinstance(manifest.get("settings"), dict)
        else None
    )
    y_range = (
        manifest.get("pipeline", {}).get("y_range", "full")
        if isinstance(manifest.get("pipeline"), dict)
        else "full"
    )
    blocks = manifest.get("blocks")
    if not isinstance(blocks, list):
        blocks = []
        report["reasons"].append("blocks must be a list")
    thumbs: list[tuple[str, np.ndarray]] = []
    rows: list[dict[str, Any]] = []
    orders: set[str] = set()
    for position, block in enumerate(blocks):
        record: dict[str, Any] = {
            "index": (
                block.get("index", position) if isinstance(block, dict) else position
            ),
            "accepted": False,
            "reasons": [],
        }
        if not isinstance(block, dict):
            record["reasons"] = ["invalid block"]
            report["blocks"].append(record)
            continue
        order = block.get("order")
        record["order"] = order
        expected = list(order) if order in ("ABBA", "BAAB") else []
        slots_raw = block.get("slots")
        if not expected:
            record["reasons"].append("invalid order")
        if not isinstance(slots_raw, list) or len(slots_raw) != 4:
            record["reasons"].append("block must have four slots")
            slots_raw = slots_raw if isinstance(slots_raw, list) else []
        analyzed = []
        for i, slot in enumerate(slots_raw):
            result, reasons = (
                _analyze_slot(run, slot, roi, y_range)
                if isinstance(slot, dict)
                else (None, ["invalid slot"])
            )
            if (
                i < len(expected)
                and isinstance(slot, dict)
                and slot.get("arm") != expected[i]
            ):
                reasons.append("slot arm does not match order")
            if reasons:
                record["reasons"].extend(f"slot {i}: {x}" for x in reasons)
            elif result:
                analyzed.append(result)
        if len(analyzed) == 4:
            all_ts = [
                float(f["metadata"]["SensorTimestamp"])
                for s in slots_raw
                for f in s["frames"]
            ]
            if (max(all_ts) - min(all_ts)) / 1e9 > 3.0:
                record["reasons"].append("block span exceeds 3 seconds")
            # Adjacent hard cuts and drift between same-arm outer slots.
            adjacent = [
                _distance(analyzed[i]["structure"], analyzed[i + 1]["structure"])
                for i in range(3)
            ]
            if max(adjacent) > 0.55:
                record["reasons"].append("coarse scene cut")
            if _distance(analyzed[0]["structure"], analyzed[3]["structure"]) > 0.35:
                record["reasons"].append("same-arm outer slot drift")
            effective_a = median(
                s["effective_exposure"] for s in analyzed if s["arm"] == "A"
            )
            effective_b = median(
                s["effective_exposure"] for s in analyzed if s["arm"] == "B"
            )
            if abs(math.log2(effective_b / effective_a)) > 0.15:
                record["reasons"].append("effective exposure mismatch")
        if not record["reasons"] and len(analyzed) == 4:
            aa, bb = _arm_aggregate(analyzed, "A"), _arm_aggregate(analyzed, "B")
            deltas = {
                key: _delta(aa[key], bb[key], direction)
                for key, direction in METRIC_DIRECTIONS.items()
            }
            record.update({"accepted": True, "A": aa, "B": bb, "deltas": deltas})
            orders.add(order)
            rows.append({"block": record["index"], "order": order, **deltas})
            for i, s in enumerate(analyzed):
                thumbs.append(
                    (f"block {record['index']} slot {i} {s['arm']}", s["thumbnail"])
                )
        record.pop("structure", None)
        report["blocks"].append(record)
    accepted = [x for x in report["blocks"] if x["accepted"]]
    report["accepted_blocks"] = len(accepted)
    report["rejected_blocks"] = len(report["blocks"]) - len(accepted)
    aggregate = {}
    for key, direction in METRIC_DIRECTIONS.items():
        vals = [x["deltas"][key] for x in accepted if x["deltas"][key] is not None]
        aggregate[key] = float(median(vals)) if vals else None
    report["deltas"] = aggregate
    if len(accepted) < MIN_BLOCKS:
        report["reasons"].append(f"need at least {MIN_BLOCKS} accepted blocks")
    if orders != {"ABBA", "BAAB"}:
        report["reasons"].append("both ABBA and BAAB orders are required")
    if len(accepted) >= MIN_BLOCKS and orders == {"ABBA", "BAAB"}:
        # Materiality floors avoid pretending tiny numerical differences decide.
        quality = [
            aggregate[k]
            for k in ("detail", "noise", "smear")
            if aggregate[k] is not None
        ]
        exposure = [
            aggregate[k] for k in ("darkness", "clipping") if aggregate[k] is not None
        ]
        good = any(x > 0.002 for x in quality + exposure)
        bad = any(x < -0.002 for x in quality + exposure)
        report["conclusion"] = (
            "trade-off"
            if good and bad
            else (
                "B preferred"
                if good
                else ("A preferred" if bad else "insufficient evidence")
            )
        )
    if output_dir is not None:
        _write_outputs(report, Path(output_dir), thumbs, rows)
    return report


def _write_outputs(
    report: dict[str, Any],
    out: Path,
    thumbs: list[tuple[str, np.ndarray]],
    rows: list[dict[str, Any]] | None = None,
) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n"
    )
    fields = ["block", "order", *METRIC_DIRECTIONS]
    with (out / "pairs.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows or [])
    tile = (192, 132)
    if not thumbs:
        sheet = Image.new("RGB", tile, "white")
        ImageDraw.Draw(sheet).text((8, 8), "No accepted frames", fill="black")
    else:
        cols = min(4, len(thumbs))
        rows_n = math.ceil(len(thumbs) / cols)
        sheet = Image.new("RGB", (cols * tile[0], rows_n * tile[1]), "white")
        draw = ImageDraw.Draw(sheet)
        for i, (label, array) in enumerate(thumbs):
            im = Image.fromarray(array.astype(np.uint8), "L").convert("RGB")
            im = ImageOps.fit(im, (tile[0], tile[1] - 20))
            x, y = (i % cols) * tile[0], (i // cols) * tile[1]
            sheet.paste(im, (x, y))
            draw.text((x + 3, y + tile[1] - 18), label, fill="black")
    sheet.save(out / "contact-sheet.jpg", quality=88)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", metavar="RUN_DIR")
    parser.add_argument("--output", required=True, metavar="REPORT_DIR")
    args = parser.parse_args(argv)
    report = analyze_run(args.run_dir, args.output)
    print(
        f"{report['conclusion']}: {report['accepted_blocks']} accepted, {report['rejected_blocks']} rejected"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
