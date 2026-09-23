"""Channels carry teleclaude messages: a receiver on the daemon's side, a sender on the client's."""

import asyncio
import base64
from abc import ABC, abstractmethod
from pathlib import Path
from typing import NamedTuple

from config import resolve_config_path

DEFAULT_CHANNEL = "telegram"
MAX_MESSAGE_CHARS = 3800
RECEIVERS = {}
SENDERS = {}
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
TELEGRAM_FILE_API = "https://api.telegram.org/file/bot{token}/{path}"
TELEGRAM_POLL_SECONDS = 50
TELEGRAM_RETRY_SECONDS = 5
TELEGRAM_SESSION_PATH = Path.home() / ".config/teleclaude/telegram"
TELEGRAM_REPLY_PROMPT = """Your replies are read in Telegram. Fenced code renders as a syntax-highlighted editor box with line numbers, and long boxes split across messages between functions.
- Put code in a fence whose info string is the language and path, plus the first line number when the excerpt does not start at line 1: ```rust src/book.rs:120
- Show changes as a ```diff fence holding unified diff output such as git diff; each file becomes its own box, numbered by the new file.
- When reviewing, quote the exact lines you discuss with their real line numbers, and put each comment right after its fence.
- **bold**, `inline code` and # headings render. Tables and [text](url) links do not, so use plain lists and bare URLs."""


def register(registry):
    def add_to_registry(channel_class):
        registry[channel_class.name] = channel_class
        return channel_class

    return add_to_registry


def find_channel(registry, config):
    name = config.get("channel", "TELECLAUDE_CHANNEL", default=DEFAULT_CHANNEL)
    if name not in registry:
        raise SystemExit(f"Unknown channel {name!r}. Registered: {', '.join(sorted(registry))}")

    return registry[name]


def split_message(text, limit=MAX_MESSAGE_CHARS):
    remaining = text.strip() or "(no output)"
    while remaining:
        if len(remaining) <= limit:
            yield remaining
            return

        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit

        yield remaining[:cut]
        remaining = remaining[cut:].lstrip("\n")


class Inbound(NamedTuple):
    chat_id: int | str
    text: str
    message_id: int | str | None
    images: tuple = ()
    reply_quote: str | None = None
    voices: tuple = ()


class TeleclaudeChannelReceiver(ABC):
    """The daemon's end of a channel: takes prompts in and sends the answers back."""

    name = ""
    max_message_chars = MAX_MESSAGE_CHARS
    # Where receiving resumes after a restart.
    cursor = None
    # Appended to Claude's system prompt so replies suit how this channel renders them.
    reply_prompt = None

    @classmethod
    @abstractmethod
    def from_config(cls, config, http, remember_user, cursor):
        """Builds the receiver from config, an httpx client, a pairing callback and a saved cursor."""

    @abstractmethod
    async def connect(self):
        """Checks the channel is reachable and returns the name the daemon goes by on it."""

    @abstractmethod
    def receive(self):
        """Yields an Inbound for every message from an allowed sender, forever."""

    @abstractmethod
    async def send(self, chat_id, text, reply_to=None, verbatim=False):
        """Sends markdown for a person to read, or `verbatim` text exactly as given."""

    async def send_typing(self, chat_id):
        pass

    async def send_link(self, chat_id, text, label, url):
        await self.send(chat_id, f"{text}\n{label}: {url}")


class TeleclaudeChannelSender(ABC):
    """The client's end of a channel: sends prompts to the daemon and hears what it says back."""

    name = ""
    max_message_chars = MAX_MESSAGE_CHARS

    @classmethod
    @abstractmethod
    def from_config(cls, config):
        pass

    async def login(self):
        """One-time interactive setup, for channels that need it."""

    @abstractmethod
    async def open(self):
        """Connects and starts collecting the daemon's messages."""

    @abstractmethod
    async def send(self, text):
        pass

    @abstractmethod
    async def next_message(self, timeout):
        """The daemon's next message, or None after `timeout` seconds of silence."""

    @abstractmethod
    async def close(self):
        pass


