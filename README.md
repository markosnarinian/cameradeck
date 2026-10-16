# CameraDeck

> [!NOTE]
> This project was generated fully with AI, with no human intervention.
> Full agent build transcript: https://ampcode.com/threads/T-01a07b1d-4f95-77de-937b-fa654d06b92f

A local-first, photo-only camera app for Raspberry Pi and Picamera2/libcamera.
The browser client uses React, TypeScript, React Router and shadcn-style Radix
components. Flask serves its compiled assets; there is no Node production server,
CDN, cloud requirement, or internet requirement during operation.

## Install and run

Use current **64-bit Raspberry Pi OS based on Debian 13 (Trixie)** with working
camera drivers. Check `rpicam-hello --list-cameras` first. CameraDeck does not
install drivers, overlays, firmware, or camera tuning files.

```bash
sudo apt install python3-picamera2 python3-venv python3-av ffmpeg rsync git curl xz-utils
curl -LsSf astral.sh/uv/install.sh | sh
git clone https://github.com/markosnarinian/cameradeck.git
cd cameradeck
```

Install the pinned build toolchain below, then run as the CameraDeck user:

```bash
uv venv --system-site-packages
uv sync --frozen --no-dev &&
  python3 scripts/frontend.py build &&
  uv run --no-dev python app.py
```

Open `http://127.0.0.1:8080` on the Pi. A clean clone contains **no compiled
frontend**. Installation and every update build `static/dist/` locally using
`npm ci --include=dev` and TypeScript/Vite. A failed install/build leaves the
old bundle intact and must not be followed by a start/restart. The application
and example systemd unit reject a missing, stale or damaged bundle at startup;
the check needs Python, not Node, and never downloads anything at boot.

### Pinned frontend toolchain

Use Node **22.23.3** (`.node-version`) and its bundled npm **10.9.9**
(`frontend/package.json`). Both versions are checked before building. Install
the official Linux binary as your normal user, without replacing system Node:

```bash
(
  set -eu
  version=$(cat .node-version)
  case "$(uname -m)" in
    aarch64) arch=arm64 ;;  # 64-bit Raspberry Pi OS
    x86_64) arch=x64 ;;    # development machines
    *) echo 'Unsupported architecture' >&2; exit 1 ;;
  esac
  archive="node-v$version-linux-$arch.tar.xz"
  download=$(mktemp -d)
  trap 'rm -rf "$download"' EXIT
  curl -fsSL "https://nodejs.org/dist/v$version/$archive" -o "$download/$archive"
  curl -fsSL "https://nodejs.org/dist/v$version/SHASUMS256.txt" -o "$download/SHASUMS256.txt"
  (cd "$download" && grep "  $archive$" SHASUMS256.txt | sha256sum -c -)
  mkdir -p "$HOME/.local/share/cameradeck/node-$version"
  tar -xJf "$download/$archive" --strip-components=1 -C "$HOME/.local/share/cameradeck/node-$version"
)
export PATH="$HOME/.local/share/cameradeck/node-$(cat .node-version)/bin:$PATH"
node --version
npm --version
```

Repeat the `export PATH=...` in each install/update shell. Keep the committed
lockfile; do not substitute `npm install`, omit dev dependencies, or use sudo
for npm. Internet access (or a populated npm cache) and space for dependencies,
the staged build and existing bundle are required during updates, not operation.
Build only while stopped; do not edit source or run concurrent builds. The build
stages output beside `static/dist`, then replaces it after success. The build
receipt checks frontend input and output hashes, not Git HEAD, so committing
unchanged built source does not invalidate it.

The system-site-packages venv sees apt's Picamera2/libcamera/simplejpeg. Locked
NumPy 2 takes precedence over apt packages, so compiled camera modules must
also support NumPy 2. Bookworm's NumPy-1-based modules are not compatible.

### Access from another device

```bash
read -rs -p 'CameraDeck password (8+ characters): ' CAMERADECK_PASSWORD; echo
export CAMERADECK_PASSWORD
uv run --no-dev python app.py --host 0.0.0.0
```

Use `http://<pi-ip>:8080` on the same trusted network. Optional arguments:
`--port 8080`, `--media /path/to/storage`. Run **one Python process**; multiple
camera owners or a development autoreloader are not supported.

LAN HTTP is not encrypted. Use only a trusted private network, or an SSH tunnel,
VPN or HTTPS reverse proxy. Do not expose the application to the internet.
Writes reject mismatched origins and login attempts are rate-limited.

Sessions use HttpOnly/SameSite cookies, last seven days, and survive service
restarts using the private `.session-key` in the media directory. Lock logs out
that browser. Changing the password invalidates existing sessions. The login
requests no autofill, but browsers/password managers may override that hint.

## Camera

- **Take photo:** saves a full-sensor JPEG without switching camera modes.
- **Start / Stop:** continuous full-resolution photos, default **5 photos/s**,
  until stopped. Runs continue independently of browser connections.
- **Frequency:** changes the target sampling rate, within the sensor/stream
  ceiling. Busy encoders skip frames instead of accumulating work. The run
  reports completed photos, achieved sensor-timestamp rate and skipped frames;
  a target is not a throughput guarantee.
