# Updating CameraDeck on the Raspberry Pi

These are the steps to update an already-deployed CameraDeck checkout on a
Raspberry Pi to the latest code. See `README.md` for first-time installation.

## Before you start

- Only update while CameraDeck is idle or stopped: nothing in progress that
  stopping it would interrupt (capture, rsync, deletion, download,
  library indexing, clip recovery, pending shutdown/reboot). Step 1 covers how
  to check. Captures and transfers keep running until someone stops them, even
  after every browser disconnects, so if one is active, leave it to whoever
  started it and update once it has finished. Don't stop someone else's run
  just to update.
- Note whether CameraDeck runs as the `cameradeck` systemd service or is
  started manually (`uv run python app.py`). Use the matching stop/start
  steps below.
- Never delete the `media/` directory during an update. It lives outside the git checkout's tracked
  files and are left alone by `git pull`.
- `.env` (password) is not tracked in git and is
  not touched by an update.
- Mounting orientation now persists in `.rotation` inside the configured
  media directory. After updating an older install, apply your preferred
  orientation once while idle to save it. Keep this file when backing up or
  moving the media directory; no migration or service changes are needed.

## Photo-only release

- Capture is now photo-only, defaulting to **5 photos/second until stopped**.
  Video recording, tests/experiments, separate-still sequences, mode selection,
  Help, and focus scoring are removed. Existing media, pending clips, experiment
  directories, `.rotation`, and `.time_offset` are retained.
  Old clients/scripts using removed routes must be updated. Stop old runs before
  installing this release; never assume closing the browser stopped them.
- Scripts supplying temporary burst controls to `/api/sequence/start` now have
  restoration validated before capture starts or camera settings change.
  Unrestorable overrides are rejected. Controls with no usable default may
  require setting a persistent value through `/api/controls` first so the burst
  can restore it. Restoration of automatic exposure/white balance remains
  supported. Normal UI capture is unchanged.
- **Frontend build migration:** `static/dist/` is no longer tracked. Every
  installation/update builds on the Pi with pinned Node 22.23.3 and npm 10.9.9,
  `npm ci --include=dev`, and TypeScript/Vite. Follow the backup step below
  **before pulling**: the first Git update removes the old tracked bundle.
  `scripts/frontend.py build` stages output without touching the installed
  bundle on dependency/build failure. Never restart after a failed command.
  Startup checks frontend source/output hashes; stale, missing or damaged
  output is rejected. Node is not run at service startup or during operation.
- Library Shift-click ranges now span pages, including skipped pages. Restart
  the Python service with the matching frontend: off-page ranges use the new
  read-only `POST /api/media/range` endpoint. Reload existing browser tabs after
  updating. Selection and bulk actions no longer have a capture-count limit.
  The matching backend also accepts Library ID lists larger than 16 KiB. If using
  a reverse proxy, allow large JSON bodies on `/api/media/range`,
  `/api/media/download`, and `/api/media/delete`, and allow long requests while a
  ZIP is prepared or transferred. Progress bars show completed deletion batches
  and ZIP bytes received; preparation and
  range resolution are indeterminate. Keep the browser page open until finished.
  Large ZIPs still require temporary Pi disk space and browser memory; rsync is
  preferable for very large transfers. No migration or dependency change is required.
- Transfer's **Delete all media** now polls the authenticated read-only
  `GET /api/media/delete-all/progress` endpoint for completed captures and an
  estimated time remaining. Deploy the matching backend/frontend and reload
  browser tabs; if using a reverse proxy, forward this route as well as the
  long-running `POST /api/media/delete-all` request and the authenticated
  `POST /api/media/delete-all/cancel` route. **Cancel deletion** stops between
  captures: the current capture finishes deleting, already-deleted captures
  cannot be restored, and remaining captures are left untouched. Keep the page
  open until the completion/cancellation result appears. No dependency,
  service-setting, or data migration is needed.
- Exposure time and gain now have independent Auto/Manual selectors, including
  shutter-priority (manual exposure, auto gain) and gain-priority (auto exposure,
  manual gain). No camera-stack upgrade is required: newer libcamera uses the
  separate mode controls; older Raspberry Pi stacks use zero for the automatic
  numeric control. Scripts using `/api/controls` on older stacks can likewise
  send `ExposureTime: 0` or `AnalogueGain: 0` to release just that control.
  `AeEnable` still switches both together; do not send it with a priority-mode
  request on an older stack. Verify both mixed modes after updating while idle.
