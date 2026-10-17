# Agent instructions

- All typed confirmation prompts must accept case-insensitive input.
- See `UPDATE.md` for how to update an already-deployed checkout of this
  software on its Raspberry Pi target.
- After changing CameraDeck software on a device where CameraDeck is already
  installed and running, update that running instance as part of the task.
  Source edits, builds, commits, or pushes alone do not finish the update. This
  instruction authorizes the local deployment and service restart; no separate
  confirmation is needed for an idle instance.
- This applies specifically to this Raspberry Pi: hostname `pi4`, checkout
  `/home/pi/cameradeck`, systemd service `cameradeck`, HTTP port `80`. Follow
  `UPDATE.md`, deploy matching backend and compiled frontend, preserve the
  installed service's device-specific settings, and verify the live service,
  authenticated UI/API, and preview after updating.
- Only update the running instance while it is **idle** or **stopped**. Idle
  means nothing that stopping the service would interrupt: no photo capture or
  capture sequence, rsync transfer, S3 upload, deletion, single or ZIP download,
  library indexing, clip recovery, or pending shutdown/reboot. Determine this
  with `python3 scripts/check-idle.py` (exit 0 = idle or stopped, 1 = busy,
  2 = restarting, failed, unreachable, or undeterminable) and stop in the same
  command so nothing can start in between:
  `python3 scripts/check-idle.py && sudo systemctl stop cameradeck`.
  Proceed only on exit 0. A `failed` service or one that is activating/
  auto-restarting is not stopped: systemd may restart it, so don't update it.
  Never infer idleness from the UI, logs, or `/api/status` alone, and never
  stop or cancel someone else's operation to make the instance idle. If the
  check does not pass, leave the instance untouched, finish the source changes,
  and report the update as pending (with the script's output). Preserve media,
  credentials, session keys, and ledgers.
- When you make a change that affects how the software is deployed or
  updated on the Pi — dependencies, the `uv`/venv setup, `deploy/` files
  (`cameradeck.service`, `90-cameradeck-power.rules`), required system
  packages, environment variables, migrations, or anything else an operator
  would need to do differently when updating an existing install — update
  `UPDATE.md` to match as part of that change.
