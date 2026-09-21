# Agent instructions

- See `UPDATE.md` for how to update an already-deployed checkout of this
  software on its Raspberry Pi target.
- When you make a change that affects how the software is deployed or
  updated on the Pi — dependencies, the `uv`/venv setup, `deploy/` files
  (`cameradeck.service`, `90-cameradeck-power.rules`), required system
  packages, environment variables, migrations, or anything else an operator
  would need to do differently when updating an existing install — update
  `UPDATE.md` to match as part of that change.