@register(RECEIVERS)
class TelegramChannelReceiver(TeleclaudeChannelReceiver):
    """A Telegram bot, polled over the Bot API, that answers only the account it is paired with."""

    name = "telegram"
    reply_prompt = TELEGRAM_REPLY_PROMPT

    def __init__(self, token, http, allowed_user_id=None, remember_user=None, cursor=None):
        self.token = token
        self.http = http
        self.allowed_user_id = allowed_user_id
        self.remember_user = remember_user
        self.cursor = cursor

    @classmethod
    def from_config(cls, config, http, remember_user, cursor):
        token = config.get("telegram.bot_token", "TELEGRAM_BOT_TOKEN", "BOT_API_KEY")
        if not token:
            raise SystemExit(f"Set telegram.bot_token in {resolve_config_path()}")

        user_id = config.get("telegram.user_id", "TELEGRAM_USER_ID")
        return cls(token, http, int(user_id) if user_id else None, remember_user, int(cursor) if cursor else None)

    async def call(self, method, **params):
        url = TELEGRAM_API.format(token=self.token, method=method)
        response = await self.http.post(url, json=params)
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"telegram {method} failed: {payload}")

        return payload["result"]

    async def connect(self):
        identity = await self.call("getMe")
        return f"@{identity['username']}"

    async def receive(self):
        while True:
            try:
                updates = await self.call(
                    "getUpdates",
                    offset=self.cursor,
                    timeout=TELEGRAM_POLL_SECONDS,
                    allowed_updates=["message"],
                )
            except Exception as error:
                print(f"poll failed: {error}", flush=True)
                await asyncio.sleep(TELEGRAM_RETRY_SECONDS)
                continue

            for update in updates:
                self.cursor = update["update_id"] + 1
                inbound = await self.admit(update.get("message"))
                if inbound:
                    yield inbound

    async def admit(self, message):
        """The message as an Inbound when its sender may prompt the daemon; the first sender pairs."""
        if not message:
            return None

        text = message.get("text") or message.get("caption") or ""
        if not text and not self.image_file(message) and not self.voice_file_id(message):
            return None

        chat_id = message["chat"]["id"]
        sender_id = message.get("from", {}).get("id")
        if self.allowed_user_id is None:
            self.allowed_user_id = sender_id
            if self.remember_user:
                self.remember_user(sender_id)

            await self.send(chat_id, f"Paired with {sender_id}. Everyone else is ignored now.")

        elif sender_id != self.allowed_user_id:
            print(f"ignored message from {message.get('from')}", flush=True)
            return None

        replied = message.get("reply_to_message") or {}
        reply_quote = replied.get("text") or replied.get("caption")
        images = await self.collect_images(message)
        return Inbound(chat_id, text, message.get("message_id"), images, reply_quote, await self.collect_voices(message))

    def image_file(self, message):
        """The Telegram file id and media type of an attached image, or None."""
        photos = message.get("photo")
        if photos:
            return photos[-1]["file_id"], "image/jpeg"

        document = message.get("document") or {}
        mime = document.get("mime_type", "")
        if mime.startswith("image/"):
            return document["file_id"], mime

        return None

    async def collect_images(self, message):
        """Base64 image blocks for an attached image (largest rendition), empty when none."""
        found = self.image_file(message)
        if not found:
            return ()

        file_id, media_type = found
        data = await self.download_file(file_id)
        return ({"media_type": media_type, "data": base64.b64encode(data).decode()},)

    def voice_file_id(self, message):
        """The Telegram file id of an attached voice memo or audio file, or None."""
        for kind in ("voice", "audio"):
            if message.get(kind):
                return message[kind]["file_id"]

        document = message.get("document") or {}
        if document.get("mime_type", "").startswith("audio/"):
            return document["file_id"]

        return None

    async def collect_voices(self, message):
        """The raw bytes of an attached voice memo or audio file, empty when none."""
        file_id = self.voice_file_id(message)
        if not file_id:
            return ()

        return (await self.download_file(file_id),)

    async def download_file(self, file_id):
        """The raw bytes of a Telegram file by id."""
        info = await self.call("getFile", file_id=file_id)
        response = await self.http.get(TELEGRAM_FILE_API.format(token=self.token, path=info["file_path"]))
        response.raise_for_status()
        return response.content

    async def send(self, chat_id, text, reply_to=None, verbatim=False):
        reply = {"reply_parameters": {"message_id": reply_to, "allow_sending_without_reply": True}} if reply_to else {}
        if verbatim:
            messages = [(chunk, []) for chunk in split_message(text, self.max_message_chars)]
        else:
            # Pygments is a daemon-only dependency.
            from codebox import build_messages

            messages = build_messages(text, self.max_message_chars)

        for chunk, entities in messages:
            formatting = {"entities": entities} if entities else {}
            await self.call("sendMessage", chat_id=chat_id, text=chunk, disable_web_page_preview=True, **reply, **formatting)

    async def send_typing(self, chat_id):
        await self.call("sendChatAction", chat_id=chat_id, action="typing")

    async def send_link(self, chat_id, text, label, url):
        await self.call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            disable_web_page_preview=True,
            reply_markup={"inline_keyboard": [[{"text": label, "web_app": {"url": url}}]]},
        )


