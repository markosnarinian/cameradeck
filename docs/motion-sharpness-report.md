# Motion blur and sharpness in CameraDeck: review and plan

*Status: review of commit `0613875` plus a proposal. No code has changed yet. Updated 2026-09-24 with your answers (Section 8) and the video-vs-stills analysis (Section 9).*

## 1. Short answer

**CameraDeck has no mode that optimizes camera settings for sharpness.** Nothing in
the code measures motion or blur and then picks shutter and gain for you. What it
does have is a set of building blocks for motion blur, and none of them closes the
loop:

| Feature | What it does | Closes the loop? |
| --- | --- | --- |
| Default exposure bias (`AeExposureMode = Short`) | Libcamera's own AE, tilted towards shorter exposures | No. There is no numeric shutter cap and no motion input |
| Exposure panel | Manual shutter and gain. Setting only the shutter gives shutter‑priority AE on current libcamera | No. You pick the values by hand |
| Sharpness grid | 3×3 Laplacian-variance scores on a small preview | No. It is display-only and too coarse to see a few pixels of blur (see F7) |
| Stationary still sweep (`test`) | An ordered shutter × gain grid of full-sensor stills | No. It has no motion and no scoring, and never picks a winner |
| Road A/B experiment (`road`) + `quality.py` | Randomized ABBA/BAAB comparison of two equal-exposure shutter/gain arms while driving, analyzed offline | No. It is an **evaluation** tool. It never applies a result, and the analyzer has correctness problems (Section 4) |

You were right that there is a mode for randomized experiments: the Road A/B
experiment. It answers "is setting A or setting B better?", not "what setting should
I use right now?". Because the optimizer you describe would rest on those same
measurements, this report covers both halves:

- **Part A**: how the current machinery works, and what is wrong with it.
- **Part B**: a plan for a motion-priority exposure mode.

---

## Part A: what exists and how it works

## 2. Exposure behaviour in normal operation

**Defaults on every camera open.** `CameraDeck._apply_motion_defaults`
(`camera.py:218`) runs on every `open()`, including the one after each capture
sequence ends. It turns on `AeEnable`/`AwbEnable`, sets `ExposureTimeMode` and
`AnalogueGainMode` to Auto, and selects `AeExposureMode = Short` when the camera
advertises it. `Short` is an exposure profile in the sensor's tuning file (the
`rpi.agc` → `exposure_modes` table). The stock Raspberry Pi tables let the shutter
climb to tens of milliseconds before gain runs out. The README states this: "it
does not impose a numerical shutter cap". To see what applies to your camera, check
the installed tuning file on the Pi (typically
`/usr/share/libcamera/ipa/rpi/vc4/<sensor>.json`).

**Manual controls.** In `static/app.js:528`, changing *Shutter · µs* sends
`ExposureTime` together with `ExposureTimeMode = Manual` only. `AnalogueGainMode`
stays Auto. On current libcamera, that is **shutter-priority AE**: you fix the
shutter and libcamera's AGC chooses gain every frame. On older stacks the UI sends
`AeEnable = false` instead, so both values become manual. A person can already do a
crude "cap the shutter" by hand; nothing automates it.

**Output encoding.** Video is H.264 at a fixed 10 Mb/s (`camera.py:859`). Stills are
full-sensor JPEGs taken through a mode switch with `controls=self.applied` and a
2-frame settle (`camera.py:823`).

### Why this matters in twilight

For a forward-facing camera, the image of the road moves at roughly
`f · h · v / D²` px/s, where `f` is the focal length in pixels, `h` is camera
height, `v` is vehicle speed and `D` is the distance to the road point. Example
assumptions: 1920 px wide, 90° horizontal field of view (f ≈ 960 px), camera 2.5 m
up, 50 km/h.

| Road point ahead | Image speed | 1 ms | 2 ms | 4 ms | 8 ms | 20 ms |
| --- | --- | --- | --- | --- | --- | --- |
| 5 m | ≈1330 px/s | 1.3 px | 2.7 px | 5.3 px | 10.7 px | 26.7 px |
| 10 m | ≈330 px/s | 0.3 | 0.7 | 1.3 | 2.7 | 6.7 |
| 20 m | ≈80 px/s | 0.1 | 0.2 | 0.3 | 0.7 | 1.7 |

