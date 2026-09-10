import protocol
import teleclaude


async def test_collect_answer_returns_only_the_final_reply_to_its_request(mocker):
    long_answer = "all green\n" * 200
    messages = [
        "Paired with 1. Everyone else is ignored now.",
        protocol.encode(protocol.Envelope("other", "someone else's answer", protocol.FINAL)),
        protocol.encode(protocol.Envelope("mine", "Running the tests…", protocol.PROGRESS)),
        "tc1:not-an-envelope",
        *protocol.encode_parts("mine", long_answer, protocol.FINAL, 300),
    ]
    sender = mocker.AsyncMock()
    sender.next_message.side_effect = [*messages, None]
    progress = []

    assert await teleclaude.collect_answer(sender, "mine", progress.append) == long_answer
    assert progress == ["Running the tests…"]

    sender.next_message.side_effect = [None]
    assert await teleclaude.collect_answer(sender, "mine", progress.append) is None
