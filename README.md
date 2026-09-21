# CameraDeck

> [!NOTE]
> This project was generated fully with AI, with no human intervention.
> Full agent build transcript: https://ampcode.com/threads/T-01a07b1d-4f95-77de-937b-fa654d06b92f

A passenger-friendly, local-first camera console for Raspberry Pi, built on Picamera2 and libcamera, with optional explicit S3-compatible backup. Supports cameras exposed by that stack, including Arducam B0569 / IMX415 and Raspberry Pi Camera Module 3 / 3 NoIR.

## Design

- One camera owner and shared 640px MJPEG preview, not one camera pipeline per browser.
- ISP-scaled YUV streams and hardware H.264 recording; MP4 muxing without transcoding.
- Full-sensor JPEG stills outside recording; video-resolution snapshots during recording.
- Controls discovered from the camera, including an advanced editor for every advertised control.
- Thumbnail-first library, intermediate-size still viewer, explicit original download, seekable video.
- Optional 3×3 sharpness grid. Relative detail/contrast scores are not a calibrated focus measurement; motion, light, noise and texture affect them.
- Large touch targets, mobile layout, explicit recording state and errors. Intended for a passenger, never a driver.

## Run on Raspberry Pi OS

Use a current **64-bit Raspberry Pi OS** with a working native libcamera/Picamera2 installation. First check `rpicam-hello --list-cameras`. CameraDeck does not install or replace camera drivers, tuning files, overlays, or firmware.

```bash
sudo apt install python3-picamera2 python3-venv python3-av ffmpeg
curl -LsSf astral.sh/uv/install.sh | sh
git clone https://github.com/markosnarinian/cameradeck.git
cd cameradeck
uv venv --system-site-packages
uv sync --frozen
uv run python app.py
```

Open **http://127.0.0.1:8080** on the Pi. The system-site-packages flag is important: use Raspberry Pi OS's Picamera2, libcamera, NumPy, and PyAV rather than unrelated pip camera packages (`uv venv --system-site-packages` followed by `uv sync --frozen` preserves this). No Node, frontend build, CDN, internet connection, or cloud account is needed to run the app.

### Access from your phone

Join the Pi and phone to the same trusted Wi-Fi network or hotspot. Stop the local instance with Ctrl+C, then:

```bash
read -rs -p 'Choose a CameraDeck password (8+ characters): ' CAMERADECK_PASSWORD; echo
export CAMERADECK_PASSWORD
uv run python app.py --host 0.0.0.0
```

Open `http://<pi-ip>:8080` on the phone (`hostname -I` on the Pi shows its IP addresses). All clients share the same camera and recording state. Up to four simultaneous live viewers are allowed, leaving server capacity for capture and stop commands. Browsing the library or hiding the tab disconnects that browser's preview, **not its recording**.

Network binding requires a password of at least eight characters. Password login is rate-limited, sessions use HttpOnly/SameSite cookies, and cross-origin writes are rejected. Plain LAN HTTP is **not encrypted**: only use a trusted private network. For untrusted networks use an SSH tunnel, VPN, or HTTPS reverse proxy. Do not port-forward this application to the internet. Sessions expire when the server restarts.

Optional arguments: `--port 8080`, `--media /path/to/storage`. Use one process only: do not run multiple WSGI workers or a development autoreloader against the camera.

## Using the console