Blur scales linearly with speed, and it roughly doubles in full-sensor stills
(3864 px wide instead of 1920). Objects beside the road, and the lower corners of
the frame, move faster than points on the centre line.

In twilight, AE `Short` can reach exposures in the right-hand columns. So the
current default probably blurs the near road by several pixels to tens of pixels,
which is exactly the region a road-surface camera cares about. Substitute your real
lens field of view and mounting height before relying on these numbers.

## 3. The randomized Road A/B experiment

### 3.1 Capture (`camera.py`)

1. **Validation** (`start_sequence`, `camera.py:352`):
   - The video rate must be at least 10 fps.
   - Arm A is a reference shutter and gain. Arm B is a comparison shutter, and its
     gain is set to `A_shutter × A_gain / B_shutter` so both arms have the same
     nominal exposure.
   - Both shutters must fit inside the frame period.
   - The experiment has 4–12 blocks, an even number.
   - The ROI is a normalized `[x, y, w, h]`.
   - A seed shuffles an equal number of `ABBA` and `BAAB` blocks.
2. **Locks** (`_run_road`, `camera.py:668`). AWB is locked to the current
   `ColourGains`, and on AF cameras focus is locked to the current `LensPosition`.
   Both arms are fully manual (`_manual_exposure`, `camera.py:281`).
3. **Frame tap.** `_metadata` (`camera.py:236`) is the Picamera2 post-callback.
   While an experiment is armed, it keeps the newest request. An older request that
   was never consumed is released and counted as a *drop*.
4. **Slots** (`_road_slot`, `camera.py:568`). After an arm change, frames are
   discarded until the measured `ExposureTime` is within max(50 µs, 3 %) of target
   and `AnalogueGain` is within max(0.05, 5 %). That match must hold for at least 3
   consecutive frames and at least 200 ms. The next 3 frames are then kept (the
   third settled frame counts as the first). A slot that doesn't settle within 3 s
   fails the run.
5. **Storage.** The Y plane of the 1080p/720p main stream is saved as a lossless
   PGM. Frames are held in memory and written per block, and after each block the
   manifest (`experiments/<id>/manifest.json`) is replaced atomically. It holds
   settings, schedule, pipeline info (including Y range), and per-frame
   metadata. Controls are restored when the sequence ends.
6. **API.** `app.py:457` lists, downloads (ZIP) and deletes experiments. The UI
   provides a road-area (ROI) drawing tool on the live preview.

### 3.2 Analysis (`quality.py`, offline: `python -m quality RUN --output OUT`)

**Per frame**, on the ROI crop (`frame_metrics`, `quality.py:83`):

- `luminance`: median, plus p10 and p90
- `darkness`: fraction of pixels in the bottom 4 %
- `clipping`: fraction of pixels in the top 2 %
- `noise`: robust MAD of a 4-neighbour high-pass residual, taken only from "flat"
  8×8 cells
- `detail`: mean squared gradient of the 3×3-smoothed image, minus `2·noise²`

**Per slot:**

- Frames are rejected when metadata is missing, the requested controls don't match,
  timestamps are duplicated or out of order, or frame duration drifts.
- `smear` (`_motion`, `quality.py:159`) is *predicted*, not measured. It
  block-matches consecutive frames downsampled to 64×48 with a ±4-cell search,
  turns the best shift into px/s, and multiplies by the exposure time.

**Per block:** the block is rejected when it spans more than 3 s, when adjacent
slots look like a scene cut, when the two same-arm outer slots have drifted apart,
or when the arms' effective exposure (shutter × analogue × digital gain) differs by
more than 0.15 EV. Otherwise the per-arm medians give per-metric deltas, signed so
that a positive value means B is better.

**Verdict** (`quality.py:443`): this needs at least 6 accepted blocks with both
orders present. The per-metric deltas are medians across blocks. Any
detail/noise/smear/darkness/clipping delta above `+0.002` counts as "good" and any
below `-0.002` counts as "bad", which gives `B preferred`, `A preferred`,
`trade-off` or `insufficient evidence`.

### 3.3 What is good about the design

- ABBA/BAAB counterbalancing with a recorded seed, which cancels linear drift and
  order effects.