- **Settings:** automatic/manual exposure, shutter and analogue gain, mounting
  orientation, white balance and supported focus controls. Editable requested
  values are separate from measured readings; stale readings are marked.
- **Preview:** short sequential JPEG requests, paused off the Camera page or
  while the tab is hidden. Missing/stale frames produce an explicit error.

The camera remains in one full-sensor YUV420 configuration with a shared 640px
preview. Sensor cadence is capped at 30 fps and the largest mode's advertised
ceiling; saved photos are sampled at their own target cadence. JPEG quality 90
and three encoding workers are the defaults. Selected requests are copied and
released before encoding. Thumbnails and intermediate previews are generated
before completed metadata is published. There is no video recording, mode
selector, experiment runner, Help page, or live focus scoring.

Exposure starts automatic, preferring the advertised Short AE mode. Short is
not a numerical shutter cap or a guarantee of brightness. Mounting orientation
is applied in the camera stack and persisted in `.rotation`, not just browser CSS.

Do not operate while driving. Do not exchange CSI cameras with power connected.

## Library and storage

`/library` supports photo and legacy-video viewing, original/metadata downloads,
ZIP download and confirmed deletion. Transfers live on `/transfer`, not in the
Library toolbar. Page and page size are
bookmarkable query parameters. Thumbnail size is remembered in the browser.
Pagination appears above and below the grid. Mirrored jumps go 1, 2, 3, 5, 10,
20, 30, 50, 100, 200, 300 or 500 pages backward/forward, increasing outward from
the center. Overshooting clamps to the first or last page.

Checkbox click toggles one item and establishes a range anchor. Shift adds an
inclusive range; Ctrl/Command+Shift removes it. Shift does not move an existing
anchor; if there is none, the first Shift-click that adds an item establishes it.
These shortcuts work on the checkbox, date label, or thumbnail. A plain thumbnail
click still opens the viewer; Ctrl/Command-click toggles one item without opening it.
Ranges span pages, including skipped pages, and survive changes to items per page.
Changing the kind filter resets the anchor, but selected IDs persist across
pages/filters; off-page selections are counted explicitly. There is no capture-count
limit on selection, ZIP download, deletion, or S3 upload. Pagination still shows at
most 100 items per page; it does not limit selection across pages.

Long operations show a progress bar. Deletion reports completed batches and S3
upload reports captures processed, leaving failed/uncertain items selected for
inspection or retry. Range resolution and ZIP preparation show an indeterminate
bar; once the ZIP is ready, transfer progress measures bytes received. These
measurable operations also show ETA based on average speed so far; estimates
adjust as work progresses. ETA is unavailable during range resolution and ZIP
preparation, whose total work is unknown. Deletion uses 100-capture requests for
progress checkpoints, not a total limit; backend locks are released per capture.
These operations have no fixed browser request timeout. Keep the page open until the
operation finishes: leaving stops subsequent batches, but an in-flight server
request may still complete. Large ZIPs need temporary disk space on the Pi and
enough memory in the browser; use rsync for very large transfers.

Completed JSON sidecars are the recovery source of truth. A persistent
`.library.sqlite3` catalog provides indexed counts and pagination; HTTP requests
do not scan the media directory. Startup reconciliation imports new/changed
sidecars, retries corrupt sidecars, and removes stale catalog entries. The first
import runs in the background and labels counts/results incomplete until it
finishes. Interrupted imports resume from persisted rows. Capture publication
and app deletion update the catalog immediately. External file edits require
a restart to reconcile.

Originals, `.json` metadata, `.thumb.jpg` and `.preview.jpg` remain in the media
directory. Existing photos/videos and saved experiment directories are retained;
the standalone `quality.py` analyzer remains available for old experiment data.
Interrupted legacy video recovery runs in the background and preserves
unrecoverable originals and `.pending.json` files.

Transfer's **Delete all media** action requires the exact text `DELETE ALL` and
an idle CameraDeck. It removes completed Library media across every page, not
just selections. Verify your remote copies first: CameraDeck does not verify
backups. It preserves remote copies, settings, credentials, the upload ledger,
pending clips, old experiment directories and unrelated files. Keep the page
open until deletion finishes; in-flight deletion cannot be cancelled.

Capture is refused below a 256 MiB free-space reserve. There is **no automatic
retention or loop deletion**. At 5 photos/s, 18,000 images are selected per hour;
storage consumption depends on scene, JPEG size and derivatives. Monitor storage
and use reliable power. Abrupt power loss can damage the filesystem.

### Transfer from the Pi with rsync

The Transfer page shows a command to run on the Pi after stopping capture:

```bash
rsync --archive --info=progress2,stats2 --human-readable --whole-file \
  --exclude='*.preview.jpg' \
  --exclude='*.thumb.jpg' \
  --exclude='.*' \
  --exclude='*.tmp' \
  --exclude='*.pending.*' \
  /home/pi/cameradeck/media/ \
  faros@192.168.1.10:/mnt/wd2tb/faros
```

