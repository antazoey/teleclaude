import hashlib

import pytest

import protocol

ENGLISH = (
    "The daemon runs Claude Code on the server and relays each turn over a channel. When a turn "
    "stops making progress it gives up, but a turn that keeps doing new work runs as long as it "
    "needs. Replies to a protocol request come back as compressed envelopes, so a client can tell "
    "progress from the final answer without guessing, and a long answer splits across messages."
)


def test_encode_parts_round_trips_text_too_long_for_one_message():
    text = "\n".join(f"step {index} → {hashlib.sha256(str(index).encode()).hexdigest()} ✅" for index in range(40))
    limit = 1000

    parts = protocol.encode_parts("r1", text, protocol.FINAL, limit)

    assert len(parts) > 1
    assert all(len(part) <= limit for part in parts)

    reassembler = protocol.Reassembler()
    envelopes = [reassembler.add(protocol.decode(part)) for part in parts]
    assert envelopes[:-1] == [None] * (len(parts) - 1)
    assert envelopes[-1] == protocol.Envelope("r1", text, protocol.FINAL)

    assert len(protocol.encode(protocol.Envelope("r1", ENGLISH))) < len(ENGLISH) * 0.8


def test_decode_passes_plain_text_through_and_rejects_a_corrupt_envelope():
    assert protocol.decode("/pwd") is None

    with pytest.raises(ValueError, match="malformed"):
        protocol.decode(protocol.encode(protocol.Envelope("r1", "hello"))[:-3])