- Flask must serve `/`, `/library`, `/transfer`, `/assets/*`, `/api/*`, and `/media/*` on the
  same origin. If using a reverse proxy, forward the new routes too; avoid
  caching app HTML/API responses. Hashed assets can be cached. Direct `/library`
  and `/transfer` reloads must reach Flask, not a proxy filesystem lookup.
- Transfer tools have moved off the Library page. `/transfer` can run and
  supervise rsync on the Pi, pushing to an SSH destination or a local directory. It needs
  the `rsync` system package on the Pi (`sudo apt install rsync` if
  `rsync --version` fails) and on the destination. For SSH destinations,
  the service user (`pi`) also needs a key that logs in without a prompt and
  an accepted host key. A run is
  stopped when the service stops, so treat an active transfer like an active
  capture in step 1. Check `GET /api/transfer/rsync` (`"active": false`) or the
  Transfer page before stopping. The last destination is stored in
  `.rsync-destination` in the media directory; keep it with the other hidden files.
  Without a saved destination, it defaults to
  `faros@192.168.1.10:/mnt/wd2tb/faros`, matching
  the copyable command (now run on the Pi, not the Mac). Saved destinations
  are preserved and can be changed in the Transfer page. The default used to be
  `faros@192.168.1.10:/mnt/cvat-external/wd-images/Faros-OTA/boxed/`; an
  install that saved that value keeps using it until you change the
  destination in the Transfer page (or delete `.rsync-destination` while idle).
  No unit file or environment change is required. If you use a reverse proxy,
  forward the new `/api/transfer/rsync` routes.
- On startup, `.library.sqlite3` is created inside the media directory. Existing
  completed sidecars are imported **in the background**. The Library labels
  results/counts incomplete until indexing finishes; do not interpret an early
  zero as missing files. Leave the service running for the initial import.
  Later starts reuse persisted entries and reconcile changes; new captures and
  app deletions update the catalog immediately. External file edits reconcile
  on restart. There is no separate operator-run migration.
- The catalog is rebuildable from JSON sidecars. If it must be rebuilt, stop the
  service first, then remove **only** `.library.sqlite3` and its `-wal`/`-shm`
  companions. Never remove originals or metadata. Leave
  enough disk space for the index and future captures.
- `.session-key` is created with mode 0600 in the media directory. Sessions last
  seven days and survive restarts. The first update requires signing in again;
  changing the password also invalidates sessions. Keep the key private and
  retain it across updates. If it is invalid, stop the service before removing
  only that key to regenerate it (this logs out all browsers).
- The preview uses short sequential `/api/preview` JPEG requests instead of a
  long-lived browser MJPEG connection. Missing/stale frames show an error rather
  than a LIVE badge. Pending legacy clip recovery also runs in the background.
- `GET /api/activity` (authenticated) reports whether stopping the service would
  interrupt anything: `{"idle": true, "operations": {}}`, or `"idle": false` with
  counts such as `capture_sequence`, `photo_capture`, `rsync_transfer`,
  `deletion`, `download`, `zip_download`, `library_indexing`,
  `clip_recovery`, or `power_action`. `scripts/check-idle.py` wraps it together
  with the systemd state for step 1. If using a reverse proxy, forward the route.
- The example systemd unit now has a Python-only `ExecCondition` build check.
  Install that directive while preserving device-specific user, paths, port,
  environment and other settings (step 5). Build-time Node/npm, `curl` and
  `xz-utils` are newly required; no Python dependency or environment-variable
  migration is needed. The existing venv and media remain in place.
- The header and locked screen identify Git HEAD at backend startup, not dirty
  working-tree contents or a later checkout change. Deploy committed source,
  build it, restart, and reload tabs together. An unavailable Git checkout
  displays `unknown`.

## Update steps

