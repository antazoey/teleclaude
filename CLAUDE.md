# teleclaude

When a message arrives over a channel, this is the daemon you are running inside.

- Changes to `daemon.py`, `channels.py`, `protocol.py`, `config.py` or `teleclaude.py` need a matching test.
- Never restart the daemon yourself. Editing the files is the whole job.
- Tell the user to send `/upgrade` when the edit is ready. That runs the tests, preflights the new
  code under `uv` so new dependencies resolve, and restarts onto it.
- Secrets and machine-specific values (hosts, bot names, paths) belong in the config, never in code.
- Never bump `VERSION` by hand on a fix. `/upgrade` auto-bumps the patch on deploy (only when the tree has changes), so the version tracks deploys, not edits; the boot greeting reports it.
- A commit is not a deploy. To know what is actually live, read the deploy log (`~/.config/teleclaude/deploy.log`, one `<utc> vX` line per boot) or `journalctl -u teleclaude | grep listening`; the working tree's `VERSION` must never sit below the live version.
- Do NOT commit or push on edit. Leave changes in the working tree. The commit + push to `origin/main` happens only when the user runs `/upgrade` (that is the sole trigger), so GitHub tracks each version that actually goes live.
