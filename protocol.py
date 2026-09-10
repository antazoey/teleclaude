"""teleclaude wire format: Brotli-compressed JSON envelopes carried inside ordinary chat messages."""

import base64
import json
from typing import NamedTuple

import brotli

MARKER = "tc1:"
PROGRESS = "p"
FINAL = "f"


class Envelope(NamedTuple):
    """One protocol message; `kind` is unset on requests, `more` marks a split that continues."""

    request_id: str
    text: str
    kind: str | None = None
    more: bool = False


def encode(envelope):
    fields = {"i": envelope.request_id, "t": envelope.text}
    if envelope.kind:
        fields["k"] = envelope.kind

    if envelope.more:
        fields["m"] = 1

    payload = json.dumps(fields, separators=(",", ":"), ensure_ascii=False).encode()
    return MARKER + base64.b85encode(brotli.compress(payload, mode=brotli.MODE_TEXT)).decode()


def decode(message):
    """The envelope a chat message carries, or None when it is plain text."""
    if not message.startswith(MARKER):
        return None

    try:
        fields = json.loads(brotli.decompress(base64.b85decode(message[len(MARKER):])))
        return Envelope(str(fields["i"]), fields["t"], fields.get("k"), bool(fields.get("m")))
    except (ValueError, KeyError, TypeError, brotli.error) as error:
        raise ValueError(f"malformed teleclaude envelope: {error}") from error


def split_to_fit(request_id, text, kind, limit):
    if len(encode(Envelope(request_id, text, kind, more=True))) <= limit or len(text) < 2:
        return [text]

    middle = text.rfind("\n", 0, len(text) // 2) + 1 or len(text) // 2
    return split_to_fit(request_id, text[:middle], kind, limit) + split_to_fit(request_id, text[middle:], kind, limit)


def encode_parts(request_id, text, kind, limit):
    """Envelopes carrying `text`, each short enough for one chat message of `limit` characters."""
    pieces = split_to_fit(request_id, text, kind, limit)
    return [
        encode(Envelope(request_id, piece, kind, more=index < len(pieces) - 1))
        for index, piece in enumerate(pieces)
    ]


class Reassembler:
    """Joins the parts of split envelopes back into whole ones."""

    def __init__(self):
        self.partial = {}

    def add(self, envelope):
        """The whole envelope once its last part arrives, else None."""
        key = (envelope.request_id, envelope.kind)
        if envelope.more:
            self.partial[key] = self.partial.get(key, "") + envelope.text
            return None

        return envelope._replace(text=self.partial.pop(key, "") + envelope.text)
