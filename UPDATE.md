# Updating CameraDeck on the Raspberry Pi

These are the steps to update an already-deployed CameraDeck checkout on a
Raspberry Pi to the latest code. See `README.md` for first-time installation.

## Before you start

- Only update while the camera is idle: no video recording and no capture
  sequence (periodic stills, camera test, road experiment) active. Step 1
  covers how to check. Recordings and periodic stills keep running until
  someone stops them, even after every browser disconnects, so if one is
  active, leave it to whoever started it and update once they've stopped it.
  Don't stop someone else's run just to update.
- Note whether CameraDeck runs as the `cameradeck` systemd service or is
  started manually (`uv run python app.py`). Use the matching stop/start
  steps below.
- Never delete `.uploads.sqlite3` (the S3 upload ledger) or the `media/`
  directory during an update. Both live outside the git checkout's tracked
  files and are left alone by `git pull`.
- `.env` (password, S3 credentials/endpoint) is not tracked in git and is
  not touched by an update.
- Mounting orientation now persists in `.rotation` inside the configured
  media directory. After updating an older install, apply your preferred
  orientation once while idle to save it. Keep this file when backing up or
  moving the media directory; no migration or service changes are needed.

## Photo-only release

- Capture is now photo-only, defaulting to **5 photos/second until stopped**.
  Video recording, tests/experiments, separate-still sequences, mode selection,
  Help, and focus scoring are removed. Existing media, pending clips, experiment
  directories, `.rotation`, `.time_offset`, and the S3 ledger are retained.
  Old clients/scripts using removed routes must be updated. Stop old runs before
  installing this release; never assume closing the browser stopped them.
- Scripts supplying temporary burst controls to `/api/sequence/start` now have
  restoration validated before capture starts or camera settings change.
  Unrestorable overrides are rejected. Controls with no usable default may
  require setting a persistent value through `/api/controls` first so the burst
  can restore it. Restoration of automatic exposure/white balance remains
  supported. Normal UI capture is unchanged.
- The React/React Router frontend is compiled into tracked `static/dist/`.
  A normal update needs **no Node installation or frontend build on the Pi**.
  When changing frontend source, use Node 22+ on a build machine:
  `npm --prefix frontend ci && npm --prefix frontend run build`, and ship the
  source, lockfile and compiled files together. The service still runs Python only.
- Flask must serve `/`, `/library`, `/transfer`, `/assets/*`, `/api/*`, and `/media/*` on the
  same origin. If using a reverse proxy, forward the new routes too; avoid
  caching app HTML/API responses. Hashed assets can be cached. Direct `/library`
  and `/transfer` reloads must reach Flask, not a proxy filesystem lookup.
- Transfer tools have moved off the Library page. `/transfer` shows the Mac
  rsync command first; optional S3 upload is collapsed until opened. For rsync,
  stop capture first and exclude hidden internal files (`--exclude='.*'`),
  previews, thumbnails, and temporary/pending sidecars as shown in `README.md`.
  Never copy the private `.session-key` to a shared destination. `--ignore-existing`
  does not repair previously copied incomplete files.
- On startup, `.library.sqlite3` is created inside the media directory. Existing
  completed sidecars are imported **in the background**. The Library labels
  results/counts incomplete until indexing finishes; do not interpret an early
  zero as missing files. Leave the service running for the initial import.
  Later starts reuse persisted entries and reconcile changes; new captures and
  app deletions update the catalog immediately. External file edits reconcile
  on restart. There is no separate operator-run migration.
- The catalog is rebuildable from JSON sidecars. If it must be rebuilt, stop the
  service first, then remove **only** `.library.sqlite3` and its `-wal`/`-shm`
  companions. Never remove `.uploads.sqlite3`, originals, or metadata. Leave
  enough disk space for the index and future captures.
- `.session-key` is created with mode 0600 in the media directory. Sessions last
  seven days and survive restarts. The first update requires signing in again;
  changing the password also invalidates sessions. Keep the key private and
  retain it across updates. If it is invalid, stop the service before removing
  only that key to regenerate it (this logs out all browsers).