- Frames are accepted only after the camera's *measured* metadata matches the
  request, and AWB and focus are locked, so the arms differ only in shutter and
  gain.
- Lossless Y-plane evidence and an atomic manifest. Runs can be reproduced and
  analyzed again later.
- Conservative framing: the tool never auto-applies a result, and it states its
  limitations in the README and the UI.

## 4. Review findings

I checked these by reading the code and running numerical experiments against
`quality.py` (method in Appendix A). The synthetic road is 1/f "asphalt" texture
with a lane marking, scrolled vertically as in forward driving. Box motion blur of
length speed × shutter and Gaussian noise are added. Each run is 8 blocks through
`analyze_run`.

| # | Severity | Finding |
| --- | --- | --- |
| F1 | **High** | The **noise metric responds to real detail**: a sharper arm reads as "noisier" |
| F2 | **High** | The **detail metric's noise correction over-subtracts about 6×**. Together with F1, detail comes out lower for the sharper arm |
| F3 | **High** | **One `0.002` materiality floor is applied to metrics in different units.** Detail can effectively never influence the verdict |
| F4 | **High** | **Predicted smear is counted as evidence**, so the verdict mostly restates the shutter ratio |
| F5 | Medium | `_motion` converts vertical shifts with the horizontal scale. On the default ROI it **over-reports vertical smear by about 2.1×** |
| F6 | Medium | The motion search range is too small for slow sensors or near-road ROIs, and the motion model is a single global shift |
| F7 | Medium | The sharpness grid, and the `focus` scores stored with stills, are computed at 480 px. They cannot see 1–4 px of blur at 1080p or 4K |
| F8 | Medium | The analysis ignores the H.264/JPEG encode, which is where extra noise costs the most |
| F9 | Low–Med | The verdict has no statistics: no confidence interval, no sign test, no effect size |
| F10 | Low–Med | The stationary sweep can't measure motion blur, runs in a fixed order, and varies brightness 32× across the grid |

**F1: the noise metric responds to detail** (`quality.py:101`). "Flat" cells are
those whose smoothed gradient is below 0.05. Fine, low-contrast asphalt texture
passes that test, so the residual includes texture, and motion blur removes exactly
that texture. Results:

| Scenario (B = half shutter, twice the gain) | Reported noise delta (positive = B better) | Truth |
| --- | --- | --- |
| **No sensor noise at all**, B sharper | −4.4e-3 ("B noisier") | Neither arm has noise |
| Same noise in both arms, B sharper | −4.4e-3 | Equal noise |
| Static scene, **B has 2× the noise** | −1.45e-3 (below the floor, so ignored) | B clearly noisier |

The metric reacts more to sharpness than to noise.

**F2: detail over-correction** (`quality.py:126`). For white noise, the gradient
operator used here yields about 0.47·σ² of energy. The code subtracts
`2·noise_est²` ≈ 2.7·σ², which is about 5.8× too much (measured). The result is
clamped at 0, so the error doesn't show as negative values. It systematically
penalizes the arm that reports more "noise", and because of F1 that is often the
sharper arm. In every moving-scene run, the arm with **half the true blur** got a
**lower** detail score (e.g. −1.2e-4 even with zero sensor noise).

**F3: unit-blind floor** (`quality.py:453`). One `0.002` threshold is applied to:

- `detail` (typical values about 5e-4 on realistic texture, deltas about 1e-4)
- `noise` (about 1e-2)
- `smear` (pixels, deltas of 1–4)
- `darkness`/`clipping` (pixel fractions)

Detail is the only metric that measures the thing we want, and on realistic texture
it never crosses the floor. The unit test that shows a detail-driven verdict
(`tests/test_quality.py:129`) passes only because it uses high-contrast stripes
with a 2 px Gaussian blur.

**F4: circular smear.** Smear is measured speed × *requested* shutter, and both arms
see the same speed, so smear_B / smear_A ≈ shutter_B / shutter_A by construction.
Whenever motion is detected, the shorter-shutter arm "wins" on smear by several
pixels, far above the floor. Results:

- All of the synthetic moving scenarios returned **`trade-off`**: smear won, and F1
  made noise lose.
- Swapping which arm has the shorter shutter mirrored every delta exactly.

The verdict therefore reflects the design of the experiment, not the images.

