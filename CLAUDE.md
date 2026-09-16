# teleclaude

When a message arrives over a channel, this is the daemon you are running inside.

- Changes to `daemon.py`, `channels.py`, `protocol.py`, `config.py` or `teleclaude.py` need a matching test.
- Never restart the daemon yourself. Editing the files is the whole job.
- Tell the user to send `/upgrade` when the edit is ready. That runs the tests, preflights the new
  code under `uv` so new dependencies resolve, and restarts onto it.
- Secrets and machine-specific values (hosts, bot names, paths) belong in the config, never in code.
- Every upgrade bumps `VERSION` in `daemon.py` (semver, patch by default); the boot greeting reports it so a finished `/upgrade` is visible.
- Every upgrade is committed and pushed to `origin/main` so GitHub tracks each running version.