@register(SENDERS)
class TelegramChannelSender(TeleclaudeChannelSender):
    """Your own Telegram account, over MTProto, messaging the daemon's bot."""

    name = "telegram"

    def __init__(self, api_id, api_hash, bot_username, session_path=TELEGRAM_SESSION_PATH):
        self.api_id = api_id
        self.api_hash = api_hash
        self.bot_username = bot_username
        self.session_path = Path(session_path)
        self.client = None
        self.bot = None
        self.inbox = asyncio.Queue()

    @classmethod
    def from_config(cls, config):
        settings = {
            "telegram.api_id": config.get("telegram.api_id", "TELEGRAM_API_ID", "TELECLAUDE_APP_ID"),
            "telegram.api_hash": config.get("telegram.api_hash", "TELEGRAM_API_HASH", "TELEGRAM_API_KEY"),
            "telegram.bot_username": config.get("telegram.bot_username", "TELEGRAM_BOT_USERNAME"),
        }
        missing = [key for key, value in settings.items() if not value]
        if missing:
            raise SystemExit(f"Set {', '.join(missing)} in {resolve_config_path()}")

        return cls(
            int(settings["telegram.api_id"]),
            str(settings["telegram.api_hash"]),
            str(settings["telegram.bot_username"]).lstrip("@"),
            config.get_path("telegram.session", default=TELEGRAM_SESSION_PATH),
        )

    def build_client(self):
        # Telethon is a client-only dependency.
        from telethon import TelegramClient

        self.session_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        return TelegramClient(str(self.session_path), self.api_id, self.api_hash)

    async def login(self):
        client = self.build_client()
        await client.start()
        account = await client.get_me()
        await client.disconnect()
        self.session_path.with_suffix(".session").chmod(0o600)
        print(f"Logged in as {account.username or account.first_name} ({account.id}).")
        print("The daemon only answers the account it paired with, so that id must match.")

    async def open(self):
        from telethon import events

        self.client = self.build_client()
        await self.client.connect()
        if not await self.client.is_user_authorized():
            await self.client.disconnect()
            raise SystemExit("Not logged in. Run: uv run teleclaude.py login")

        self.bot = await self.client.get_entity(self.bot_username)
        self.client.add_event_handler(self.enqueue, events.NewMessage(chats=self.bot, incoming=True))

    async def enqueue(self, event):
        self.inbox.put_nowait(event.raw_text)

    async def send(self, text):
        # Markdown parsing would strip the envelope's punctuation.
        await self.client.send_message(self.bot, text, parse_mode=None, link_preview=False)

    async def next_message(self, timeout):
        try:
            return await asyncio.wait_for(self.inbox.get(), timeout)
        except TimeoutError:
            return None

    async def close(self):
        if self.client:
            await self.client.disconnect()