- The preview uses short sequential `/api/preview` JPEG requests instead of a
  long-lived browser MJPEG connection. Missing/stale frames show an error rather
  than a LIVE badge. Pending legacy clip recovery also runs in the background.
- No deployment unit, system package, Python dependency, or environment-variable
  change is required for this release.

## Update steps

1. **Confirm the camera is idle.** Open the web UI and check that nothing is
   recording and no capture sequence is running. If the UI has just
   reconnected, wait until status loads before deciding. Don't continue
   until the camera is idle.

2. **Stop the running instance.**

   ```bash
   sudo systemctl stop cameradeck   # if installed as a service
   # or Ctrl+C the manually-run `uv run python app.py` process
   ```

   Stopping while idle loses nothing, and a graceful stop finalizes any
   recording. It also stops systemd from auto-restarting CameraDeck against
   a half-updated checkout. Confirm nothing is left running:

   ```bash
   systemctl is-active cameradeck   # should print inactive
   pgrep -af 'app\.py'              # should print nothing
   ```

   Don't reboot the Pi until step 6. The service is enabled at boot and
   would start against a partly updated checkout.

3. **Fetch and apply the new code.**

   ```bash
   cd /home/pi/cameradeck   # or wherever you cloned it
   git status               # confirm there are no local edits you need to keep
   git pull origin main
   ```

   If you have local modifications you want to keep, `git stash` before
   pulling and `git stash pop` after.

4. **Re-sync dependencies.**

   ```bash
   uv sync --frozen --no-dev
   ```

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

   If either differs (review the diff — paths/usernames may be
   site-specific), re-install it:

   ```bash
   sudo cp deploy/cameradeck.service /etc/systemd/system/
   sudo cp deploy/90-cameradeck-power.rules /etc/polkit-1/rules.d/
   sudo chown root:root /etc/polkit-1/rules.d/90-cameradeck-power.rules
   sudo chmod 644 /etc/polkit-1/rules.d/90-cameradeck-power.rules
   sudo systemctl daemon-reload
   ```

6. **Start it back up.**

   ```bash
   sudo systemctl restart cameradeck
   journalctl -u cameradeck -f
   ```

   For a manual run, export `CAMERADECK_PASSWORD` in the current shell
   first (see README's "Access from your phone"; `.env` is only read by the
   service), then run `uv run --no-dev python app.py --host 0.0.0.0`
   (without `--no-dev`, `uv run` installs the dev tools again).

7. **Verify.** Open the web UI, sign in, reload `/library`, and wait for indexing
   to finish. Confirm the count, pagination, preview, and measured exposure.
   Take a test photo, then perform a short 5/s run while browsing the Library;
   stop it and inspect completed count, achieved rate and skipped frames. A
   configured target is not a hardware throughput guarantee. Check `git log -1`
   matches the intended revision. Interrupted legacy clip errors preserve the
   original files; see README's "Library and storage".

## Rolling back

If the update causes a regression:

1. If the web UI is reachable, confirm the camera is idle as in step 1.
2. Stop CameraDeck and switch to the last known-good commit:
   ```bash
   sudo systemctl stop cameradeck   # or Ctrl+C a manual run
   git log --oneline -10            # find the last known-good commit
   git checkout <previous-commit-or-tag>
   uv sync --frozen --no-dev
   ```
3. Repeat step 5 so the installed unit and polkit rule match the
   rolled-back checkout (including `sudo systemctl daemon-reload`).
4. Start it again as in step 6.

`media/`, `.env`, `.uploads.sqlite3`, `.library.sqlite3`, and `.session-key` are
untouched by checking out a different commit, so no data is lost by rolling back.
Older releases ignore the new catalog/key and use their original library/login
behavior. The compiled frontend must match the Python revision; a Git checkout
restores both tracked parts. The checkout is now on
a detached commit. To return to the latest code later, run `git checkout main`
and follow the update steps again.