- **Take photo:** full sensor resolution outside recording. Preview pauses briefly during the mode switch and resumes automatically. Two settling frames are allowed before capture. During recording, a still uses the selected 720p/1080p stream without stopping video.
- **Record video:** 10 Mbps target H.264 hardware encoding on Pi 4, with timestamp-preserving PyAV MP4 muxing. Video is silent; no microphone is assumed. The red stop button remains available from the library. Actual frame rate depends on the sensor and exposure, and is shown in live telemetry.
- **Periodic stills:** choose this capture mode instead of Video. Set the interval (1–86,400 seconds), photo limit (0 means until stopped), and optional still-only JSON control overrides. The first full-sensor JPEG is immediate; the interval is measured between capture starts, and slow captures extend it without building a backlog. Each photo appears in the library as it completes. The Pi runs the sequence independently of browsers and checks the 256 MiB storage reserve before each photo. Stop remains available in the library; an in-progress photo finishes before stopping.
- **Road A/B experiment:** compares two equal-nominal-exposure shutter/gain arms in randomized, balanced ABBA/BAAB blocks without leaving the configured video pipeline. B gain is derived from A so shutter × analogue gain stays equal. Draw the road analysis area directly on the live preview or adjust its percentage coordinates; letterboxing is excluded and any edit requires reconfirmation. The Pi accepts frames only after actual exposure/gain metadata matches the arm, preserves three native Y-plane samples per slot as lossless PGM, and records request metadata in an atomic manifest. Eight 1080p blocks need roughly 200 MiB. Camera controls are restored afterward; the experiment never applies a winner automatically.
- **Offline road analysis:** download the experiment ZIP, extract it, then run `uv run python -m quality <run-directory> --output <report-directory>`. The NumPy/Pillow-only analyzer writes `report.json`, `pairs.csv`, and `contact-sheet.jpg`. It measures predicted exposure smear from local inter-frame motion, noise-corrected road detail, a high-frequency noise proxy, darkness and clipping. Blocks with missing or mismatched metadata, unequal measured effective exposure, timestamp/frame-duration faults, excessive duration or coarse scene changes are rejected. At least six accepted blocks with both orders are required; the result is A preferred, B preferred, trade-off, or insufficient evidence. Scores are meaningful only within one camera/resolution/ROI/run and are not calibrated sensor SNR or downstream-model accuracy.
- **Camera test:** this is an ordered **stationary still sweep**, not a driving comparison. It automatically captures every combination of selected shutter times and analogue gains, with configurable settling time and 1–5 photos per pair. Defaults test 1,000, 2,000, 4,000 and 8,000 µs (1/1000–1/125 s) at 2×, 4× and 8× gain, with three photos per pair: 36 photos total. Up to eight values per axis are allowed; unsupported controls or out-of-range values are rejected before anything changes. Manual exposure uses the camera's advertised mode controls (or the older AE toggle). Settling occurs in preview before each full-sensor capture. Actual sensor results may differ from requests: comparison buttons show requested and measured exposure/gain and open the photo viewer.
- **Comparing motion tests:** repeat the same scene and movement safely, preferably with a passenger operating the console. Short shutter times reduce motion blur but make low-light images darker; increasing gain increases noise. Inspect originals for blur, brightness and noise rather than choosing the highest sharpness score, which noise can inflate. The test does not claim an automatic optimal setting, simulate movement, change camera drivers, or measure video temporal denoising. Apply a preferred result manually after comparison.
- **Road-experiment limits:** equal shutter × analogue gain is only equal nominal signal exposure; digital gain and ISP behavior are measured and may invalidate a block. Randomization and short blocks reduce changing-road bias but cannot make one camera observe two settings simultaneously. Coarse drift checks reject obvious changes, not all parallax or lighting changes. Use repeated representative routes and treat high rejection rates as insufficient evidence. A synchronized reference/experiment camera pair is required for tightly controlled radical comparisons. Verify Picamera2 request ownership, YUV range, control-settling traces, preview continuity and restoration on the deployed Pi before operational use.
- **Sequence settings and safety:** still, stationary-test, and road-experiment settings are kept separately for the server session. Overrides apply only during the run; the previous camera configuration and explicit controls are restored on completion, cancellation, or failure. Reconfiguration, manual captures, control edits, video recording, and power actions are blocked during sequences. Road experiments require at least 10 fps so settling and scene-pairing remain bounded. Stop the sequence before switching modes. Graceful server shutdown stops the worker; sequences do not automatically resume after a restart, and already completed evidence remains available.
- **Video settings:** 720p or 1080p, 1–30 fps target (clamped to the advertised sensor ceiling), and 0°/180° mounting orientation. Apply while not recording. Orientation is applied in the camera stack to preview, video, and stills, not just as CSS. Applying video settings resets controls. Settings are session-local.
- **Controls and in-app help:** every capture mode shows a plain-language purpose, run summary, contextual validation, and a link to the top-level **Help** page. Periodic stills use an explicit “until stopped” option, stationary sweeps show their photo count and minimum settling time, and Road mode shows calculated gain, storage, slots, and frame counts before starting. The main panel provides common controls; “Every camera control” exposes **all controls advertised by the configured camera**, including names, types, bounds, array lengths, and available enumerations. Values are JSON: `true`, `1.5`, `[1.8,1.4]`, or `[x,y,width,height]` for a crop. AF windows are arrays of rectangles. Unsupported names and invalid numeric values are rejected.
- **Manual operation:** changing shutter/gain selects the corresponding manual mode on current libcamera (or disables AE on older versions). Set Shutter mode and Gain mode to Auto to restore automatic operation; older stacks show an Auto exposure toggle instead. Changing lens position selects manual focus; the one-shot button triggers autofocus. White-balance presets turn AWB on. For custom `ColourGains`, turn AWB off first. Advanced changes may require related controls; advertised controls do not guarantee every value is effective on every sensor.
- **Sharpness grid:** optional live 3×3 Laplacian-variance scores, sampled twice per second from a small shared preview. The brightest score label marks the highest-detail region in that frame, not a guaranteed in-focus region. Still scores are stored in the JSON sidecar. Video-review scores are calculated from the displayed/paused/seeked frame on the client, not by decoding full videos on the Pi. Compare the same scene and settings; live, still, and video scores are not calibrated against each other.
- **Library:** newest first, photo/video filters, 40-item Previous/Next pages, selectable cards, ZIP download, bulk deletion, and optional S3 upload. Load full resolution explicitly or download one original from the viewer. Video supports seeking/range requests. Download JSON metadata for exposure and camera settings. Deletion requires confirmation and removes the original and its derived files permanently.
- **Power:** the controls panel can reboot or shut down the Pi after typed confirmation. Power actions are refused while recording. A shutdown requires physical access or separate hardware to restore power.

