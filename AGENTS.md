# Agent instructions

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
- Never interrupt an active capture to update. Check live status before stopping
  or restarting; if capture is active, leave it running and report the update as
  pending until it is idle. Preserve media, credentials, session keys, and ledgers.
- When you make a change that affects how the software is deployed or
  updated on the Pi — dependencies, the `uv`/venv setup, `deploy/` files
  (`cameradeck.service`, `90-cameradeck-power.rules`), required system
  packages, environment variables, migrations, or anything else an operator
  would need to do differently when updating an existing install — update
  `UPDATE.md` to match as part of that change.
