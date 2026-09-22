# /// script
# requires-python = ">=3.11"
# dependencies = ["brotli>=1.1", "faster-whisper>=1.1", "httpx>=0.27", "pygments>=2.17", "text-unicoder>=1.3"]
# ///
"""teleclaude daemon: runs Claude Code on this machine and relays each turn over a channel."""

import asyncio
import calendar
import hashlib
import hmac
import io
import json
import os
import random
import re
import shutil
import sys
import time
import urllib.parse
from collections import deque
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import NamedTuple

import httpx

import protocol
from channels import Inbound, RECEIVERS, find_channel
from config import Config

VERSION = "0.0.8"
WORKING_PREFIX = "⚙️ "
DEPLOY_LOG = Path.home() / ".config/teleclaude/deploy.log"
SASSY_RESPONSES = (
    "ok! Let me get started…",
    "thinking…",
    "your request has been forwarded to my brain! Please standby.",
    "ugh… i was sleeping! Just kidding, on it.. ",
    "do it yourself! Ugh, fine…",
    "i just LOVE when you tell me what to do",
    "you're the captain, i will oblige…",
)
GENTLE_RESPONSES = (
    "on it, love. hang tight.",
    "okay, i've got you. let me look.",
    "of course. give me a sec with this one.",
    "yeah, let me dig into this carefully.",
    "i'm on it, take a breath.",
)
NEUTRAL_RESPONSES = (
    "ok, on it.",
    "let me take a look.",
    "one sec.",
)
QUEUED_RESPONSES = (
    "I put your request in the queue! It's {pos} in line to my brain.",
    "added to the queue — you're {pos} in line.",
    "still on the last one; this makes you {pos} in line.",
    "queued! {pos} in line to my brain, hang tight.",
)
TPLUS_MARKERS = re.compile(
    r"\b(tplus|pull request|pr review|code review|reviews?|gh-dash|serge|rebase|clippy|utoipa|orderbook|cargo)\b"
    r"|\bpr\b|merge main|catch up with main|github\.com/\S*tplus",
    re.I,
)
SPYWEAR_MARKERS = re.compile(
    r"\b(soulaire|soulbrai|brai|spywear|spies|soulful|cult|police|authorit\w*|casia|cece|fai|gemma"
    r"|lorawai|bigfoot|katie|keonna|amber|larai|laira|hiavus|june|mission|glasses|edpa|nutraceutical"
    r"|clinic|survivors?|evidence|transcript|documentary|alexia)\b",
    re.I,
)
SCRIPT_PATH = Path(__file__).resolve()
PROJECT_DIR = SCRIPT_PATH.parent
ENV_PATH = PROJECT_DIR / ".env"
HTTP_TIMEOUT_SECONDS = 65
# One claude event is one line, and a large tool result overruns asyncio's 64 KiB default.
STREAM_LINE_LIMIT = 64 * 1024 * 1024
STREAM_CHUNK_BYTES = 1024 * 1024
DIFF_LINK_TTL_SECONDS = 12 * 3600
HEARTBEAT_SECONDS = 60
HEARTBEAT_TICK_SECONDS = 1
# Telegram splits a long message into several updates that arrive together; gather
# everything within this window into one prompt, and only split on a real pause.
COALESCE_WINDOW_SECONDS = 1.5
# A folded turn answers several prompts with one result, orphaning the rest of the queue.
IDLE_DRAIN_SECONDS = 15
PATIENT_HEARTBEAT_SECONDS = 600
STALL_TIMEOUT_SECONDS = 15 * 60
PATIENT_STALL_TIMEOUT_SECONDS = 60 * 60
# Recent tool calls and remarks a new one must differ from to count as progress.
PROGRESS_WINDOW = 8
# Words that mark a prompt or tool call as waiting on something slow.
PATIENT_HINTS = ("monitor", "watch", "poll", "wait for", "waiting for", "keep an eye", "until it finishes", "until it passes", "ci", "pipeline", "deploy", "workflow run", "sleep", "gh pr checks", "cargo build", "cargo test", "nextest", "docker build")
PATIENT_AUTO_WORDS = ("auto", "detect", "reset")
STOP_WORDS = ("stop", "stop it", "stop stop", "stop please", "please stop", "halt", "abort", "cancel", "quit", "enough", "nevermind", "never mind", "thats enough", "that is enough")
STDERR_TAIL_LINES = 40
STDERR_NOTE_CHARS = 300
# stderr lines worth repeating back: the upstream failures claude retries through in silence.
STALL_HINTS = ("429", "500", "502", "503", "529", "overloaded", "rate limit", "quota", "timeout", "econnreset", "socket hang up")
TOOL_DETAIL_KEYS = ("command", "file_path", "path", "pattern", "query", "url", "prompt", "description")
TOOL_DETAIL_CHARS = 160
NARRATION_CHARS = 90
TOOL_PATH_KEYS = ("file_path", "path", "notebook_path")
DOUBLING_VERBS = ("commit", "cut", "debug", "drop", "get", "grep", "log", "map", "plan", "plot", "put", "run", "scan", "set", "ship", "skip", "split", "stop", "swap", "tag", "trim", "wrap")
TOOL_SEARCH_KEYS = ("pattern", "query")
TOOL_VERBS = {
    "Edit": "Editing",
    "MultiEdit": "Editing",
    "NotebookEdit": "Editing",
    "Read": "Reading",
    "Write": "Writing",
}
TOOL_LABELS = {
    "Bash": "Running a command",
    "Glob": "Looking through files",
    "Grep": "Searching the code",
    "Task": "Handing off to a subagent",
    "TodoWrite": "Updating the plan",
    "WebFetch": "Fetching a page",
    "WebSearch": "Searching the web",
}
DEBUG_ON_WORDS = ("on", "1", "true", "yes")
DEBUG_OFF_WORDS = ("off", "0", "false", "no")
PROMPT_ECHO_CHARS = 100
NEW_COMMANDS = ("/new", "/new_context", "/clear", "/reset")
DEFAULT_MODEL_WORDS = ("default", "reset", "none")
MODELS = (
    ("default", "whatever the CLI is configured to use"),
    ("fable", "Claude Fable 5.1, most capable"),
    ("opus", "Claude Opus 5"),
    ("sonnet", "Claude Sonnet 5"),
    ("haiku", "Claude Haiku 4.5, fastest"),
)
DEFAULT_CLAUDE_BIN = Path.home() / ".local/bin/claude"
CLAUDE_PROJECTS_DIR = Path.home() / ".claude/projects"
CLEAN_SHOW_ONLY_WORDS = ("--only-show", "--show", "show")
CLEAN_STALE_HOURS = 25
SHOW_MAX_LINES = 400
SHOW_PATTERN = re.compile(r"^(.*?)(?::(\d+)(?:-(\d+))?)?$")
DEFAULT_UV_BIN = Path.home() / ".local/bin/uv"
DEFAULT_WHISPER_MODEL = "small"
VOICE_NOTE = "The user has sent a voice memo. Here is the transcription: '{transcript}'"
NO_SPEECH_REPLY = "Couldn't make out any speech in that voice memo."
WHISPER_MODELS = {}
TEST_COMMAND = ("run", "--with", "brotli", "--with", "faster-whisper", "--with", "httpx", "--with", "pygments", "--with", "telethon", "--with", "text-unicoder", "--with", "textual", "--with", "pytest", "--with", "pytest-asyncio", "--with", "pytest-mock", "pytest", "-q")
PREFLIGHT_PYTHON_PREFIX = "python: "
CHECK_TIMEOUT_SECONDS = 600
CHECK_OUTPUT_CHARS = 1500
RESTART_KEYS = {
    "chat": "TELECLAUDE_RESTART_CHAT",
    "session": "TELECLAUDE_RESTART_SESSION",
    "cwd": "TELECLAUDE_RESTART_CWD",
    "cursor": "TELECLAUDE_RESTART_CURSOR",
    "debug": "TELECLAUDE_RESTART_DEBUG",
    "model": "TELECLAUDE_RESTART_MODEL",
}
HELP_TEXT = """Send any message and it becomes a Claude Code turn in the current directory.
Messages sent while one is running fire straight away; Claude interleaves them itself.

/new - start a fresh conversation (/clear, /reset, /new_context all work)
/cd <path> - change the working directory
/resume <id> - adopt a session pushed over from another machine
/pwd - show directory, session and busy state
/stop - kill the running turn and drop anything still in flight (plain "stop" works too)
/debug [on|off] - show raw tool input instead of plain descriptions
/model [name] - show the models, or switch to one
/patient [on|off|auto] - long waits get an hour without progress and a 10m nudge
/timeout [on|off] - turn the no-progress timeout off entirely
/diff [path] - open the diff viewer, e.g. /diff org/repo/my-branch
/show <path>[:start[-end]] - send a file or a line range as highlighted code
/clean [--only-show] - show the last cleanup run, then start one
/upgrade - test the edited daemon and restart onto it
/help - this message"""


