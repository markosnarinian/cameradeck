import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

from quality import _motion, analyze_run, frame_metrics


def road(seed=2):
    rng = np.random.default_rng(seed)
    a = np.full((64, 96), 105, dtype=np.float32)
    a[:, ::8] = 190
    a[::12, :] = 45
    a[20:44, 20:76] += rng.normal(0, 2, (24, 56))
    return np.clip(a, 0, 255).astype("uint8")


def test_metric_directions_and_ranges():
    sharp = road()
    blur = np.asarray(Image.fromarray(sharp).filter(ImageFilter.GaussianBlur(2)))
    assert frame_metrics(sharp)["detail"] > frame_metrics(blur)["detail"]
    rng = np.random.default_rng(4)
    noisy = np.clip(np.full((64, 96), 100) + rng.normal(0, 12, (64, 96)), 0, 255)
    clean = np.full((64, 96), 100)
    assert frame_metrics(noisy)["noise"] > 0
    assert frame_metrics(clean)["noise"] is None
    # Noise correction prevents random energy from beating real structure.
    assert frame_metrics(noisy)["detail"] < frame_metrics(sharp)["detail"]
    flat = np.full((64, 96), 20)
    assert frame_metrics(flat, "limited")["darkness"] == 1
    assert frame_metrics(flat, "full")["darkness"] == 0
    bright = np.full((64, 96), 238)
    assert frame_metrics(bright, "limited")["clipping"] == 1
    assert frame_metrics(bright, "full")["clipping"] == 0


def make_run(root: Path, mutation=None):
    root.mkdir()
    blocks = []
    stamp = 1_000_000_000
    for bi in range(6):
        order = "ABBA" if bi % 2 == 0 else "BAAB"
        slots = []
        for si, arm in enumerate(order):
            frames = []
            for fi in range(2):
                name = f"{bi}-{si}-{fi}.pgm"
                Image.fromarray(road()).save(root / name)
                ts = stamp + (si * 2 + fi) * 25_000_000
                frames.append(
                    {
                        "file": name,
                        "width": 96,
                        "height": 64,
                        "metadata": {
                            "ExposureTime": 1000,
                            "AnalogueGain": 2.0,
                            "DigitalGain": 1.0,
                            "SensorTimestamp": ts,
                            "FrameDuration": 25_000_000,
                            "Arm": arm,
                        },
                    }
                )
            slots.append(
                {
                    "index": si,
                    "arm": arm,
                    "requested_controls": {
                        "ExposureTime": 1000,
                        "AnalogueGain": 2.0,
                        "DigitalGain": 1.0,
                    },
                    "settling": {},
                    "frames": frames,
                }
            )
        blocks.append({"index": bi, "order": order, "slots": slots})
        stamp += 200_000_000
    manifest = {
        "id": "test",
        "schema_version": 1,
        "state": "complete",
        "settings": {"roi": [0, 0, 1, 1]},
        "pipeline": {"y_range": "full"},
        "schedule": {},
        "blocks": blocks,
    }
    if mutation:
        mutation(manifest, root)
    (root / "manifest.json").write_text(json.dumps(manifest))


def test_outputs_and_balanced_orders(tmp_path):
    run, out = tmp_path / "run", tmp_path / "out"
    make_run(run)
    report = analyze_run(run, out)
    assert report["accepted_blocks"] == 6
    assert report["conclusion"] == "insufficient evidence"
    assert {p.name for p in out.iterdir()} == {
        "report.json",
        "pairs.csv",
        "contact-sheet.jpg",
    }


def test_duplicate_timestamp_rejects_without_crash(tmp_path):
    def mutate(m, _):
        f = m["blocks"][0]["slots"][0]["frames"]
        f[1]["metadata"]["SensorTimestamp"] = f[0]["metadata"]["SensorTimestamp"]

    make_run(tmp_path / "run", mutate)
    r = analyze_run(tmp_path / "run")
    assert r["rejected_blocks"] == 1
    assert "duplicate timestamps" in " ".join(r["blocks"][0]["reasons"])


def test_effective_exposure_mismatch_rejects(tmp_path):
    def mutate(m, _):
        for frame in m["blocks"][0]["slots"][0]["frames"]:
            frame["metadata"]["DigitalGain"] = 1.3

    make_run(tmp_path / "run", mutate)
    r = analyze_run(tmp_path / "run")
    assert "effective exposure mismatch" in " ".join(r["blocks"][0]["reasons"])


def test_arm_swap_reverses_detail_preference(tmp_path):
    def blur_arm(arm):
        def mutate(manifest, root):
            for block in manifest["blocks"]:
                for slot in block["slots"]:
                    if slot["arm"] == arm:
                        for frame in slot["frames"]:
                            path = root / frame["file"]
                            Image.open(path).filter(ImageFilter.GaussianBlur(2)).save(
                                path
                            )

        return mutate

    make_run(tmp_path / "a-sharp", blur_arm("B"))
    make_run(tmp_path / "b-sharp", blur_arm("A"))
    a_sharp = analyze_run(tmp_path / "a-sharp")
    b_sharp = analyze_run(tmp_path / "b-sharp")
    assert a_sharp["deltas"]["detail"] < 0 < b_sharp["deltas"]["detail"]
    assert a_sharp["conclusion"] == "A preferred"
    assert b_sharp["conclusion"] == "B preferred"


def test_controlled_scene_trend_is_not_mistaken_for_an_arm_effect(tmp_path):
    def mutate(manifest, root):
        radii = (0, 1, 2, 3)
        for block in manifest["blocks"]:
            for slot, radius in zip(block["slots"], radii):
                for frame in slot["frames"]:
                    path = root / frame["file"]
                    Image.open(path).filter(ImageFilter.GaussianBlur(radius)).save(path)

    make_run(tmp_path / "run", mutate)
    report = analyze_run(tmp_path / "run")
    assert report["accepted_blocks"] == 0
    assert all(
        "same-arm outer slot drift" in block["reasons"] for block in report["blocks"]
    )
    assert report["conclusion"] == "insufficient evidence"


def test_motion_estimate_tracks_translation_and_rejects_missing_texture():
    textured = road()[:48, :64]
    translated = [np.roll(textured, 2 * i, axis=1) for i in range(3)]
    smear, coverage = _motion(
        translated, [0, 50_000_000, 100_000_000], exposure_us=10_000
    )
    assert coverage == 1
    assert 0.3 <= smear <= 0.5

    flat = [np.full((48, 64), 100, dtype=np.uint8) for _ in range(3)]
    smear, coverage = _motion(flat, [0, 50_000_000, 100_000_000], 10_000)
    assert smear is None and coverage == 0


def test_scene_cut_rejects(tmp_path):
    def mutate(m, root):
        for frame in m["blocks"][0]["slots"][3]["frames"]:
            rng = np.random.default_rng(99)
            Image.fromarray(rng.integers(0, 256, (64, 96), dtype=np.uint8)).save(
                root / frame["file"]
            )

    make_run(tmp_path / "run", mutate)
    r = analyze_run(tmp_path / "run")
    assert "scene" in " ".join(r["blocks"][0]["reasons"])


def test_incomplete_manifest_is_reported(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "manifest.json").write_text('{"schema_version": 1, "blocks": [null]}')
    r = analyze_run(run)
    assert r["rejected_blocks"] == 1
    assert r["conclusion"] == "insufficient evidence"
