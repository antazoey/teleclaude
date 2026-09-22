---
name: teleclaude
description: Hand a task to the teleclaude daemon, a Claude Code session on a remote server, and read back its answer. Use when the work is better done on that server (its checkouts, long builds, CI babysitting, disk-heavy jobs), or the user says "ask teleclaude", "have teleclaude do it", or "run it on the server".
---

# teleclaude

The teleclaude daemon is a separate Claude Code session on a server, reached over a chat channel
(Telegram by default). The client sends as the user's own account, so every exchange also shows up
in the user's chat with the daemon.

## Sending

    uv run ~/teleclaude/teleclaude.py ask "<prompt>"

- The final answer is printed to stdout. Progress lines go to stderr as the daemon works.
- Exit code 1 means the daemon went silent for 15 minutes, usually because it is down.
- Long prompts: pipe them on stdin, `uv run ~/teleclaude/teleclaude.py ask <<'EOF' ... EOF`.
- Anything that might run longer than a few minutes: run it with `run_in_background: true`,
  since a foreground Bash call dies at 10 minutes. You are notified when it exits.

## A pasted prompt may arrive split

Telegram caps a message at 4096 characters and splits longer pastes. The daemon coalesces messages
that arrive within a moment of each other into one prompt, so send the whole thing. Commands and
stop requests skip that buffer and act immediately.

## Writing the prompt

The daemon sees none of this conversation. Give it everything it needs: the repo path on the
server, the branch, what done looks like, and exactly what to report back. Ask for a short answer,
since the whole reply comes back as text.

## Daemon commands

Slash commands go through the same `ask` and print the daemon's reply.

- `/pwd` shows its directory, session, whether it is busy, and the timeout state.
- `/cd <path>` moves it and starts a fresh conversation. Do this before a prompt that needs a repo.
- `/new`, `/stop`, `/upgrade`, `/timeout off`, `/model`: only when the user asks. The user's phone
  shares this conversation, so these interrupt or reset whatever they are doing.

The daemon gives up on a turn that stops making progress: 15 minutes normally, an hour while it
waits on CI, builds, or a watch loop.

## When `ask` fails with a config or login error

Tell the user, do not attempt it yourself. They need `~/.config/teleclaude/config.toml` filled in
from `config.example.toml`, then `uv run ~/teleclaude/teleclaude.py login` once in a real terminal.
