#!/usr/bin/env python3
"""Copy a Claude transcript from this machine onto the one running the teleclaude daemon."""

import json
import shlex
import subprocess
import sys
from pathlib import Path

from config import Config

PROJECTS_DIR = Path.home() / ".claude/projects"
LISTING_LIMIT = 15


def recent_transcripts(limit=LISTING_LIMIT):
    paths = sorted(PROJECTS_DIR.glob("*/*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
    return paths[:limit]


def find_transcript(prefix):
    return [path for path in PROJECTS_DIR.glob("*/*.jsonl") if path.stem.startswith(prefix)]


def transcript_cwd(text):
    """The working directory the conversation was pinned to."""
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        if record.get("cwd"):
            return record["cwd"]

    return None


def remote_home(host):
    result = subprocess.run(["ssh", host, 'printf %s "$HOME"'], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def run_remote(host, command):
    subprocess.run(["ssh", host, command], check=True)


def push(path, host):
    local_home = str(Path.home())
    remote = remote_home(host)
    text = path.read_text(errors="replace")

    cwd = transcript_cwd(text)
    if not cwd:
        sys.exit(f"No cwd recorded in {path.name}")

    remote_cwd = cwd.replace(local_home, remote)
    remote_dir = f"{remote}/.claude/projects/{remote_cwd.replace('/', '-')}"
    remote_path = f"{remote_dir}/{path.name}"

    run_remote(host, f"mkdir -p {shlex.quote(remote_dir)}")
    subprocess.run(
        ["ssh", host, f"cat > {shlex.quote(remote_path)}"],
        input=text.replace(local_home, remote).encode(),
        check=True,
    )

    missing = subprocess.run(
        ["ssh", host, f"test -d {shlex.quote(remote_cwd)} || echo missing"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    if missing:
        print(f"warning: {remote_cwd} does not exist on {host}, the bot will resume without the repo")

    print(f"pushed {path.stem} to {host}:{remote_path}")
    print("\nsend the daemon:")
    print(f"  /cd {remote_cwd}")
    print(f"  /resume {path.stem}")


def main(argv):
    host = Config.load().get("client.ssh_host", "TELECLAUDE_HOST")
    arguments = list(argv)
    if "--to" in arguments:
        index = arguments.index("--to")
        host = arguments[index + 1]
        del arguments[index : index + 2]

    if not arguments:
        for path in recent_transcripts():
            print(f"{path.stem}  {path.parent.name}")

        return

    matches = find_transcript(arguments[0])
    if not matches:
        sys.exit(f"No transcript starting with {arguments[0]}")

    if len(matches) > 1:
        sys.exit("Ambiguous, matches: " + ", ".join(path.stem for path in matches))

    if not host:
        sys.exit("No daemon host. Set client.ssh_host in the config or pass --to <host>.")

    push(matches[0], host)


if __name__ == "__main__":
    main(sys.argv[1:])