1. **Confirm CameraDeck is idle or stopped, then stop it.** On the Pi:

   ```bash
   cd /home/pi/cameradeck
   python3 scripts/check-idle.py && sudo systemctl stop cameradeck
   ```

   The script checks the `cameradeck` service state and, if it is running,
   signs in with `CAMERADECK_PASSWORD` (from the environment or `.env`) and
   queries `GET /api/activity` on `http://127.0.0.1:80`. It exits 0 and prints
   `idle` or `stopped` when it is safe to continue; 1 with `busy:` and the
   operations in progress; 2 when the service is restarting (`activating`/
   `auto-restart`), `failed`, unreachable, or too old to report activity. Only
   continue on exit 0; otherwise wait and re-run it later. Chaining the stop
   leaves no window for a capture or transfer to start in between. A failed
   service may be restarted by systemd, so investigate it rather than updating
   over it. For a manual run on another port, pass `--url http://127.0.0.1:PORT`
   and stop it with Ctrl+C once the check passes. Releases that predate
   `/api/activity` can only be checked manually: in the web UI confirm no capture
   is running, the Transfer page shows no running rsync, and no Library deletion
   or download is in progress.

2. **Make sure it is stopped.** If step 1 printed `idle`, the chained command
   stopped the service.

   Stopping while idle loses nothing. It also stops systemd from auto-restarting CameraDeck against
   a half-updated checkout. Confirm nothing is left running:

   ```bash
   systemctl is-active cameradeck   # should print inactive
   pgrep -af 'app\.py'              # should print nothing
   ```

   Don't reboot the Pi until step 6. The service is enabled at boot and
   would start against a partly updated checkout.

   **Back up the running version's bundle before Git removes tracked output.**
   Keep this directory outside the checkout until the update is verified:

   ```bash
   backup=$(mktemp -d "$HOME/cameradeck-rollback.XXXXXX")
   git rev-parse HEAD > "$backup/revision"
   cp -a static/dist "$backup/dist"
   printf 'Rollback backup: %s\n' "$backup"
   ```

   If the backup fails, do not pull. This also protects against an interrupted
   installation. Record the printed path; do not delete or move media, `.env`,
   `.session-key`, `.rotation`, `.time_offset`, or either SQLite ledger.

3. **Fetch and apply the new code.**

   ```bash
   cd /home/pi/cameradeck   # or wherever you cloned it
   git status               # confirm there are no local edits you need to keep
   git pull origin main
   ```

   If you have local modifications you want to keep, `git stash` before
   pulling and `git stash pop` after.