def diff_viewer_link(base_url, auth_key, path, now=None):
    expires = int(now or time.time()) + DIFF_LINK_TTL_SECONDS
    signature = hmac.new(auth_key.encode(), str(expires).encode(), hashlib.sha256).hexdigest()
    query = urllib.parse.urlencode({"exp": expires, "sig": signature})
    return f"{base_url.rstrip('/')}/{path.strip('/')}?{query}"


def render_models(current):
    """The model list, marking the one in use."""
    active = current or "default"
    rows = [
        f"{'>' if name == active else ' '} {name:<8} {description}"
        for name, description in MODELS
    ]
    if not any(name == active for name, _ in MODELS):
        rows.append(f"> {active:<8} passed straight to the CLI")

    listing = "\n".join(rows)
    return f"Model: {active}\n\n{listing}\n\n/model <name> to switch, or give a full model id."


def read_clean_status(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def gigabytes(megabytes):
    return f"{megabytes / 1024:.1f}G"


def describe_age(seconds):
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"

    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"

    return f"{int(seconds // 86400)}d ago"


def render_clean_status(status, now=None):
    """Summary of the last cleanup run, flagging one that is overdue."""
    if not status:
        return "No cleanup status yet. The cron may never have run."

    finished = status.get("finished_at", "")
    try:
        ran_at = calendar.timegm(time.strptime(finished, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return "Cleanup status has no readable timestamp."

    age = max(0.0, (now if now is not None else time.time()) - ran_at)
    lines = [f"cleanup ran {describe_age(age)} ({finished})"]
    if age > CLEAN_STALE_HOURS * 3600:
        lines.append(f"nothing for over {CLEAN_STALE_HOURS}h -- check the cron")

    available = status.get("disk_available_mb", 0)
    size = status.get("disk_size_mb", 0)
    used_percent = round(100 * status.get("disk_used_mb", 0) / size) if size else 0
    lines.append(
        f"freed {gigabytes(status.get('freed_mb_total', 0))}"
        f" - {gigabytes(available)} free ({used_percent}% used)"
    )

    freed = status.get("freed_mb") or {}
    if any(freed.values()):
        per_repo = ", ".join(
            f"{name} {gigabytes(megabytes)}"
            for name, megabytes in sorted(freed.items())
            if megabytes
        )
        lines.append(f"  {per_repo}")

    if "sync_merged" in status or "sync_conflicts" in status:
        lines.append(
            f"branch sync: {status.get('sync_merged', 0)} merged,"
            f" {status.get('sync_conflicts', 0)} conflicted"
        )

    return "\n".join(lines)


def load_env(path):
    if not path.exists():
        return

    for line in path.read_text().splitlines():
        stripped_line = line.strip()
        if not stripped_line or stripped_line.startswith("#") or "=" not in stripped_line:
            continue

        key, _, value = stripped_line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def summarize(prompt):
    return " ".join(prompt.split())[:PROMPT_ECHO_CHARS]


def format_duration(seconds):
    whole = int(seconds)
    if whole < 60:
        return f"{whole}s"

    if whole < 3600:
        return f"{whole // 60}m{whole % 60:02d}s"

    return f"{whole // 3600}h{whole % 3600 // 60:02d}m"


def is_stop_request(text):
    """A plain-English stop, matched whole so a prompt about stopping something still runs."""
    return re.sub(r"[^a-z ]", "", text.lower()).strip() in STOP_WORDS


def mentions_long_wait(text):
    """Whether the text describes work that waits on something slow, like CI or a build."""
    lowered = text.lower()
    return any(re.search(rf"\b{re.escape(hint)}\b", lowered) for hint in PATIENT_HINTS)


def is_patient_tool(block):
    payload = block.get("input") or {}
    parts = (block.get("name", ""), payload.get("command", ""), payload.get("description", ""))
    return mentions_long_wait(" ".join(str(part) for part in parts))


def progress_fingerprint(block):
    """A comparable identity for a tool call or remark."""
    if block.get("type") == "tool_use":
        return json.dumps([block.get("name"), block.get("input")], sort_keys=True)

    if block.get("type") == "text":
        return block.get("text", "").strip() or None

    return None


def stall_hint(stderr_lines):
    """The most recent stderr line naming an upstream failure, e.g. an API 529."""
    for line in reversed(stderr_lines):
        if any(hint in line.lower() for hint in STALL_HINTS):
            return line.strip()[:STDERR_NOTE_CHARS]

    return None


def condense(value, limit=TOOL_DETAIL_CHARS):
    return " ".join(str(value).split())[:limit]


def to_gerund(word):
    lowered = word.lower()
    if lowered.endswith("ing"):
        return word

    if lowered in DOUBLING_VERBS:
        return f"{word}{word[-1]}ing"

    if lowered.endswith("e") and not lowered.endswith(("ee", "oe", "ye")):
        return f"{word[:-1]}ing"

    return f"{word}ing"


def describe_tool(block):
    name = block.get("name", "tool")
    payload = block.get("input") or {}
    detail = next((condense(payload[key]) for key in TOOL_DETAIL_KEYS if payload.get(key)), "")
    return f"[{name}] {detail}" if detail else f"[{name}]"


def narrate_tool(block):
    """What the tool is doing, phrased for a reader rather than a shell."""
    name = block.get("name", "tool")
    payload = block.get("input") or {}

    if payload.get("description"):
        head, separator, rest = condense(payload["description"], NARRATION_CHARS).partition(" ")
        return f"{to_gerund(head)}{separator}{rest}…"

    for key in TOOL_PATH_KEYS:
        if payload.get(key):
            return f"{TOOL_VERBS.get(name, 'Working on')} {Path(condense(payload[key])).name}…"

    for key in TOOL_SEARCH_KEYS:
        if payload.get(key):
            return f"Searching for {condense(payload[key], NARRATION_CHARS)}…"

    if payload.get("url"):
        return f"Fetching {condense(payload['url'], NARRATION_CHARS)}…"

    return f"{TOOL_LABELS.get(name, f'Using {name}')}…"


def resolve_claude_bin(config):
    return str(Path(config.get("daemon.claude_bin", "CLAUDE_BIN", "CLAUDE_PATH", default=str(DEFAULT_CLAUDE_BIN))).expanduser())


def resolve_uv_bin(config):
    return config.get("daemon.uv_bin", "UV_BIN") or shutil.which("uv") or str(DEFAULT_UV_BIN)


def upgrade_checks(uv_bin):
    """Tests, then a boot of the edited daemon in the environment its script header resolves to."""
    return (
        ("Tests", [uv_bin, *TEST_COMMAND]),
        ("Preflight", [uv_bin, "run", str(SCRIPT_PATH), "--preflight"]),
    )


def find_preflight_python(output):
    """The interpreter a passing preflight reported running under."""
    for line in output.splitlines():
        if line.startswith(PREFLIGHT_PYTHON_PREFIX):
            return line.removeprefix(PREFLIGHT_PYTHON_PREFIX).strip()

    return None


async def run_check(command):
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=PROJECT_DIR,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), CHECK_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        process.kill()
        return False, f"gave up after {CHECK_TIMEOUT_SECONDS}s"

    return process.returncode == 0, output.decode(errors="replace")


def pop_restart_state():
    return {key: os.environ.pop(name, "") for key, name in RESTART_KEYS.items()}


def remember_user(channel_name, user_id):
    with ENV_PATH.open("a") as handle:
        handle.write(f"\n{channel_name.upper()}_USER_ID={user_id}\n")


def transcript_path(cwd, session_id):
    """Where claude keeps one conversation's transcript for one working directory."""
    return CLAUDE_PROJECTS_DIR / str(cwd).replace("/", "-") / f"{session_id}.jsonl"


class Request(NamedTuple):
    """A message owed a reply; `protocol_id` is set when it arrived as a protocol envelope."""

    chat_id: int | str
    message_id: int | str | None
    protocol_id: str | None = None


class Session:
    """One Claude conversation, pinned to a working directory."""

    def __init__(self, cwd, model=None):
        self.cwd = cwd
        self.model = model
        self.session_id = None
        self.process = None
        self.reader = None
        self.stderr_reader = None
        self.stderr_tail = deque(maxlen=STDERR_TAIL_LINES)
        self.last_activity = None
        self.patient = False
        self.patient_tool = False
        self.claimed = False
        self.pending = deque()

    @property
    def running(self):
        return self.process is not None and self.process.returncode is None

    @property
    def busy(self):
        return self.claimed or bool(self.pending)

    def stop(self):
        """Ends the process; its reader settles whatever was still in flight."""
        if not self.running:
            return False

        self.process.terminate()
        return True


def claude_command(session, claude_bin, skip_permissions, reply_prompt=None):
    """The long-lived streaming process backing one conversation."""
    command = [
        claude_bin,
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    if session.model:
        command += ["--model", session.model]

    if session.session_id:
        command += ["--resume", session.session_id]

    if skip_permissions:
        command.append("--dangerously-skip-permissions")

    if reply_prompt:
        command += ["--append-system-prompt", reply_prompt]

    return command


def compose_prompt(prompt, reply_quote):
    """Prefix the quoted message when the user used Telegram's reply, so Claude sees what they answered."""
    if not reply_quote:
        return prompt

    return f"[The user is replying to this earlier message:]\n> {reply_quote}\n\n{prompt}"


def compose_voice_prompt(prompt, transcripts):
    notes = [VOICE_NOTE.format(transcript=transcript) for transcript in transcripts]
    return "\n\n".join([*notes, prompt] if prompt else notes)


def transcribe(audio, model_name):
    # Imported on first use, since loading it is slow.
    from faster_whisper import WhisperModel

    if model_name not in WHISPER_MODELS:
        WHISPER_MODELS[model_name] = WhisperModel(model_name, device="cpu", compute_type="int8")

    segments, _ = WHISPER_MODELS[model_name].transcribe(io.BytesIO(audio), vad_filter=True)
    return " ".join(segment.text.strip() for segment in segments).strip()


def image_block(image):
    return {"type": "image", "source": {"type": "base64", "media_type": image["media_type"], "data": image["data"]}}


def user_message(prompt, images=()):
    content = prompt
    if images:
        note = "The user sent an image." if len(images) == 1 else f"The user sent {len(images)} images."
        if prompt:
            note = f"{note}\n\n{prompt}"

        content = [image_block(image) for image in images] + [{"type": "text", "text": note}]

    return json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"


async def stream_json_lines(stream):
    """Newline-delimited events off a pipe, whatever a single event weighs."""
    buffer = bytearray()
    while True:
        chunk = await stream.read(STREAM_CHUNK_BYTES)
        if not chunk:
            break

        buffer.extend(chunk)
        newline = buffer.find(b"\n")
        while newline >= 0:
            line = bytes(buffer[:newline])
            del buffer[: newline + 1]
            if line:
                yield line

            newline = buffer.find(b"\n")

    if buffer:
        yield bytes(buffer)


async def run_claude(session, prompt, claude_bin, skip_permissions, on_tool):
    """One prompt, answered and torn down. The TUI drives a tab at a time this way."""
    process = await asyncio.create_subprocess_exec(
        *claude_command(session, claude_bin, skip_permissions),
        cwd=session.cwd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=STREAM_LINE_LIMIT,
    )
    session.process = process
    process.stdin.write(user_message(prompt).encode())
    await process.stdin.drain()

    answer = None
    async for raw_line in stream_json_lines(process.stdout):
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

        if event.get("session_id"):
            session.session_id = event["session_id"]

        if event.get("type") == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    await on_tool(block)

        elif event.get("type") == "result":
            answer = event.get("result")
            break

    process.stdin.close()
    stderr = (await process.stderr.read()).decode(errors="replace")
    await process.wait()

    if answer:
        return answer

    if process.returncode == -15:
        return "Stopped."

    return f"claude exited with {process.returncode}\n{stderr.strip()[-CHECK_OUTPUT_CHARS:]}"


class Daemon:
    def __init__(self, channel, session, claude_bin, skip_permissions, debug=False, config=None):
        self.channel = channel
        self.session = session
        self.claude_bin = claude_bin
        self.skip_permissions = skip_permissions
        self.debug = debug
        self.config = config or Config()
        self.tasks = set()
        self.stream_lock = asyncio.Lock()
        self.reassembler = protocol.Reassembler()
        self.last_sent_at = None
        self.turn_started_at = None
        self.patient_forced = None
        self.timeout_enabled = True
        self.last_progress_at = None
        self.recent_progress = deque(maxlen=PROGRESS_WINDOW)
        self.stream_idle = True
        self.idle_since = None
        self.coalescing = {}
        self.transcribe_lock = asyncio.Lock()

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def run(self):
        self.spawn(self.heartbeat_forever())
        async for inbound in self.channel.receive():
            self.route_inbound(inbound)

    def route_inbound(self, inbound):
        text = (inbound.text or "").strip()
        if protocol.decode(text) or text.startswith("/") or is_stop_request(text):
            self.spawn(self.handle(inbound))  # TUI envelopes reassemble themselves
        else:
            self.buffer_inbound(inbound)

    def buffer_inbound(self, inbound):
        """Hold a plain message briefly so a Telegram-split (or a quick burst) becomes one prompt."""
        buf = self.coalescing.get(inbound.chat_id)
        if buf is None:
            buf = {"texts": [], "images": [], "voices": [], "message_id": inbound.message_id, "reply_quote": None}
            self.coalescing[inbound.chat_id] = buf
        if inbound.text:
            buf["texts"].append(inbound.text)
        buf["images"].extend(inbound.images)
        buf["voices"].extend(inbound.voices)
        if inbound.reply_quote and not buf["reply_quote"]:
            buf["reply_quote"] = inbound.reply_quote
        if buf.get("timer"):
            buf["timer"].cancel()
        buf["timer"] = self.spawn(self.flush_coalesced(inbound.chat_id))

    async def flush_coalesced(self, chat_id):
        try:
            await asyncio.sleep(COALESCE_WINDOW_SECONDS)
        except asyncio.CancelledError:
            return

        buf = self.coalescing.pop(chat_id, None)
        if not buf:
            return

        combined = Inbound(chat_id, "\n".join(buf["texts"]), buf["message_id"],
                           tuple(buf["images"]), buf["reply_quote"], tuple(buf["voices"]))
        await self.handle(combined)

    async def handle(self, inbound):
        request = Request(inbound.chat_id, inbound.message_id)
        try:
            envelope = protocol.decode(inbound.text)
            if envelope:
                request = request._replace(protocol_id=envelope.request_id)
                envelope = self.reassembler.add(envelope)
                if envelope is None:
                    return

            text = (envelope.text if envelope else inbound.text).strip()
            if text.startswith("/"):
                await self.handle_command(request, text)
            elif is_stop_request(text):
                await self.reply(request, self.stop_everything())
            elif inbound.voices:
                await self.handle_voice_turn(request, text, inbound)
            else:
                await self.handle_turn(request, text, inbound.images, inbound.reply_quote)
        except Exception as error:
            await self.reply(request, f"daemon error: {error}")

    async def handle_voice_turn(self, request, text, inbound):
        await self.channel.send_typing(request.chat_id)
        transcripts = [transcript for transcript in await self.transcribe_voices(inbound.voices) if transcript]
        if not transcripts and not text and not inbound.images:
            await self.reply(request, NO_SPEECH_REPLY)
            return

        for transcript in transcripts:
            await self.reply(request, f"> 🎤 {transcript}", final=False)

        await self.handle_turn(request, compose_voice_prompt(text, transcripts), inbound.images, inbound.reply_quote)

    async def transcribe_voices(self, voices):
        model_name = self.config.get("daemon.whisper_model", "TELECLAUDE_WHISPER_MODEL", default=DEFAULT_WHISPER_MODEL)
        async with self.transcribe_lock:
            return [await asyncio.to_thread(transcribe, audio, model_name) for audio in voices]

    async def reply(self, request, text, final=True):
        """Answers in the form the request came in: plain text, or protocol envelopes."""
        reply_to = request.message_id if final else None
        if request.protocol_id is None:
            await self.channel.send(request.chat_id, text if final else f"{WORKING_PREFIX}{text}", reply_to=reply_to)
            return

        kind = protocol.FINAL if final else protocol.PROGRESS
        for message in protocol.encode_parts(request.protocol_id, text, kind, self.channel.max_message_chars):
            await self.channel.send(request.chat_id, message, reply_to=reply_to, verbatim=True)

    async def handle_command(self, request, text):
        name, _, argument = text.partition(" ")
        argument = argument.strip()

        if name == "/help" or name == "/start":
            await self.reply(request, HELP_TEXT)

        elif name in NEW_COMMANDS:
            self.session.stop()
            self.session.pending.clear()
            self.session.session_id = None
            await self.reply(request, f"Fresh conversation in {self.session.cwd}")

        elif name == "/pwd":
            state = "working" if self.session.busy else "idle"
            conversation = self.session.session_id or "none yet"
            await self.reply(
                request,
                f"{self.session.cwd}\nsession: {conversation}\nstate: {state}\n"
                f"in flight: {len(self.session.pending)}\npatient: {'yes' if self.patient else 'no'}\n"
                f"timeout: {'on' if self.timeout_enabled else 'off'}",
            )

        elif name == "/cd":
            await self.change_directory(request, argument)

        elif name == "/resume":
            await self.reply(request, self.resume_session(argument))

        elif name == "/stop":
            await self.reply(request, self.stop_everything())

        elif name == "/debug":
            await self.reply(request, self.set_debug(argument))

        elif name == "/patient":
            await self.reply(request, self.set_patient(argument))

        elif name == "/timeout":
            await self.reply(request, self.set_timeout(argument))

        elif name == "/model":
            await self.reply(request, self.set_model(argument))

        elif name == "/clean":
            await self.clean(request, argument)

        elif name == "/diff":
            await self.open_diff_viewer(request, argument)

        elif name == "/show":
            await self.reply(request, self.show_file(argument))

        elif name == "/upgrade":
            await self.upgrade(request)

        else:
            await self.reply(request, f"Unknown command {name}\n\n{HELP_TEXT}")

    def resume_session(self, argument):
        """Adopt a transcript pushed over from another machine."""
        if not argument:
            return "Usage: /resume <session-id>"

        if not transcript_path(self.session.cwd, argument).exists():
            return f"No transcript for {argument} in {self.session.cwd}"

        self.session.stop()
        self.session.pending.clear()
        self.session.session_id = argument
        return f"Resumed {argument} in {self.session.cwd}"

    def set_model(self, argument):
        """Switch the model for this conversation, or list them when given nothing."""
        if not argument:
            return render_models(self.session.model)

        self.session.model = None if argument in DEFAULT_MODEL_WORDS else argument
        return render_models(self.session.model)

    async def clean(self, request, argument):
        """Report the last run first, then start one unless asked only to look."""
        script = self.config.get_path("daemon.clean_script")
        status_path = self.config.get_path("daemon.clean_status")
        if not script or not status_path:
            await self.reply(request, "Set daemon.clean_script and daemon.clean_status in the config to use /clean.")
            return

        only_show = argument in CLEAN_SHOW_ONLY_WORDS
        await self.reply(request, render_clean_status(read_clean_status(status_path)), final=only_show)
        if only_show:
            return

        if argument:
            await self.reply(request, f"Unknown option {argument}. Use /clean --only-show.")
            return

        # Cleaning mid-build deletes the target out from under it.
        if self.session.busy:
            await self.reply(request, "Busy. /stop first, or /clean --only-show.")
            return

        await self.reply(request, "Running the clean, this takes a minute…", final=False)
        passed, output = await run_check([str(script), "--force"])
        if not passed:
            tail = output.strip()[-CHECK_OUTPUT_CHARS:]
            await self.reply(request, f"Clean failed.\n\n{tail}")
            return

        await self.reply(request, render_clean_status(read_clean_status(status_path)))

    async def open_diff_viewer(self, request, path):
        base_url = self.config.get("daemon.diff_url", "TELECLAUDE_DIFF_URL")
        auth_key = self.config.get("daemon.diff_auth_key", "TELECLAUDE_DIFF_AUTH_KEY")
        if not base_url or not auth_key:
            await self.reply(request, "Set daemon.diff_url and daemon.diff_auth_key in the config to use /diff.")
            return

        link = diff_viewer_link(base_url, auth_key, path)
        label = path.rsplit("/", 1)[-1] if path.strip("/") else "open dashboard"
        if request.protocol_id:
            await self.reply(request, f"Diff viewer: {label}\n{link}")
            return

        await self.channel.send_link(request.chat_id, f"Diff viewer: {label}", "Open", link)

    def render_tool(self, block):
        return describe_tool(block) if self.debug else narrate_tool(block)

    @property
    def patient(self):
        """Whether the current turn waits on something slow."""
        if self.patient_forced is not None:
            return self.patient_forced

        return self.session.patient or self.session.patient_tool

    @property
    def heartbeat_interval(self):
        return PATIENT_HEARTBEAT_SECONDS if self.patient else HEARTBEAT_SECONDS

    @property
    def stall_timeout(self):
        return PATIENT_STALL_TIMEOUT_SECONDS if self.patient else STALL_TIMEOUT_SECONDS

    def set_timeout(self, argument):
        """Turn the no-progress timeout on or off, or report it when given nothing."""
        wanted = argument.strip().lower()
        if wanted in DEBUG_ON_WORDS:
            self.timeout_enabled = True
        elif wanted in DEBUG_OFF_WORDS:
            self.timeout_enabled = False
        elif wanted:
            return "Usage: /timeout on|off"

        if not self.timeout_enabled:
            return "Timeout off: turns run until they finish or you /stop."

        return (
            f"Timeout on: a turn gives up after {format_duration(STALL_TIMEOUT_SECONDS)} without progress,"
            f" {format_duration(PATIENT_STALL_TIMEOUT_SECONDS)} while it waits on something slow."
        )

    def set_patient(self, argument):
        """Force the long-wait pace on or off, or hand it back to detection."""
        wanted = argument.strip().lower()
        if wanted in DEBUG_ON_WORDS:
            self.patient_forced = True
        elif wanted in DEBUG_OFF_WORDS:
            self.patient_forced = False
        elif wanted in PATIENT_AUTO_WORDS:
            self.patient_forced = None
        elif wanted:
            return "Usage: /patient on|off|auto"

        source = "detected from the prompt and tool calls" if self.patient_forced is None else "forced"
        pace = (
            f"gives up after {format_duration(self.stall_timeout)} without progress,"
            f" a nudge every {format_duration(self.heartbeat_interval)}."
        )
        return f"Patient ({source}): {pace}" if self.patient else f"Impatient ({source}): {pace}"

    def set_debug(self, argument):
        wanted = argument.strip().lower()
        if wanted in DEBUG_ON_WORDS:
            self.debug = True
        elif wanted in DEBUG_OFF_WORDS:
            self.debug = False
        elif wanted:
            return "Usage: /debug on|off"
        else:
            self.debug = not self.debug

        return "Debug on, showing raw tool input." if self.debug else "Debug off, showing plain descriptions."

    async def upgrade(self, request):
        if self.session.busy:
            await self.reply(request, "Busy. /stop first, then /upgrade.")
            return

        await self.reply(request, "Checking the edited daemon…", final=False)
        python = sys.executable
        for name, command in upgrade_checks(resolve_uv_bin(self.config)):
            passed, output = await run_check(command)
            if not passed:
                tail = output.strip()[-CHECK_OUTPUT_CHARS:]
                await self.reply(request, f"{name} failed, staying on the old code.\n\n{tail}")
                return

            python = find_preflight_python(output) or python

        published = await self.publish_upgrade()
        await self.reply(request, f"Checks passed, restarting.\n{published}")
        self.restart(request.chat_id, python)

    async def publish_upgrade(self):
        """Bump the patch version, then commit and push the deployed tree; best-effort."""
        await run_check(["git", "add", "-A"])
        clean, _ = await run_check(["git", "diff", "--cached", "--quiet"])
        if clean:
            return "No code changes to publish."

        version = bump_version()
        await run_check(["git", "add", "-A"])
        committed, output = await run_check(["git", "commit", "-m", f"teleclaude v{version}"])
        if not committed:
            return f"Commit failed, deploying anyway.\n{output.strip()[-CHECK_OUTPUT_CHARS:]}"

        pushed, output = await run_check(["git", "push"])
        if not pushed:
            return f"Committed v{version}; push failed (retries next upgrade).\n{output.strip()[-CHECK_OUTPUT_CHARS:]}"

        return f"Published v{version} to origin."

    def restart(self, chat_id, python):
        os.execve(python, [python, str(SCRIPT_PATH)], self.restart_environment(chat_id))

    def restart_environment(self, chat_id):
        return {
            **os.environ,
            RESTART_KEYS["chat"]: str(chat_id),
            RESTART_KEYS["session"]: self.session.session_id or "",
            RESTART_KEYS["cwd"]: str(self.session.cwd),
            RESTART_KEYS["cursor"]: str(self.channel.cursor or ""),
            RESTART_KEYS["debug"]: "1" if self.debug else "0",
            RESTART_KEYS["model"]: self.session.model or "",
        }

    def stop_everything(self):
        stopped = self.session.stop()
        dropped = self.drain()
        if not stopped and not dropped:
            return "Nothing running."

        parts = ["Stopped the running turn."] if stopped else []
        if dropped:
            parts.append(f"Dropped {dropped} in flight.")

        return " ".join(parts)

    def show_file(self, argument):
        """A file, or a line range of it, as a fenced block the channel renders as code."""
        path_text, first, last = SHOW_PATTERN.match(argument).groups()
        if not path_text:
            return "Usage: /show <path>[:start[-end]]"

        target = Path(path_text).expanduser()
        if not target.is_absolute():
            target = self.session.cwd / target

        if not target.is_file():
            return f"Not a file: {target}"

        lines = target.read_text(errors="replace").rstrip("\n").split("\n")
        start = int(first) if first else 1
        end = min(int(last) if last else start + SHOW_MAX_LINES - 1, len(lines))
        if start > end:
            return f"{path_text} has {len(lines)} lines."

        excerpt = "\n".join(lines[start - 1:end])
        fence = "`" * max(3, max((len(run) for run in re.findall(r"`+", excerpt)), default=0) + 1)
        shown = f"{fence} {path_text}:{start}\n{excerpt}\n{fence}"
        if end < len(lines) and not last:
            shown += f"\nLines {start}-{end} of {len(lines)}. /show {path_text}:{end + 1} for more."

        return shown

    async def change_directory(self, request, argument):
        if not argument:
            await self.reply(request, "Usage: /cd <path>")
            return

        target = Path(argument).expanduser()
        if not target.is_absolute():
            target = self.session.cwd / target

        target = target.resolve()
        if not target.is_dir():
            await self.reply(request, f"Not a directory: {target}")
            return

        self.session.stop()
        self.session.pending.clear()
        self.session.cwd = target
        self.session.session_id = None
        await self.reply(request, f"Now in {target} (fresh conversation)")

    async def handle_turn(self, request, prompt, images=(), reply_quote=None):
        """Hands the prompt to Claude straight away; it does its own scheduling."""
        # Messages arriving together are handled concurrently, so one writer at a time
        # keeps them to a single process and keeps the stdin lines whole.
        async with self.stream_lock:
            await self.ensure_stream()
            if request.protocol_id is None:
                if self.session.pending:
                    await self.channel.send(request.chat_id, queued_response(len(self.session.pending) + 1))
                else:
                    await self.channel.send(request.chat_id, pick_initial_response(prompt))
            if self.session.pending:
                await self.reply(request, f"> {summarize(prompt) or '[image]'}", final=False)

            self.session.pending.append(request)
            self.session.patient = mentions_long_wait(prompt)
            self.stream_idle = False
            self.idle_since = None
            self.turn_started_at = time.monotonic()
            self.last_sent_at = self.turn_started_at
            self.last_progress_at = self.turn_started_at
            self.recent_progress.clear()
            self.session.process.stdin.write(user_message(compose_prompt(prompt, reply_quote), images).encode())
            await self.session.process.stdin.drain()

        await self.channel.send_typing(request.chat_id)

    async def notify(self, request, text, final=True):
        """Every turn message goes through here, so the heartbeat knows the chat isn't silent."""
        self.last_sent_at = time.monotonic()
        await self.reply(request, text, final=final)

    async def heartbeat_forever(self):
        """Guarantees a line to the waiting chat on a timer, without asking Claude for one."""
        while True:
            await asyncio.sleep(HEARTBEAT_TICK_SECONDS)
            if not self.session.pending:
                continue

            if self.stream_idle:
                self.drop_orphaned_replies()
                continue

            if await self.enforce_timeout():
                continue

            quiet_for = time.monotonic() - (self.last_sent_at or time.monotonic())
            if quiet_for < self.heartbeat_interval:
                continue

            try:
                await self.notify(self.session.pending[0], self.stall_note(), final=False)
            except Exception as error:
                print(f"heartbeat failed: {error}", flush=True)
                self.last_sent_at = time.monotonic()

    def drop_orphaned_replies(self):
        """Forgets waiters a folded turn already answered, so an idle session stops reporting busy."""
        if self.idle_since is None or time.monotonic() - self.idle_since < IDLE_DRAIN_SECONDS:
            return False

        self.session.pending.clear()
        return True

    async def enforce_timeout(self):
        """Ends the turn once it has gone the stall timeout without progress."""
        if not self.timeout_enabled or not self.last_progress_at:
            return False

        stalled_for = time.monotonic() - self.last_progress_at
        if stalled_for < self.stall_timeout:
            return False

        requests = list(self.session.pending)
        self.session.stop()
        self.drain()
        note = f"Gave up after {format_duration(stalled_for)} without progress."
        note += "\nSend /timeout off first if it genuinely needs longer."
        for request in requests:
            await self.notify(request, note)

        return True

    def track_progress(self, block):
        """Restarts the stall clock on a tool call or remark unlike the recent ones."""
        fingerprint = progress_fingerprint(block)
        if fingerprint is None:
            return

        if block.get("type") == "tool_use":
            self.session.patient_tool = is_patient_tool(block)

        if fingerprint not in self.recent_progress:
            self.last_progress_at = time.monotonic()

        self.recent_progress.append(fingerprint)

    def stall_note(self):
        """What can be said about a quiet turn from this side of the pipe alone."""
        elapsed = format_duration(time.monotonic() - (self.turn_started_at or time.monotonic()))
        parts = [f"Teleclaude still thinking… ({elapsed})"]
        if not self.session.running:
            parts.append("the claude process is gone; /new to start over")

        hint = stall_hint(self.session.stderr_tail)
        if hint:
            parts.append(hint)

        return "\n".join(parts)

    async def ensure_stream(self):
        if self.session.running:
            return

        command = claude_command(self.session, self.claude_bin, self.skip_permissions, self.channel.reply_prompt)
        self.session.process = await asyncio.create_subprocess_exec(
            *command,
            cwd=self.session.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=STREAM_LINE_LIMIT,
        )
        self.session.stderr_tail.clear()
        self.session.reader = asyncio.create_task(self.read_events(self.session.process))
        self.session.stderr_reader = asyncio.create_task(self.read_stderr(self.session.process))

    async def read_stderr(self, process):
        """Drained as it arrives, so a retry storm is visible before the process exits."""
        async for raw_line in process.stderr:
            self.session.stderr_tail.append(raw_line.decode(errors="replace"))

    async def read_events(self, process):
        async for raw_line in stream_json_lines(process.stdout):
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue

            await self.dispatch(event)

        await self.finish_stream(process)

    async def dispatch(self, event):
        if event.get("session_id"):
            self.session.session_id = event["session_id"]

        # A result is the last event of a turn, so the process is idle until the next prompt.
        self.stream_idle = event.get("type") == "result"
        self.idle_since = time.monotonic() if self.stream_idle else None

        if event.get("type") == "assistant":
            for block in event.get("message", {}).get("content", []):
                self.track_progress(block)
                if block.get("type") == "tool_use":
                    await self.announce(self.render_tool(block))

        elif event.get("type") == "result":
            request = self.next_request()
            if request is not None:
                await self.notify(request, event.get("result") or "(no answer)")
                self.turn_started_at = time.monotonic()
                self.last_progress_at = self.turn_started_at
                self.recent_progress.clear()
                if self.session.pending:
                    await self.channel.send_typing(request.chat_id)

    def next_request(self):
        """The prompt this result answers, in the order they were sent."""
        return self.session.pending.popleft() if self.session.pending else None

    async def announce(self, text):
        """Tool lines go to whoever is still waiting, so a quiet chat stays quiet."""
        if not self.session.pending:
            return

        request = self.session.pending[0]
        self.session.last_activity = text
        await self.notify(request, text, final=False)
        await self.channel.send_typing(request.chat_id)

    async def finish_stream(self, process):
        if self.session.stderr_reader:
            await self.session.stderr_reader

        await process.wait()
        stderr = "".join(self.session.stderr_tail)
        waiting = list(self.session.pending)
        self.session.pending.clear()
        if process.returncode in (0, -15) and not waiting:
            return

        note = "Stopped." if process.returncode == -15 else (
            f"claude exited with {process.returncode}\n{stderr.strip()[-CHECK_OUTPUT_CHARS:]}"
        )
        for request in waiting:
            await self.notify(request, note)

    def drain(self):
        dropped = len(self.session.pending)
        self.session.pending.clear()
        return dropped


def boot_greeting():
    return f"Hello! This is teleclaude v{VERSION}, up on the new code."


def record_deploy(version, when=None):
    """Append this boot to the deploy log so what is live, and since when, is always auditable."""
    stamp = (when or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    DEPLOY_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(DEPLOY_LOG, "a") as handle:
        handle.write(f"{stamp} v{version}\n")


def bump_version():
    """Increment the patch version in this file on disk (a deploy-time step); returns the new version."""
    source = SCRIPT_PATH.read_text()
    current = re.search(r'^VERSION = "(\d+)\.(\d+)\.(\d+)"', source, re.M)
    major, minor, patch = (int(part) for part in current.groups())
    new_version = f"{major}.{minor}.{patch + 1}"
    SCRIPT_PATH.write_text(source.replace(current.group(0), f'VERSION = "{new_version}"', 1))
    return new_version


def pick_initial_response(prompt):
    """Fast keyword routing: sassy for tplus work, gentle for the investigation, plain otherwise."""
    text = prompt or ""
    if SPYWEAR_MARKERS.search(text):
        return random.choice(GENTLE_RESPONSES)
    if TPLUS_MARKERS.search(text):
        return random.choice(SASSY_RESPONSES)

    return random.choice(NEUTRAL_RESPONSES)


def ordinal(number):
    suffix = "th" if 10 <= number % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def queued_response(position):
    """The opener for a request that lands while an earlier one is still running."""
    return random.choice(QUEUED_RESPONSES).format(pos=ordinal(position))


def parse_chat_id(value):
    return int(value) if value.lstrip("-").isdigit() else value


async def main(preflight=False):
    load_env(ENV_PATH)
    config = Config.load()
    restarted = pop_restart_state()
    receiver_class = find_channel(RECEIVERS, config)

    workdir = Path(restarted["cwd"] or config.get("daemon.workdir", "CLAUDE_WORKDIR", default=str(Path.home())))
    workdir = workdir.expanduser().resolve()
    skip_permissions = config.get_bool("daemon.skip_permissions", "SKIP_PERMISSIONS", default=True)
    claude_bin = resolve_claude_bin(config)
    debug = restarted["debug"] == "1" if restarted["chat"] else config.get_bool("daemon.debug", "TELECLAUDE_DEBUG")
    model = restarted["model"] or config.get("daemon.model", "CLAUDE_MODEL") or None

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as http:
        channel = receiver_class.from_config(config, http, partial(remember_user, receiver_class.name), restarted["cursor"])
        identity = await channel.connect()
        session = Session(workdir, model=model)
        session.session_id = restarted["session"] or None
        daemon = Daemon(channel, session, claude_bin, skip_permissions, debug, config)

        if preflight:
            print("preflight ok", flush=True)
            print(f"{PREFLIGHT_PYTHON_PREFIX}{sys.executable}", flush=True)
            return

        record_deploy(VERSION)
        print(f"{identity} v{VERSION} listening on {receiver_class.name}, cwd={workdir}, claude={claude_bin}", flush=True)
        if restarted["chat"]:
            await channel.send(parse_chat_id(restarted["chat"]), boot_greeting())

        await daemon.run()


if __name__ == "__main__":
    try:
        asyncio.run(main(preflight="--preflight" in sys.argv))
    except KeyboardInterrupt:
        pass