**F5: vertical-scale bug** (`quality.py:190`). The code is
`velocity = hypot(dx, dy) * (width / 64) / dt`. The ROI is resized to 64×48
regardless of its aspect ratio, so `dy` should use `height / 48`. With the default
ROI `[0.15, 0.45, 0.7, 0.45]` at 1080p (1344×486 px), results:

- 20 px/frame of true vertical motion is reported as **42 px**.
- Horizontal motion is reported correctly (21 px).

Forward driving makes road motion mostly vertical, so the absolute smear figures in
reports are about 2× too high.

**F6: motion estimator limits.** On a 64×48 grid, ±4 cells covers only about
±40 px vertically at 1080p with the default ROI. At the B0569's advertised
15.75 fps, that is roughly 630 px/s, which the near road exceeds at moderate
speeds (see the Section 2 table). Matches then fail and coverage falls, or they
alias onto a wrong shift.

A single global translation is also a poor model for forward motion. The flow field
radiates outward and grows about 1/D² towards the bottom of the frame, so the
median shift is a crude average over very different speeds.

**F7: sharpness grid resolution.**

- The live grid (`camera.py:250`) scores the 640×360, q70 MJPEG preview after
  shrinking it to 480 px.
- Still `focus` scores (`camera.py:786`) come from a 480 px thumbnail.

A 4 px blur at 1080p is under 1 px at that scale, and JPEG artifacts and noise
(the grid has no noise correction) dominate. These scores are fine for "is the lens
roughly focused". They cannot guide a motion-blur optimizer.

**F8: the output encoder is ignored.** The experiment scores lossless Y frames, but
what actually gets delivered is 10 Mb/s H.264 or a JPEG. At a fixed bitrate, a
noisier (higher-gain) arm spends bits on noise, and the encoder then removes real
texture. So the lossless comparison is biased towards short shutter plus high gain,
relative to what ends up recorded.

**F9: no statistics.** The verdict is the sign of the median block delta once it
passes the floor. It reports no confidence interval, no count of blocks favouring
each arm, and no effect size. Each block spans roughly 1–2 s of driving (15–30 m at
50 km/h), so the arms within a block see different road patches, and texture varies
a lot from patch to patch. Eight blocks is a small sample, and the verdict wording
("preferred") overstates it.

**F10: stationary sweep limits.**

- The scene is stationary, so the sweep cannot measure motion blur at all.
- The shutter × gain grid runs in a fixed order, so slow light drift (twilight!) is
  confounded with shutter.
- The default grid spans 1000 µs × 2 to 8000 µs × 8, a 32× brightness range. Detail
  comparisons across it mostly measure brightness.

The sweep is still useful as a **noise-calibration** tool: a static scene, noise
against gain.

**Smaller notes:**

- Darkness and clipping use the same 0.002 floor (0.2 % of pixels). At equal
  exposure these deltas should be about 0, but noise around clipped lane markings or
  headlights can push them across the floor and flip the verdict.
- `road_drops` is recorded in the manifest but never used.
- Analysis runs only on a workstation. It is NumPy/Pillow-only and could run on the
  Pi after a run.
- **Tests:** in this container, 79 of 80 tests pass. `test_interrupted_clip_recovery`
  fails only because `ffmpeg` is not installed here.

---

## Part B: plan for a motion-priority exposure mode

## 5. Goal and principle

**Goal:** get the most usable detail in a chosen region (by default the road
surface), in what is actually recorded (H.264 video and/or periodic full-sensor
stills), as speed and light change.

**The decision is essentially one-dimensional:**

- Motion blur in pixels = image speed (px/s) × shutter time.
- At a given brightness, photon noise depends on the shutter time. Gain restores
  brightness. At a fixed shutter, more analogue gain is usually better than
  underexposing and brightening later, up to clipping.
- So the controller picks a **shutter from a blur budget in pixels**, and AE picks
  gain.
- The best budget (for example 1.0 px or 2.0 px) is an empirical constant, found
  with experiments (Phase 4). The controller should not search for it live.

This splits the work into "control online, calibrate offline". An online
search-and-score optimizer ("try settings and keep the sharpest") would be fragile
on a moving truck: every frame shows a different piece of road, which is exactly
the confound Part A shows the metrics can't handle yet.

## 6. Proposed phases

