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


def test_find_channel_defaults_to_telegram_and_rejects_unknown_names():
    assert find_channel(RECEIVERS, Config()) is TelegramChannelReceiver
    assert find_channel(SENDERS, Config()) is TelegramChannelSender

    with pytest.raises(SystemExit, match="Registered: telegram"):
        find_channel(RECEIVERS, Config({"channel": "carrier-pigeon"}))
