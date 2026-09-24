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
   uv sync --frozen
   ```
   Keep using the existing `--system-site-packages` venv (created once at
   install time) so Picamera2/libcamera/NumPy/PyAV keep resolving to
   Raspberry Pi OS's apt-installed versions rather than PyPI wheels. Only
   recreate the venv (`uv venv --system-site-packages`) if `uv sync` reports
   it is missing or broken.

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
   service), then run `uv run python app.py --host 0.0.0.0`.

7. **Verify.** Open the web UI, confirm the camera initializes, take a test
   still, and check `git log -1` matches what you expect. If the UI shows an
   error about an interrupted clip (for example, from an earlier crash), see
   README's "Storage and interrupted sessions".

## Rolling back

If the update causes a regression:

1. If the web UI is reachable, confirm the camera is idle as in step 1.
2. Stop CameraDeck and switch to the last known-good commit:
   ```bash
   sudo systemctl stop cameradeck   # or Ctrl+C a manual run
   git log --oneline -10            # find the last known-good commit
   git checkout <previous-commit-or-tag>
   uv sync --frozen
   ```
3. Repeat step 5 so the installed unit and polkit rule match the
   rolled-back checkout (including `sudo systemctl daemon-reload`).
4. Start it again as in step 6.

`media/`, `.env`, and `.uploads.sqlite3` are untouched by checking out a
different commit, so no data is lost by rolling back. The checkout is now on
a detached commit. To return to the latest code later, run `git checkout main`
and follow the update steps again.
