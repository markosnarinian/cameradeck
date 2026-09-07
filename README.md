# CameraDeck

A passenger-friendly camera console for Raspberry Pi: local-first, no cloud, built on Picamera2 and libcamera. Supports cameras exposed by that stack, including Arducam B0569 / IMX415 and Raspberry Pi Camera Module 3 / 3 NoIR.

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
- **Video settings:** 720p or 1080p, 1–30 fps target (clamped to the advertised sensor ceiling), and 0°/180° mounting orientation. Apply while not recording. Orientation is applied in the camera stack to preview, video, and stills, not just as CSS. Applying video settings resets controls. Settings are session-local.
- **Controls:** the main panel provides common controls; “Every camera control” exposes **all controls advertised by the configured camera**, including names, types, bounds, array lengths, and available enumerations. Values are JSON: `true`, `1.5`, `[1.8,1.4]`, or `[x,y,width,height]` for a crop. AF windows are arrays of rectangles. Unsupported names and invalid numeric values are rejected.
- **Manual operation:** changing shutter/gain selects the corresponding manual mode on current libcamera (or disables AE on older versions). Set Shutter mode and Gain mode to Auto to restore automatic operation; older stacks show an Auto exposure toggle instead. Changing lens position selects manual focus; the one-shot button triggers autofocus. White-balance presets turn AWB on. For custom `ColourGains`, turn AWB off first. Advanced changes may require related controls; advertised controls do not guarantee every value is effective on every sensor.
- **Sharpness grid:** optional live 3×3 Laplacian-variance scores, sampled twice per second from a small shared preview. The brightest score label marks the highest-detail region in that frame, not a guaranteed in-focus region. Still scores are stored in the JSON sidecar. Video-review scores are calculated from the displayed/paused/seeked frame on the client, not by decoding full videos on the Pi. Compare the same scene and settings; live, still, and video scores are not calibrated against each other.
- **Library:** newest first, photo/video filters, 40-item pages, 480px thumbnails, then up-to-1600px still previews. Load full resolution explicitly or download the original. Video supports seeking/range requests. Download JSON metadata for exposure and camera settings. Deletion requires confirmation and removes the original and its derived files permanently.

## Camera compatibility

| Camera | Behaviour |
| --- | --- |
| Arducam B0569 / IMX415 | Fixed focus; 3864 × 2192 sensor. The connected module advertises a single 15.75 fps mode. No fabricated autofocus controls. Requires working native IMX415 driver/tuning; Arducam documents libcamera 0.5.0 or newer. |
| Raspberry Pi Camera Module 3 / IMX708 | Autofocus mode, range, speed, lens position, trigger and windows appear when advertised. Sensor modes are discovered. |
| Camera Module 3 NoIR / IMX708 | Same software controls; the missing IR filter is a physical difference and is not reliably auto-detectable. Colour depends on illumination and any external filter. |

The Pi 4 normally has one CSI connector; this is not a simultaneous three-camera recorder. Select among cameras actually enumerated by libcamera. **Power off before exchanging ribbon-connected modules** and ensure the appropriate overlay/driver is configured before starting again. Hardware-tested on the B0569; Module 3 and NoIR require physical verification when attached. Sensor HDR behaviour, driver-specific controls, and maximum usable modes remain dependent on the installed camera stack. The Pi 4 hardware video encoder is limited to 1080p here; stills retain full sensor resolution.

## Storage and interrupted sessions

Captures stay under `media/` (gitignored). Originals are never uploaded. Each completed capture has a `.json` metadata sidecar, `.thumb.jpg`, and `.preview.jpg`. The library only publishes completed sidecars; recording files are not offered as downloads until finalized.

Capture is refused below 256 MiB free; a server-side watchdog checks every two seconds and stops recording at that threshold, even without a connected browser. Other processes can still exhaust the disk between checks. Use reliable power and adequate storage; abrupt removal of power can damage the filesystem.

Video uses fragmented MP4 with approximately one-second keyframe intervals. Graceful Ctrl+C/SIGTERM finalizes recording. On startup, `.pending.json` records are used to recover decodable interrupted clips into the library. **Recovery is best effort:** the last fragment may be missing, very short recordings may contain no decodable fragment, and disk/SD failures cannot be repaired by the app. Unrecoverable originals are preserved and an error is displayed. Some older video editors may require remuxing a downloaded clip: `ffmpeg -i input.mp4 -c copy output.mp4`.

## Optional boot service

An example unit is provided in `deploy/cameradeck.service`. Review its user, paths, and network binding before installing it. Put a strong `CAMERADECK_PASSWORD=...` in `/home/pi/cameradeck/.env` with permissions `chmod 600 .env`. This service is **not installed or enabled automatically**.

```bash
sudo cp deploy/cameradeck.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cameradeck
journalctl -u cameradeck -f
```

Stop any manually running CameraDeck/rpicam application first. To release the camera for another tool, stop the service with `sudo systemctl stop cameradeck`.

## Development and verification

```bash
uv sync --frozen
uv run python -m pytest -q
# With CameraDeck already running and Chromium installed:
uv run python tests/browser_smoke.py
```

Unit/API tests do not open a real camera. The explicit browser smoke test **does** change camera settings, take real stills, record video, exercise playback/seek/focus/download/delete, and check desktop/mobile layouts. It restores the initial video settings and deletes only its own captures. Override `CAMERADECK_TEST_URL`, `CAMERADECK_PASSWORD`, or `CHROMIUM` as needed. Screenshots/downloads remain in ignored `test-results/`; inspect them locally and do not publish private camera scenes.

Code formatting: `uv run black app.py camera.py tests`, `uv run js-beautify -r static/app.js`, `uv run css-beautify -r static/style.css`.

### Verified on the connected Pi 4 / B0569 (7 September 2026)

- 23 unit/API checks passed, including authentication/origin protection, range downloads, bounded streaming connections, control validation, failed-still preview recovery, and recovery of a fragmented MP4 with its final fragment removed.
- Real Chromium hardware smoke passed at desktop and 390px mobile widths, with no JavaScript errors: 28 advertised controls, manual exposure readback, 3864 × 2192 stills, 1920 × 1080 recording and uninterrupted snapshots, playback/seek, live/still/video sharpness grids, original/metadata downloads, and confirmed deletion.
- A 30 fps request was correctly clamped to 15.75 fps for this sensor. FFprobe confirmed the downloaded clip is 1080p H.264 with a duration matching the published 5.06-second clip metadata.
- Rendered screenshots were inspected, including recording and viewer states. These are local test results, not a guarantee for other camera/OS combinations. No Module 3/NoIR hardware was available for physical testing.

## Architecture

`camera.py` owns camera lifecycle, shared frames, controls, capture, media derivatives, and clip recovery. `app.py` owns HTTP/authentication, streaming and range delivery, and storage protection. `static/` is a small dependency-free browser client. Capture/reconfigure operations share a lock; MJPEG clients consume the latest frame rather than accumulating queues. A two-thread software JPEG encoder handles the small preview, leaving Pi 4's hardware encoder for H.264. Full-resolution images are processed only after a capture, not continuously.

References: [Picamera2](https://github.com/raspberrypi/picamera2), [official examples](https://github.com/raspberrypi/picamera2-examples), [Arducam IMX415 guide](https://docs.arducam.com/Raspberry-Pi-Camera/Native-camera/8.3MP-IMX415/).