### Phase 0: fix the measurement tools (prerequisite; small and testable offline)

1. **Fix F5** (use `height/48` for `dy`) and add a regression test with a
   non-4:3 ROI.
2. **Replace the noise estimate.** Two options:
   - (a) Calibrate a noise-versus-gain table from stationary captures (lens cap,
     or a flat grey card, at each gain). The sweep mode can produce these.
   - (b) Estimate temporal noise from consecutive frames after motion
     compensation.

   Either way, stop deriving noise from "flat" cells in road texture.
3. **Fix the detail metric.** Calibrate the noise correction to the actual filter
   (about 0.47·σ² for white noise, and measured for real ISP-correlated noise), or
   switch to a band-limited or directional measure. Motion blur is anisotropic
   while noise is isotropic, so across-motion versus along-motion gradient energy
   cancels much of the noise.
4. **Take predicted smear out of the verdict** (F4). Report it as context: the
   blur in px per arm, and the measured image speed.
5. **Make the verdict unit-aware and statistical** (F3, F9):
   - Per-metric floors in natural units: log-ratio for detail and noise,
     percentage points for clipping.
   - A per-block paired sign test or bootstrap CI.
   - Report "k of n blocks favour B".
6. **Replace the synthetic test fixtures** with 1/f texture, realistic noise, and
   vertical scroll, like Appendix A, so tests cover realistic magnitudes and not
   just the high-contrast stripes used today.
7. *(Optional)* **Score after the encoder** (F8): round-trip the stored Y frames
   through H.264 at the configured bitrate (PyAV is already a dependency) or JPEG,
   and compute the metrics on the result as well.
8. *(Optional)* **Run the analyzer on the Pi** after a run and show the report in
   the UI, instead of requiring a workstation.

### Phase 1: "Shutter cap" exposure preset (quick win; no motion sensing)

- New Exposure-panel option: **Motion priority · max shutter N µs**, with a
  default chosen from the Section 2 table (e.g. 2000 µs), plus a max-gain setting.
- **Mechanism A (preferred), app-level loop at about 5 Hz:**
  - Keep `ExposureTimeMode = Manual` and `AnalogueGainMode = Auto`, so libcamera
    adjusts gain every frame (shutter priority).
  - Read `ExposureTime`/`AnalogueGain`/`DigitalGain` from metadata. When AGC sits
    at minimum gain and the ROI is still above target (bright daylight), shorten
    the shutter below the cap. Otherwise hold the shutter at the cap.
  - This gives "short as necessary, never longer than the cap" and stays
    responsive to changing light.
- **Mechanism B (alternative), a custom tuning-file exposure mode:**
  - Load the sensor's tuning with `Picamera2.load_tuning_file()`, edit the
    `rpi.agc` `exposure_modes` table (on newer libcamera it is under `channels[0]`)
    so the shutter stops at N µs and gain rises to the maximum, then open
    `Picamera2(tuning=…)`.
  - AE is then native and needs no app loop. The cap is static, though: changing
    it means reopening the camera.
- **Policy when it is too dark** (the gain is at its maximum): a user choice
  between *keep sharpness* (accept a darker, noisier image), *keep brightness*
  (let the shutter rise to a hard ceiling), and *balanced* (allow up to 2× the
  cap).
- **Applies to** Video and Periodic stills. Stills already inherit
  `self.applied` through `capture()`, but the still-mode settle (2 frames) must be
  checked with gain on Auto.
- **Logging:** write the chosen shutter, gain and policy state into still sidecars,
  and into a per-video CSV (about 1 Hz) alongside the MP4.

### Phase 2: motion-adaptive budget (the actual optimizer)

- **Motion sensing:** tap the **lores** Y plane (640×360, already produced by the
  ISP) at 5–10 Hz from the post-callback.
  - Estimate per-tile motion with a small pyramid block match or phase correlation
    over a 4×3 tile grid inside the ROI.
  - Take the **p90 tile speed**, not a global shift, and scale it to the pixel grid
    of the target output (1920 px for video; the full sensor width for stills).
  - The CPU cost is small on a Pi 4 at this size, but it has to share the CPU with
    the two MJPEG preview threads.
