# /// script
# requires-python = ">=3.11"
# dependencies = ["brotli>=1.1", "telethon>=1.36"]
# ///
"""teleclaude client: send a prompt or /command to the daemon and print its answer."""

import argparse
import asyncio
import secrets
import sys

import protocol
from channels import SENDERS, find_channel
from config import Config

# Longer than the daemon's slowest heartbeat.
SILENCE_LIMIT_SECONDS = 15 * 60
EXIT_NO_ANSWER = 1


async def collect_answer(sender, request_id, on_progress, silence_limit=SILENCE_LIMIT_SECONDS):
    """The daemon's final reply to one request, relaying its progress lines along the way."""
    reassembler = protocol.Reassembler()
    while True:
        message = await sender.next_message(silence_limit)
        if message is None:
            return None

        try:
            envelope = protocol.decode(message)
        except ValueError:
            continue

        if envelope is None or envelope.request_id != request_id:
            continue

        envelope = reassembler.add(envelope)
        if envelope is None:
            continue

        if envelope.kind == protocol.FINAL:
            return envelope.text

        on_progress(envelope.text)


def print_progress(text):
    print(text, file=sys.stderr, flush=True)


async def ask(sender, text):
    request_id = secrets.token_hex(4)
    await sender.open()
    try:
        for message in protocol.encode_parts(request_id, text, None, sender.max_message_chars):
            await sender.send(message)

        return await collect_answer(sender, request_id, print_progress)
    finally:
        await sender.close()


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("login", help="one-time interactive setup for the configured channel")
    ask_parser = actions.add_parser("ask", help="send a prompt or /command and print the reply")
    ask_parser.add_argument("text", nargs="*", help="what to send; read from stdin when omitted")
    arguments = parser.parse_args(argv)

    config = Config.load()
    sender = find_channel(SENDERS, config).from_config(config)
    if arguments.action == "login":
        asyncio.run(sender.login())
        return

    text = " ".join(arguments.text).strip() or sys.stdin.read().strip()
    if not text:
        parser.error("nothing to send")

    answer = asyncio.run(ask(sender, text))
    if answer is None:
        print_progress(f"No answer after {SILENCE_LIMIT_SECONDS // 60}m of silence. Is the daemon up?")
        sys.exit(EXIT_NO_ANSWER)

    print(answer)


if __name__ == "__main__":
    main(sys.argv[1:])