4. **Re-sync dependencies and build the frontend on the Pi.** Install the
   pinned Node/npm toolchain from README's "Pinned frontend toolchain" if not
   present (`sudo apt install curl xz-utils` supplies the download tools).
   Run as the service user, never with sudo npm:

   ```bash
   export PATH="$HOME/.local/share/cameradeck/node-$(cat .node-version)/bin:$PATH"
   uv sync --frozen --no-dev && python3 scripts/frontend.py build
   ```

   **Continue only if the entire command succeeds.** On any failure leave the
   service stopped, fix the toolchain/network/disk/source problem and retry, or
   roll back below. Do not start, restart or reboot into a half-updated checkout.
   `npm ci` installs the lockfile including build-time dev dependencies even
   with `NODE_ENV=production`. Build output is staged beside `static/dist`, then
   replaces it only after success; the old bundle is preserved on npm/build
   failure. The first migration's old tracked files may already be removed by
   Git, which is why step 2's separate backup is mandatory.

   `--no-dev` leaves out the test and formatting tools, and removes them if
   an earlier sync installed them. Keep using the existing
   `--system-site-packages` venv (created once at install time) so
   Picamera2/libcamera keep resolving to Raspberry Pi OS's apt-installed
   versions. NumPy, PyAV and the other Python dependencies are installed into
   the venv at the locked versions and take precedence over any apt copies.
   Because the locked NumPy is 2.x, the apt camera stack's compiled modules
   (such as `python3-simplejpeg`, used by Picamera2's JPEG encoder) must be
   built for NumPy 2, as on Raspberry Pi OS based on Debian 13 "Trixie". On
   Bookworm, whose apt NumPy is 1.24, they fail to import. Only recreate the
   venv (`uv venv --system-site-packages`) if `uv sync` reports it is missing
   or broken.

5. **Check for changed deployment files.** `deploy/cameradeck.service` and
   `deploy/90-cameradeck-power.rules` are not automatically re-installed.
   Diff them against the installed copies and re-copy if they changed:

   ```bash
   diff deploy/cameradeck.service /etc/systemd/system/cameradeck.service
   diff deploy/90-cameradeck-power.rules /etc/polkit-1/rules.d/90-cameradeck-power.rules
   ```

   Preserve installed paths/usernames, HTTP port (80 on `pi4`), environment,
   credentials and other device settings. Do not blindly overwrite a customized
   unit. For this migration, add this directive under `[Service]` in the installed
   unit (adjust only the checkout path if necessary):

   ```ini
   ExecCondition=/usr/bin/python3 /home/pi/cameradeck/scripts/frontend.py check
   ```

   It returns nonzero without starting the service if the bundle is invalid;
   unlike a failing pre-start command, an `ExecCondition` exit 1 does not trigger
   the unit's restart loop. `app.py` also checks before opening the camera, so
   manually launched instances cannot bypass the build requirement.
   If replacing files, merge site-specific settings into the candidate first:

   ```bash
   sudoedit /etc/systemd/system/cameradeck.service
   sudo cp deploy/90-cameradeck-power.rules /etc/polkit-1/rules.d/
   sudo chown root:root /etc/polkit-1/rules.d/90-cameradeck-power.rules
   sudo chmod 644 /etc/polkit-1/rules.d/90-cameradeck-power.rules
   sudo systemctl daemon-reload
   ```

6. **Start it back up only after step 4 succeeded.** Recheck the build immediately
   before restarting:

   ```bash
   python3 scripts/frontend.py check && sudo systemctl restart cameradeck
   journalctl -u cameradeck -f
   ```

   For a manual run, export `CAMERADECK_PASSWORD` in the current shell
   first (see README's "Access from your phone"; `.env` is only read by the
   service), then run `python3 scripts/frontend.py check && uv run --no-dev python app.py --host 0.0.0.0`
   (without `--no-dev`, `uv run` installs the dev tools again).

7. **Verify.** Open the web UI, sign in, reload `/library`, and wait for indexing
   to finish. Confirm the count, pagination, preview, and measured exposure.
   Take a test photo, then perform a short 5/s run while browsing the Library;
   stop it and inspect completed count, achieved rate and skipped frames. A
   configured target is not a hardware throughput guarantee. Confirm the locked
   screen/header's Commit label and `/api/session` revision equal
   `git rev-parse --short=12 HEAD`, and `git status --short` shows no source edits
   (the local bundle is ignored). Check authenticated `/api/status`, preview,
   direct `/library` and `/transfer` reloads, the six camera readings, and the
   rsync command. Test destructive actions only with disposable captures after
   verifying backups. Keep the rollback backup until these checks pass.
   Interrupted legacy clip errors preserve the
   original files; see README's "Library and storage".

## Rolling back

If the update causes a regression:

1. If the service is running, check that it is idle and stop it as in step 1.
2. Switch to the last known-good commit:
   ```bash
   git log --oneline -10            # find the last known-good commit
   git checkout <previous-commit-or-tag>
   uv sync --frozen --no-dev
   ```
3. For releases with `scripts/frontend.py`, install their pinned Node/npm,
   then run `python3 scripts/frontend.py build`. If offline, restore the saved
   bundle only with its exact saved source revision and require
   `python3 scripts/frontend.py check` to pass. For a pre-migration release,
   Git restores its tracked bundle; the backup is also available at
   `$backup/dist`. Remove the new `ExecCondition` if that older checkout lacks
   `scripts/frontend.py`.
4. Repeat step 5 so the installed unit and polkit rule match the rolled-back
   checkout, preserving device settings; run `sudo systemctl daemon-reload`.
5. Start only after the matching dependencies and bundle are restored. New
   releases use step 6; older releases without the check script use
   `sudo systemctl restart cameradeck`. Verify the live UI/API and preview again.

`media/`, `.env`, `.library.sqlite3`, and `.session-key` are
untouched by checking out a different commit, so no data is lost by rolling back.
Older releases ignore the new catalog/key and use their original library/login
behavior. The compiled frontend must match the Python revision; a Git checkout
alone no longer restores the bundle for new releases. The checkout is now on
a detached commit. To return to the latest code later, run `git checkout main`
and follow the update steps again.