- **Planner:**
  - `t = clamp(budget_px / speed_p90, t_min, min(frame_period, t_ceiling))`.
  - Use EMA smoothing, change at most about ⅓ EV per second, and quantize to steps
    to avoid visible pumping.
  - When the vehicle stops, speed falls towards 0 and the shutter relaxes to the
    policy ceiling. Noise drops while stationary.
- **Hand-off:** the planner's `t` becomes the cap for the Phase 1 loop.
- **Fallbacks:**
  - Low texture, night, or a failed match → the last good value, which decays
    towards the Phase 1 static cap.
  - (Future) GPS or OBD speed as an optional input.
- **UI:**
  - Blur budget in px (default 1.5), with min and max shutter.
  - The "too dark" policy.
  - Reuse the Road ROI drawing tool.
  - A live readout of image speed, predicted blur, shutter and gain.

### Phase 3: other detail levers (each one A/B-tested before becoming a default)

- ISP `Sharpness` and `NoiseReductionMode`: denoise strength trades against fine
  texture.
- H.264 bitrate: 10 Mb/s is conservative for noisy twilight 1080p. Raise it within
  the Pi 4 encoder's limits and check storage impact.
- On Module 3 (AF): lock `LensPosition` near hyperfocal while driving instead of
  continuous AF, which can hunt on moving scenes.
- Flicker: shutters of a few ms can band or flicker under LED street lights and
  signs at twilight. Evaluate `AeFlickerMode` where it is advertised.
- Rolling shutter: short exposures do not remove skew. This is a known limit to
  document, not something to optimize.

### Phase 4: calibrate the budget with experiments

- Extend the Road A/B experiment so each block records the **measured image speed**
  (the Phase 2 estimator). Pooled runs then give detail as a function of *blur px*
  and gain, instead of a single A-versus-B verdict. That curve sets the default
  `budget_px`.
- Optionally allow 3–4 arms (a Williams or Latin-square order) and arms defined
  by budget rather than fixed shutter.
- Keep "never auto-apply" for *experiment results*. The online controller applies
  only the *calibrated* budget that the operator has chosen.

## 7. Where it goes in the code

- `camera.py`:
  - A `MotionPriority` controller object owned by `CameraDeck`, with a worker
    thread like `_analyze`.
  - It must go through `self.lock` and `set_controls`, and be allowed past
    `_sequence_guard` like the sequence worker. Alternatively, suspend it
    explicitly during sequences.
  - Reuse the lores tap pattern from `_metadata`/`road_condition`, but without
    holding requests: copy a small Y crop and release.
  - `open()` has to decide whether `_apply_motion_defaults` or the
    motion-priority preset is the default after reopening.
- `app.py`: `GET/POST /api/exposure-policy` (mode, budget, caps, policy, ROI), and
  its live state in `/api/status`.
- `static/`: Exposure-panel preset, ROI reuse, telemetry fields.
- `quality.py`: the Phase 0 fixes, plus the shared motion estimator (one
  implementation used both online and offline).
- **Tests:**
  - Controller unit tests with fake metadata and frame sequences: bright, dark,
    stopped and fast; hysteresis; frame-period clamp.
  - Analyzer tests with realistic fixtures.
- **Docs:** README sections, and `UPDATE.md` if Mechanism B's tuning handling
  changes deployment.

## 8. Decisions

**Answered (2026-09-24):**

| Question | Answer | What it means for the plan |
| --- | --- | --- |
| Primary output | Frames from video, or stills every ~0.5 s | Section 9 compares the two. **Recommendation: frames tapped from the video stream** |
| What must be sharp | Near road surface, **3–5 m** ahead | Fixed road-band ROI. This is where image motion is fastest, so blur is the limiting factor |
| Consumer | An **object-detection model on the edge** | Set the blur budget in *model-input* pixels. Collect frames the same way the deployed model will receive them |
| Speed | **5–40 km/h**, no GPS/OBD | The Phase 2 optical-flow estimator becomes the speed signal. It drives both the shutter and the sampling cadence |
| Phase 0 first? | **Yes** | Fix the analyzer before trusting any verdict |
| Phase 1 mechanism | Not answered | I still recommend the app loop |

**Still open. These don't block Phase 0, but Phases 1–2 need them:**

1. **Model input:** does the detector take the whole frame resized (e.g. 640 px
   wide), or crops/tiles of the road band at native resolution? This sets the blur
   budget in camera pixels (up to 3× apart) and decides whether full-sensor
   resolution can ever help.