Adjust host and destination if needed. This copies originals and JSON metadata,
not previews/thumbnails or internal files such as `.session-key` (private),
`.library.sqlite3` or `.uploads.sqlite3`, nor temporary/pending sidecars.
It never deletes the source. Keep originals on the Pi until the copy is verified.

### Run rsync from CameraDeck

The Transfer page can also have the Pi push the same files itself, so the copy
is started, watched and stopped from the browser. Under **rsync from
CameraDeck**, enter a destination and press **Start rsync**:

- `faros@192.168.1.10:/mnt/wd2tb/faros` over SSH
  (the default), or
- an existing absolute directory on the Pi (for example a mounted USB drive),
  outside the media directory.

It runs the same flags and exclusions as the command above, with the Pi as the
source, plus an I/O timeout. The page shows progress, rate, ETA, files copied,
the exact command and the log tail, and can stop the run. One run at a time is
allowed. rsync cannot start during capture, and capture and Pi power actions
are refused while it runs. It continues if the browser leaves; restarting or
stopping CameraDeck stops it. The last destination is remembered in the hidden
`.rsync-destination` file in the media directory.

SSH runs non-interactively (`BatchMode`), so a password or host-key prompt fails
the run instead of hanging. Set it up once on the Pi as the CameraDeck user:

```bash
ssh-keygen -t ed25519            # if ~/.ssh has no key yet
ssh-copy-id user@mac.local       # or paste the key shown on the Transfer page
ssh user@mac.local true          # accept the host key
```

On the Mac, enable **System Settings → General → Sharing → Remote Login**, and
for external volumes, allow full disk access for remote users. The Mac's
built-in rsync is enough on the receiving side.

### Optional S3-compatible upload

Set `CAMERADECK_S3_ENDPOINT` to override the initial endpoint (default
`http://192.168.1.10:3900`). On `/transfer`, expand **S3 upload**, select captures,
then enter bucket and optional prefix in the upload dialog. Selection is local
to this page; it does not inherit the Library's selection.
Credentials stay server-side through boto3's standard AWS credential chain.
Use HTTPS outside private networks. Upload never removes local originals.

Content-addressed SHA-256 keys and `.uploads.sqlite3` deduplicate uploads across
renames, copies and restarts. Ambiguous failures become **uncertain** and are
reconciled with HEAD, not resent. The server must support conditional PutObject
and read-after-write-consistent HeadObject. Never delete the upload ledger to
resolve uncertain uploads.

## Boot service and updates

See [UPDATE.md](UPDATE.md) before updating an existing installation. The example
`deploy/cameradeck.service` and polkit rule are not installed automatically.
Review paths/user/network binding, put a strong password and optional S3
environment in `.env`, and run `chmod 600 .env`.

```bash
sudo cp deploy/cameradeck.service /etc/systemd/system/
sudo cp deploy/90-cameradeck-power.rules /etc/polkit-1/rules.d/
sudo chown root:root /etc/polkit-1/rules.d/90-cameradeck-power.rules
sudo chmod 644 /etc/polkit-1/rules.d/90-cameradeck-power.rules
sudo systemctl daemon-reload
python3 scripts/frontend.py build && sudo systemctl enable --now cameradeck
journalctl -u cameradeck -f
```

Stop other camera applications first. Do not run CameraDeck as root. The
optional power API retains typed confirmation, fixed commands, and active-run
guards; it is not placed in the primary interface.

## Development and verification

Use the pinned Node/npm toolchain above. The production build helper installs
locked frontend dependencies and builds a startup-checked bundle:

```bash
python3 scripts/frontend.py build
uv sync --frozen
uv run python -m pytest -q
```

Commit frontend source and lockfile, **never** `static/dist/`, dependencies or
temporary build output. `npm --prefix frontend run typecheck` is available for
a quick check; direct Vite builds do not produce a deployment receipt. Flask serves
`/`, `/library` and `/transfer` as app routes and `/assets/*` as public hashed assets, so the
unlock screen can load before authentication.

The header and locked screen show Git HEAD captured when the backend starts
(`git rev-parse --short=12 HEAD`, or `unknown` without Git metadata). This is
not a dirty-tree fingerprint or the checkout's current HEAD after a later pull.
Deploy clean committed source with its matching build and restart together;
reload browser tabs after updating. Compare the label with the deployed
checkout's `git rev-parse --short=12 HEAD`, not a build-machine commit.

Tests use synthetic captures and camera requests, never real hardware. Chromium
is required for browser tests. Set `CAMERADECK_SCREENSHOTS=.amp/in/artifacts` to
capture synthetic-only review images. `tests/browser_smoke.py` performs read-only
checks on an already-running server; `CAMERADECK_TEST_URL` selects it and an
optional `CAMERADECK_PASSWORD` authorizes login. It does not capture, configure,
delete, upload, or change power.

Hardware acceptance remains separate: verify negotiated full-sensor mode,
preview continuity, measured exposure, sustained 5/s publication and skipped
frames under concurrent browsing before operational use. IMX415/B0569,
IMX708/Module 3 and NoIR behavior depends on installed drivers and camera tuning.

Format Python with `uv run black app.py camera.py catalog.py power.py storage.py tests`.