### Starting defaults for truck-mounted cameras at dawn/twilight

- **Video:** 1080p with a 30 fps request, reduced to the advertised sensor ceiling. The documented B0569 mode tops out at 15.75 fps; cameras capable of 30 fps can use 30. The Pi 4's [official specification](https://www.raspberrypi.com/products/raspberry-pi-4-model-b/specifications/) rates hardware H.264 encoding at 1080p30; higher-rate recording needs separate sensor/encoder verification. Frame rate controls sampling, not shutter blur.
- **Exposure:** opening/reconfiguring the camera explicitly enables automatic exposure, gain and white balance where available, and selects the advertised **Short** AE exposure mode. This favours short exposures while adjusting brightness as daylight changes; it does not impose a numerical shutter cap. If Short is not advertised within the supported range, the camera keeps its normal automatic exposure. The Exposure panel exposes this preference. Inspect actual shutter time in telemetry and metadata; manual shutter/gain settings override AE behaviour. See [libcamera control semantics](https://libcamera.org/api-html/namespacelibcamera_1_1controls.html).
- **Periodic stills:** one-second interval, unlimited until stopped, inheriting current camera controls unless overridden. At 30 km/h this is about 8.3 m between photos, versus 41.7 m at the former five-second default. Capture overhead can increase that gap. Stills are sampled observations, not continuous dashcam coverage; use video when gaps are unacceptable. There is no automatic loop deletion: storage must be managed.
- **Calibration:** start comparisons around 2,000 µs (1/500 s). The 1,000 µs test trades brightness for motion detail; 8,000 µs provides a brighter but more blur-prone reference. Three repeats help reveal vibration/focus variability. These are engineering starting points, not validated exposure prescriptions: scene distance, lens field of view, illumination, mounting vibration, rolling shutter, windshield reflections and sensor tuning all matter. Test a rigid mount on a representative route with a passenger operating the console; inspect dark areas and nearby objects at the upper operating speed before relying on captures. Short exposures do not eliminate rolling-shutter distortion or lighting flicker.

### Optional S3-compatible upload

Set the server shown initially in the upload dialog; the requested example default is:

```bash
CAMERADECK_S3_ENDPOINT=http://192.168.1.10:3900
```

Select library items, choose **Upload to S3**, and enter the bucket plus an optional prefix. Credentials remain server-side and use boto3's standard AWS credential chain (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, optional `AWS_SESSION_TOKEN`, profiles, or an instance role). The endpoint must use HTTPS outside private networks. Local media is never removed after upload.

CameraDeck hashes every original with SHA-256 and uses a content-addressed object key. A durable SQLite ledger, conditional object creation, and remote metadata checks deduplicate copies, renames, concurrent requests, and process restarts for each endpoint/bucket/prefix destination. If a network failure makes the result ambiguous, CameraDeck marks it **uncertain** and will only reconcile with `HEAD`; it will not send the bytes again. This strict at-most-once behavior requires an S3-compatible server that supports conditional `PutObject` and read-after-write-consistent `HeadObject`. Resolve an uncertain object at the server rather than deleting CameraDeck's `.uploads.sqlite3` ledger.

## Camera compatibility

| Camera | Behaviour |
| --- | --- |
| Arducam B0569 / IMX415 | Fixed focus; 3864 × 2192 sensor. The connected module advertises a single 15.75 fps mode. No fabricated autofocus controls. Requires working native IMX415 driver/tuning; Arducam documents libcamera 0.5.0 or newer. |
| Raspberry Pi Camera Module 3 / IMX708 | Autofocus mode, range, speed, lens position, trigger and windows appear when advertised. Sensor modes are discovered. |
| Camera Module 3 NoIR / IMX708 | Same software controls; the missing IR filter is a physical difference and is not reliably auto-detectable. Colour depends on illumination and any external filter. |

The Pi 4 normally has one CSI connector; this is not a simultaneous three-camera recorder. Select among cameras actually enumerated by libcamera. **Power off before exchanging ribbon-connected modules** and ensure the appropriate overlay/driver is configured before starting again. Hardware-tested on the B0569; Module 3 and NoIR require physical verification when attached. Sensor HDR behaviour, driver-specific controls, and maximum usable modes remain dependent on the installed camera stack. The Pi 4 hardware video encoder is limited to 1080p here; stills retain full sensor resolution.

## Storage and interrupted sessions

Captures stay under `media/` (gitignored). Originals are uploaded only after explicit selection and confirmation. Each completed capture has a `.json` metadata sidecar, `.thumb.jpg`, and `.preview.jpg`. The library only publishes completed sidecars; recording files are not offered as downloads until finalized.

Capture is refused below 256 MiB free; a server-side watchdog checks every two seconds and stops recording at that threshold, even without a connected browser. Other processes can still exhaust the disk between checks. Use reliable power and adequate storage; abrupt removal of power can damage the filesystem.

Video uses fragmented MP4 with approximately one-second keyframe intervals. Graceful Ctrl+C/SIGTERM finalizes recording. On startup, `.pending.json` records are used to recover decodable interrupted clips into the library. **Recovery is best effort:** the last fragment may be missing, very short recordings may contain no decodable fragment, and disk/SD failures cannot be repaired by the app. Unrecoverable originals are preserved and an error is displayed. Some older video editors may require remuxing a downloaded clip: `ffmpeg -i input.mp4 -c copy output.mp4`.

## Optional boot service

An example unit is provided in `deploy/cameradeck.service`. Review its user, paths, and network binding before installing it. Put a strong `CAMERADECK_PASSWORD=...` and any S3 credentials/endpoint override in `/home/pi/cameradeck/.env` with permissions `chmod 600 .env`. This service is **not installed or enabled automatically**.

```bash
sudo cp deploy/cameradeck.service /etc/systemd/system/
sudo cp deploy/90-cameradeck-power.rules /etc/polkit-1/rules.d/
sudo chown root:root /etc/polkit-1/rules.d/90-cameradeck-power.rules
sudo chmod 644 /etc/polkit-1/rules.d/90-cameradeck-power.rules
sudo systemctl daemon-reload
sudo systemctl enable --now cameradeck
journalctl -u cameradeck -f
```

Stop any manually running CameraDeck/rpicam application first. To release the camera for another tool, stop the service with `sudo systemctl stop cameradeck`.

The polkit rule grants only the `pi` service user the logind power-off/reboot actions and keeps the service's `NoNewPrivileges=true` hardening. Review and change the username in both deployment files if CameraDeck runs as another user. Without this root-owned rule, power requests fail safely and are logged; never run CameraDeck as root or grant it unrestricted sudo.

## Development and verification

```bash
uv sync --frozen
uv run python -m pytest -q
# With CameraDeck already running and Chromium installed:
uv run python tests/browser_smoke.py
```

Unit/API tests do not open a real camera. The explicit browser smoke test **does** change camera settings, take real stills, record video, exercise playback/seek/focus/download/delete, and check desktop/mobile layouts. It restores the initial video settings and deletes only its own captures. Override `CAMERADECK_TEST_URL`, `CAMERADECK_PASSWORD`, or `CHROMIUM` as needed. Screenshots/downloads remain in ignored `test-results/`; inspect them locally and do not publish private camera scenes.

Code formatting: `uv run black app.py camera.py power.py storage.py tests`, `uv run js-beautify -r static/app.js`, `uv run css-beautify -r static/style.css`.

### Verified on the connected Pi 4 / B0569 (7 September 2026)

- 23 unit/API checks passed, including authentication/origin protection, range downloads, bounded streaming connections, control validation, failed-still preview recovery, and recovery of a fragmented MP4 with its final fragment removed.
- Real Chromium hardware smoke passed at desktop and 390px mobile widths, with no JavaScript errors: 28 advertised controls, manual exposure readback, 3864 × 2192 stills, 1920 × 1080 recording and uninterrupted snapshots, playback/seek, live/still/video sharpness grids, original/metadata downloads, and confirmed deletion.
- A 30 fps request was correctly clamped to 15.75 fps for this sensor. FFprobe confirmed the downloaded clip is 1080p H.264 with a duration matching the published 5.06-second clip metadata.
- Rendered screenshots were inspected, including recording and viewer states. These are local test results, not a guarantee for other camera/OS combinations. No Module 3/NoIR hardware was available for physical testing.

## Architecture

`camera.py` owns camera lifecycle, shared frames, controls, capture, media derivatives, and clip recovery. `app.py` owns HTTP/authentication, streaming, range/ZIP delivery, pagination, and storage protection. `storage.py` owns S3 delivery and its deduplication ledger; `power.py` owns the fixed host power commands. `static/` is a small dependency-free browser client. Capture/reconfigure operations share a lock; MJPEG clients consume the latest frame rather than accumulating queues. A two-thread software JPEG encoder handles the small preview, leaving Pi 4's hardware encoder for H.264. Full-resolution images are processed only after a capture, not continuously.

References: [Picamera2](https://github.com/raspberrypi/picamera2), [official examples](https://github.com/raspberrypi/picamera2-examples), [Arducam IMX415 guide](https://docs.arducam.com/Raspberry-Pi-Camera/Native-camera/8.3MP-IMX415/).