2. **Mounting geometry:** camera height, downward tilt, and the lens's horizontal
   field of view. A level camera at about 2.5 m cannot see 3 m ahead; it has to be
   tilted down about 30–40°. Replace the example numbers below with the real ones.
3. **Where the model runs:** on the Pi 4 itself, or on an accelerator or separate
   device? This sets the CPU budget left for frame selection and encoding.

## 9. Video-stream frames vs. stills for this use case

**Recommendation: take frames from the video stream, as uncompressed YUV tapped
from the ISP (the way the Road A/B experiment already does it).** Do not decode
them from the H.264 file, and do not use the periodic full-sensor still path.

### Numbers for the 3–5 m band

These assume 2.5 m camera height, 90° horizontal field of view and the same field
of view in both modes; point-at-image-centre approximation.

| | 1080p video frame | Full-sensor still (3864 px) |
| --- | --- | --- |
| Ground size of one pixel, along the road, at 3 m / 5 m | 6.4 / 13 mm | 3.2 / 6.5 mm |
| Blur at 40 km/h, 1 ms shutter, at 3 m | 1.75 px | 3.5 px |
| Blur at 20 km/h, 1 ms shutter, at 3 m | 0.9 px | 1.8 px |

In 1 ms the road moves the same distance on the ground (11 mm at 40 km/h)
whichever path you use. Full resolution adds real detail only when that movement is
smaller than one full-res pixel (3.2 mm at 3 m). That requires a shutter no longer
than:

| Speed | Max shutter for full-res to help |
| --- | --- |
| 40 km/h | 0.28 ms |
| 20 km/h | 0.57 ms |
| 10 km/h | 1.1 ms |
| 5 km/h | 2.3 ms |

In twilight, shutters that short need very high gain. Check the shutter AE actually
picks in your twilight telemetry. If it is 1 ms or more, **blur, not pixel count,
limits detail above roughly 10–15 km/h**, and 1080p already samples finer than the
blur.

### Why video-stream frames win here

1. **Coverage.** The 3–5 m band is only 2 m deep. For every piece of road to appear
   in at least one frame, frames must come at least this often:

   | Speed | Minimum rate |
   | --- | --- |
   | 5 km/h | 0.7 Hz |
   | 14 km/h | 2 Hz |
   | 20 km/h | 2.8 Hz |
   | 40 km/h | 5.6 Hz |

   **At 2 Hz, the band has gaps above about 14 km/h**, and at 40 km/h about two
   thirds of the road is never imaged. The sensor delivers 15.75 fps, so each road
   point appears in about 3 frames at 40 km/h and about 23 at 5 km/h. From that you
   can choose a cadence based on distance travelled (below).
2. **Best-of-window selection.** Truck vibration makes blur vary from frame to
   frame. With about 8 candidate frames per 0.5 s window, CameraDeck can keep the
   sharpest one. A still gets one attempt. (The selection metric must be
   noise-robust; this is Phase 0 work.)
3. **The current still path can't do 0.5 s.**
   - Periodic stills are validated at a **1 s minimum interval** (`camera.py:323`,
     `static/index.html:48`).
   - Every photo stops the camera, switches to a full-sensor configuration,
     captures after 2 frames, and switches back.
   - Then CameraDeck decodes the full-res JPEG again to make the sharpness scores,
     thumbnail and preview.
   - A slow capture stretches the interval.

   You can measure the real cadence from the `created` timestamps in the sidecars
   of an existing periodic-stills run. The video stream never stops, so frame
   timing is exact (from `SensorTimestamp`), and AE and the motion-priority
   controller run without interruption.
4. **The same sensor readout, with less noise per pixel.** The B0569/IMX415
   advertises a single 3864×2192 mode, so video and stills read the sensor
   identically, with the same photons and the same rolling-shutter skew.
   - The ISP's 2× downscale to 1080p averages neighbouring pixels, which gives up
     to about 2× lower per-pixel noise.
   - This matters at the high gains that short shutters need.
   - When blur limits detail, that is a free gain.
   - On a Module 3 the case is stronger still: its 1080p video mode is binned, with
     faster readout and better signal-to-noise ratio than its full-res mode.
