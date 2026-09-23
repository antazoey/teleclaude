import base64
import os

import pytest

from channels import MAX_MESSAGE_CHARS, RECEIVERS, SENDERS, TelegramChannelReceiver, TelegramChannelSender, find_channel
from config import Config


@pytest.mark.asyncio
async def test_send_threads_every_chunk_under_the_prompt(mocker):
    receiver = TelegramChannelReceiver("token", mocker.AsyncMock())
    call = mocker.patch.object(receiver, "call")

    await receiver.send(7, "word\n" * MAX_MESSAGE_CHARS, reply_to=55)
    await receiver.send(7, "narration")

    threaded, plain = call.call_args_list[:-1], call.call_args_list[-1]
    assert len(threaded) > 1
    assert all(chunk.kwargs["reply_parameters"]["message_id"] == 55 for chunk in threaded)
    assert "reply_parameters" not in plain.kwargs


@pytest.mark.asyncio
async def test_admit_downloads_the_largest_photo_as_an_image_prompt(mocker):
    receiver = TelegramChannelReceiver("token", mocker.AsyncMock(), allowed_user_id=42)
    mocker.patch.object(receiver, "call", return_value={"file_path": "photos/big.jpg"})
    receiver.http.get.return_value = mocker.Mock(content=b"\xff\xd8\xff", raise_for_status=mocker.Mock())

    inbound = await receiver.admit({
        "chat": {"id": 7},
        "from": {"id": 42},
        "message_id": 9,
        "caption": "what is this?",
        "photo": [{"file_id": "small"}, {"file_id": "large"}],
    })

    receiver.call.assert_awaited_once_with("getFile", file_id="large")
    assert inbound.text == "what is this?"
    assert inbound.images == ({"media_type": "image/jpeg", "data": base64.b64encode(b"\xff\xd8\xff").decode()},)



@pytest.mark.asyncio
async def test_admit_downloads_a_captionless_voice_memo(mocker):
    receiver = TelegramChannelReceiver("token", mocker.AsyncMock(), allowed_user_id=42)
    mocker.patch.object(receiver, "call", return_value={"file_path": "voice/memo.oga"})
    receiver.http.get.return_value = mocker.Mock(content=b"OggS", raise_for_status=mocker.Mock())

    inbound = await receiver.admit({"chat": {"id": 7}, "from": {"id": 42}, "message_id": 9, "voice": {"file_id": "memo"}})

    receiver.call.assert_awaited_once_with("getFile", file_id="memo")
    assert (inbound.text, inbound.images, inbound.voices) == ("", (), (b"OggS",))

@pytest.mark.asyncio
async def test_admit_carries_a_telegram_reply_quote(mocker):
    receiver = TelegramChannelReceiver("token", mocker.AsyncMock(), allowed_user_id=42)

    inbound = await receiver.admit({
        "chat": {"id": 7},
        "from": {"id": 42},
        "message_id": 9,
        "text": "yes, that one",
        "reply_to_message": {"text": "Which mission should I enrich, 60 or 61?"},
    })

    assert inbound.text == "yes, that one"
    assert inbound.reply_quote == "Which mission should I enrich, 60 or 61?"


@pytest.mark.asyncio
async def test_send_renders_markdown_unless_verbatim(mocker):
    receiver = TelegramChannelReceiver("token", mocker.AsyncMock())
    call = mocker.patch.object(receiver, "call")

    await receiver.send(7, "Use `depth`:\n```rust src/book.rs:12\nfn depth() {}\n```")
    await receiver.send(7, "tc1:`a`**b**", verbatim=True)

    rendered, envelope = call.call_args_list
    assert [entity["type"] for entity in rendered.kwargs["entities"]] == ["code", "pre"]
    assert rendered.kwargs["text"].startswith("Use depth:\n╭─")
    assert envelope.kwargs["text"] == "tc1:`a`**b**"
    assert "entities" not in envelope.kwargs


def test_find_channel_defaults_to_telegram_and_rejects_unknown_names():
    assert find_channel(RECEIVERS, Config()) is TelegramChannelReceiver
    assert find_channel(SENDERS, Config()) is TelegramChannelSender

    with pytest.raises(SystemExit, match="Registered: telegram"):
        find_channel(RECEIVERS, Config({"channel": "carrier-pigeon"}))


def test_sender_from_config_reads_the_app_id_and_key_aliases(mocker):
    environment = {"TELECLAUDE_APP_ID": "12345678", "TELEGRAM_API_KEY": "0123456789abcdef0123456789abcdef"}
    mocker.patch.dict(os.environ, environment, clear=True)

    sender = TelegramChannelSender.from_config(Config({"telegram": {"bot_username": "@my_bot"}}))

    assert (sender.api_id, sender.api_hash, sender.bot_username) == (12345678, environment["TELEGRAM_API_KEY"], "my_bot")
