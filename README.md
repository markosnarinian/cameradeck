# CameraDeck

A browser-based camera console for Raspberry Pi. Take full-resolution photos,
run continuous capture, browse your library, and copy files off the Pi—all on
your local network.

CameraDeck uses Picamera2 and libcamera. The web UI is served by the Pi; it needs
no cloud account, CDN, or internet connection once installed.

## What it does

- **Capture:** single photos or continuous capture, defaulting to 5 photos/s.
  Actual throughput depends on your camera, exposure, and storage.
- **Camera controls:** live preview, exposure, gain, white balance, focus where
  supported, and mounting orientation.
- **Library:** thumbnails, full-size viewing, range selection across pages,
  original/metadata downloads, ZIP downloads, and deletion with progress.
- **Transfers:** push your media to another machine over SSH with rsync, or copy
  it to a local mounted drive. Transfers continue after you close the browser.
- **Status:** free disk space, CPU temperature, and Pi throttling warnings.

Capture is photo-only. Existing video files from older versions remain viewable.

## Requirements

- A Raspberry Pi running **64-bit Raspberry Pi OS Trixie (Debian 13)**.
- A working Picamera2/libcamera camera: check `rpicam-hello --list-cameras`.
- Python 3.11 or newer, reliable power, and enough storage for your photos.

CameraDeck does not install camera drivers or firmware. Bookworm's NumPy-1-based
camera modules are not compatible with this release's NumPy 2 dependencies.

## Install

Install the OS dependencies, then create a virtual environment that can see the
camera packages supplied by Raspberry Pi OS:

```bash
sudo apt install python3-picamera2 python3-venv python3-pip python3-av ffmpeg rsync
python3 -m venv --system-site-packages ~/.venvs/cameradeck
~/.venvs/cameradeck/bin/python -m pip install cameradeck
~/.venvs/cameradeck/bin/cameradeck
```

Open **http://127.0.0.1:8080** in a browser on the Pi. The PyPI package includes
the compiled web UI; Node.js and a Git checkout are not needed to run it.

### Connect from another device

Set a password before listening on the network:

```bash
read -rs -p 'CameraDeck password: ' CAMERADECK_PASSWORD; echo
export CAMERADECK_PASSWORD
~/.venvs/cameradeck/bin/cameradeck --host 0.0.0.0
```

Open `http://<pi-ip>:8080` on the same trusted network. HTTP is not encrypted;
do not expose CameraDeck directly to the internet. Use an SSH tunnel, VPN, or
HTTPS reverse proxy for access beyond a trusted LAN.

### Storage and options

```bash
cameradeck --host 127.0.0.1 --port 8080 --media /path/to/photos
```

Use the full executable path above unless you have activated the virtual
environment. Packaged installs save to `~/.local/share/cameradeck/media` by
default, or `$XDG_DATA_HOME/cameradeck/media` when set. `--media` overrides it.

Photos, JSON metadata, thumbnails, and previews live in that directory alongside
the library index, saved settings, and private session key. Keep the entire
directory when moving an installation. Sessions survive restarts; changing the
password signs existing sessions out.

There is **no automatic deletion**. Capture stops accepting new photos below a
256 MiB free-space reserve. At 5 photos/s, a run can produce 18,000 photos/hour.
Monitor storage, back up your files, and verify remote copies before deleting.

Run one CameraDeck process at a time, and stop other camera applications first.
Capture sequences and rsync continue until stopped, even if you close the tab.
ZIP downloads and browser-driven batch operations need the page to stay open.
Do not run CameraDeck as root or exchange CSI cameras while powered.

## Updates and boot service

See **[UPDATE.md](https://github.com/markosnarinian/cameradeck/blob/main/UPDATE.md)**
for safe upgrades, rollback, and the existing checkout-based systemd setup.
Always check that the instance is idle before stopping or upgrading it.

The example service and power-control rule in `deploy/` are optional and are not
installed by pip. They target a Git checkout; do not copy them unchanged for a
PyPI installation. CameraDeck does not need root privileges to run.

## Run from source

A source checkout needs a local UI build. Install Git and
[uv](https://docs.astral.sh/uv/getting-started/installation/), then clone:

```bash
sudo apt install git
git clone https://github.com/markosnarinian/cameradeck.git
cd cameradeck
```

From the checkout, install the pinned
[Node/npm toolchain](https://github.com/markosnarinian/cameradeck/blob/main/UPDATE.md#frontend-toolchain),
then run:

```bash
uv venv --system-site-packages
uv sync --frozen --no-dev
python3 scripts/frontend.py build
uv run --no-dev python app.py
```

Source runs keep the existing default storage location, `media/` in the checkout.
Do not commit `static/dist/`. The build helper stages the UI and verifies it at
startup; never restart after a failed build.

For development, run `uv sync --frozen`, build the UI, then `uv run pytest -q`.
Browser tests require Chromium (`CHROMIUM` selects its executable); camera tests
use synthetic requests. Real-camera acceptance still requires testing preview,
exposure, sustained capture, and skipped frames on your hardware.

To build a release, run `uv build` with the pinned Node/npm on PATH. Both the
wheel and source distribution include the UI. GitHub Actions tests the source,
validates both distributions, and publishes on a matching `v<version>` release.
The PyPI account must authorize this repository's `python-publish.yml` workflow
and `pypi` environment as a Trusted Publisher before the first publication.