5. **It matches deployment.** An on-truck detector will consume the live stream.
   Training data taken the same way avoids a mismatch between how training and
   deployment images were produced (different processing, denoise mode, colour
   range, JPEG). Picamera2's still and video configurations use different default
   noise-reduction modes and colour spaces; verify on the Pi.
6. **Cost.** Compared with full-sensor JPEGs, 1080p frames (or just the road band)
   mean about 4× less data to write, and less CPU to encode.

**Avoid the H.264 file as a frame source.** At 10 Mb/s and 15.75 fps, each frame
gets about 80 KB on average. Fast-moving fine texture, plus noise from high gain,
is the worst case for the encoder, and it wipes out exactly the detail the
detector needs (finding F8). Recording can continue alongside the frame tap for
human review.

### When stills or full resolution would win

- **Low speed in good light.** If the detector uses the road band at native
  resolution *and* the shutter can go below the thresholds above (for example
  daylight at 5–10 km/h), full resolution resolves about 2× finer detail.
- **Hybrid option.** If that case matters, don't use mode-switched stills. Run a
  continuous full-sensor YUV stream and tap it in the same way. That keeps coverage,
  selection and exact timing.
  - H.264 recording would have to stop, because the Pi 4 encoder is limited to
    1080p.
  - Preview would need rework: the Pi 4 ISP has only main and lores outputs.
  - Memory, CPU and storage cost is about 4×.
  - Treat this as a later option to test, not the default.

### What this adds to the plan: a "Road frames" capture mode (after Phase 0, alongside Phases 1–2)

- **Frame tap:** the main 1080p YUV stream, reusing the Road A/B tap
  (`_metadata`/`_road_next`) without holding requests.
  - Score each frame's road band on a cheap downsampled crop.
  - Copy a frame only when it beats the best in its window.
- **Cadence:**
  - *Time-based:* for example 2 Hz, choosing the best of each window.
  - *Distance-based:* trigger a new window whenever the road has advanced about
    70 % of the band depth, measured by the Phase 2 flow estimate. This needs no
    GPS and no metric calibration, and it gives gap-free coverage at any speed:
    about 1 Hz at 5 km/h and about 8 Hz at 40 km/h.
- **Output:**
  - The road-band crop, or the full frame, as JPEG q≥92 or lossless.
  - A sidecar per frame with `SensorTimestamp`, exposure, gain, estimated image
    speed, predicted blur px, and the frame's sharpness score and rank in its
    window.
  - Optionally, hand frames straight to the detector in memory.
- **Exposure:** motion-priority (Phases 1–2), with the blur budget in model-input
  pixels.
  - Example: whole 1920 px frame resized to 640 → 1.5 model px = 4.5 video px →
    about 2.6 ms at 40 km/h.
  - If the model reads native crops, 1.5 video px → about 0.9 ms.

---

## Appendix A: how the findings were checked

This ran in a cloud container, not on the Pi. `uv sync --frozen` succeeded, and the
analyzer functions were exercised directly.

- **Synthetic road.** A 1/f noise field (σ 0.04 around luminance 0.35), plus a
  bright lane stripe, 960×540 (or 1344×486 for ROI-level checks). Forward motion
  is modelled as a vertical scroll of 15 px/frame at 30 fps. Blur is a vertical box
  filter of speed × shutter. Gaussian sensor noise is added before 8-bit
  quantization.
- **Runs.** 8 blocks (`ABBA`/`BAAB`), 3 frames per slot, full metadata, default
  ROI, fed to `quality.analyze_run`. Arms: A = 4000 µs × 2, B = 2000 µs × 4, with
  variations (zero noise, equal noise, static scene with 2× noise, arms swapped).
- **F5 check:**

  ```python
  frames = [np.roll(tex_1344x486_uint8, 20 * i, axis=0) for i in range(3)]
  _motion(frames, [0, 33_333_333, 66_666_666], exposure_us=1e6 / 30)
  # → 42.0 px reported for a true 20 px/frame
  ```

- **F2 check:** `mean(grad²)` from `_smooth` plus central differences on pure
  white noise is 0.47·σ², against 2·(1.16·σ)² subtracted.

Synthetic data only shows *direction and magnitude of bias*. Before changing
defaults, confirm with the fixed analyzer on real Pi runs.
