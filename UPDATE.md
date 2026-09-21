# Updating CameraDeck on the Raspberry Pi

These are the steps to update an already-deployed CameraDeck checkout on a
Raspberry Pi to the latest code. See `README.md` for first-time installation.

## Before you start

- **Precondition: no running instance of CameraDeck.** Only update while
  CameraDeck is not running — check whether the `cameradeck` systemd
  service is active (`systemctl is-active cameradeck`) or a manual
  `uv run python app.py` process is running, and wait until it isn't
  before proceeding. Do not stop the service or interrupt a manually-run
  process to force this condition, and never stop an in-progress
  recording or capture sequence — let it finish or end on its own first.
- Note whether CameraDeck runs as the `cameradeck` systemd service or is
  started manually (`uv run python app.py`). Use the matching start step
  below once CameraDeck is confirmed not running.
- Never delete `.uploads.sqlite3` (the S3 upload ledger) or the `media/`
  directory during an update. Both live outside the git checkout's tracked
  files and are left alone by `git pull`.
- `.env` (password, S3 credentials/endpoint) is not tracked in git and is
  not touched by an update.

## Update steps

1. **Confirm no running instance.**
   ```bash
   systemctl is-active cameradeck   # if installed as a service
   # or check for a running `uv run python app.py` process
   ```
   Proceed only once this shows CameraDeck is not running.

2. **Fetch and apply the new code.**
   ```bash
   cd /home/pi/cameradeck   # or wherever you cloned it
   git status               # confirm there are no local edits you need to keep
   git pull origin main
   ```
   If you have local modifications you want to keep, `git stash` before
   pulling and `git stash pop` after.

3. **Re-sync dependencies.**
   ```bash
   uv sync --frozen
   ```
   Keep using the existing `--system-site-packages` venv (created once at
   install time) so Picamera2/libcamera/NumPy/PyAV keep resolving to
   Raspberry Pi OS's apt-installed versions rather than PyPI wheels. Only
   recreate the venv (`uv venv --system-site-packages`) if `uv sync` reports
   it is missing or broken.

4. **Check for changed deployment files.** `deploy/cameradeck.service` and
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

5. **Start it back up.**
   ```bash
   sudo systemctl start cameradeck
   journalctl -u cameradeck -f
   ```
   or, for a manual run: `uv run python app.py --host 0.0.0.0`.

6. **Verify.** Open the web UI, confirm the camera initializes, take a test
   still, and check `git log -1` matches what you expect.

## Rolling back

If the update causes a regression, wait until CameraDeck is not running
(per the precondition above), then:
```bash
git log --oneline -10            # find the last known-good commit
git checkout <previous-commit-or-tag>
uv sync --frozen
sudo systemctl start cameradeck  # or run manually
```
`media/`, `.env`, and `.uploads.sqlite3` are untouched by checking out a
different commit, so no data is lost by rolling back.
