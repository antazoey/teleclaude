import asyncio
import hashlib
import json
import hmac
import re
import time
import tomllib
import urllib.parse
from pathlib import Path
from unittest import mock

import pytest

import daemon
import protocol
from channels import Inbound
from config import Config

CLEAN_CONFIG = Config({"daemon": {"clean_script": "/opt/clean.sh", "clean_status": "/opt/clean-status.json"}})
PLAIN_REQUEST = daemon.Request(7, None)
CLEAN_STATUS = {
    "finished_at": "2026-09-04T08:33:05Z",
    "freed_mb_total": 9487,
    "freed_mb": {"backend": 5161, "docs": 0, "frontend": 4326},
    "disk_available_mb": 37695,
    "disk_used_mb": 57391,
    "disk_size_mb": 100221,
    "sync_merged": 6,
    "sync_conflicts": 8,
}
CLEAN_RAN_AT = 1788510785  # the finished_at above, as epoch seconds


def sent_texts(channel):
    return [call.args[1] for call in channel.send.call_args_list]


def declared_dependencies(script_name):
    source = (Path(daemon.__file__).parent / script_name).read_text()
    block = re.search(r"# /// script\n(.*?)^# ///", source, re.MULTILINE | re.DOTALL).group(1)
    metadata = "\n".join(line.removeprefix("# ").removeprefix("#") for line in block.splitlines())
    return {re.split(r"[><=!~\[]", requirement)[0] for requirement in tomllib.loads(metadata)["dependencies"]}


class EmptyStream:
    """A pipe that is open but never carries anything, like an unused stderr."""

    async def read(self, count=-1):
        return b""

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class FakeStream:
    """A claude process that answers each streamed prompt with one result event."""

    def __init__(self):
        self.stdin = self
        # A real reader, so the tests exercise the same chunking the pipes get.
        self.stdout = asyncio.StreamReader()
        self.stderr = EmptyStream()
        self.returncode = None
        self.prompts = []

    def write(self, payload):
        prompt = json.loads(payload.decode())["message"]["content"]
        self.prompts.append(prompt)
        event = json.dumps(
            {"type": "result", "result": f"answer to {prompt}", "session_id": "s1"}
        )
        self.stdout.feed_data(event.encode() + b"\n")

    async def drain(self):
        return None

    def close(self):
        return None

    async def wait(self):
        return 0


async def drive(service, prompts):
    stream = FakeStream()

    async def fake_exec(*args, **kwargs):
        return stream

    with mock.patch.object(asyncio, "create_subprocess_exec", fake_exec):
        for message_id, prompt in enumerate(prompts, start=100):
            await service.handle_turn(daemon.Request(7, message_id), prompt)

        while service.session.pending:
            await asyncio.sleep(0)

    service.session.reader.cancel()
    return stream


@pytest.mark.asyncio
async def test_handle_turn_streams_every_prompt_into_one_process(mocker):
    channel = mocker.AsyncMock()
    service = daemon.Daemon(channel, daemon.Session("/tmp"), "claude", True)

    stream = await drive(service, ["first", "second", "third"])

    # One process took all three, rather than one process per turn.
    assert stream.prompts == ["first", "second", "third"]
    answers = [call for call in channel.send.call_args_list if call.args[1].startswith("answer to")]
    assert [call.args[1] for call in answers] == [
        "answer to first",
        "answer to second",
        "answer to third",
    ]
    assert [call.kwargs["reply_to"] for call in answers] == [100, 101, 102]


@pytest.mark.asyncio
async def test_handle_streams_an_image_as_content_blocks(mocker):
    channel = mocker.AsyncMock()
    service = daemon.Daemon(channel, daemon.Session("/tmp"), "claude", True)
    stream = FakeStream()

    async def fake_exec(*args, **kwargs):
        return stream

    image = {"media_type": "image/jpeg", "data": "QUJD"}
    with mock.patch.object(asyncio, "create_subprocess_exec", fake_exec):
        await service.handle(Inbound(7, "look", 100, (image,)))
        while service.session.pending:
            await asyncio.sleep(0)

    service.session.reader.cancel()
    content = stream.prompts[0]
    assert content[0] == {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "QUJD"}}
    assert content[-1] == {"type": "text", "text": "The user sent an image.\n\nlook"}


def test_user_message_flags_an_image_even_without_a_caption():
    image = {"media_type": "image/png", "data": "QUJD"}
    content = json.loads(daemon.user_message("", (image,)))["message"]["content"]
    assert content == [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}},
        {"type": "text", "text": "The user sent an image."},
    ]


@pytest.mark.asyncio
async def test_handle_answers_an_envelope_in_kind(mocker):
    channel = mocker.AsyncMock(max_message_chars=200)
    service = daemon.Daemon(channel, daemon.Session("/tmp"), "claude", True)
    model = "".join(hashlib.sha256(str(index).encode()).hexdigest() for index in range(8))
    parts = protocol.encode_parts("abc", f"/model {model}", None, channel.max_message_chars)
    assert len(parts) > 1

    for message_id, part in enumerate(parts):
        await service.handle(Inbound(7, part, message_id))

    reassembler = protocol.Reassembler()
    replies = [reassembler.add(protocol.decode(text)) for text in sent_texts(channel)]
    final = [reply for reply in replies if reply][-1]
    assert (final.request_id, final.kind) == ("abc", protocol.FINAL)
    assert f"> {model}" in final.text
    assert service.session.model == model


@pytest.mark.asyncio
async def test_handle_turn_does_not_wait_for_the_running_prompt(mocker):
    channel = mocker.AsyncMock()
    service = daemon.Daemon(channel, daemon.Session("/tmp"), "claude", True)
    stream = FakeStream()

    async def fake_exec(*args, **kwargs):
        return stream

    with mock.patch.object(asyncio, "create_subprocess_exec", fake_exec):
        await service.handle_turn(PLAIN_REQUEST, "first")
        # The reader has not run yet, so "first" is still in flight.
        await service.handle_turn(PLAIN_REQUEST, "second")

    # Both reached claude without the second waiting on the first's answer.
    assert stream.prompts == ["first", "second"]
    assert f"{daemon.WORKING_PREFIX}> second" in sent_texts(channel)
    service.session.reader.cancel()


@pytest.mark.asyncio
async def test_upgrade_restarts_only_when_every_check_passes(mocker):
    run_check = mocker.patch.object(daemon, "run_check", return_value=(False, "2 failed, 1 passed"))
    restart = mocker.patch.object(daemon.Daemon, "restart")
    channel = mocker.AsyncMock()
    service = daemon.Daemon(channel, daemon.Session("/tmp"), "claude", True)

    await service.upgrade(PLAIN_REQUEST)

    restart.assert_not_called()
    assert "2 failed, 1 passed" in sent_texts(channel)[-1]

    # The restart runs under the interpreter the preflight reported.
    run_check.return_value = (True, f"Installed 2 packages\npreflight ok\n{daemon.PREFLIGHT_PYTHON_PREFIX}/envs/new/bin/python3\n")
    await service.upgrade(PLAIN_REQUEST)

    restart.assert_called_once_with(7, "/envs/new/bin/python3")


def test_boot_greeting_announces_the_running_version():
    assert daemon.boot_greeting() == f"Hello! This is teleclaude v{daemon.VERSION}, up on the new code."


def test_record_deploy_appends_a_line_per_boot(mocker, tmp_path):
    log = tmp_path / "deploy.log"
    mocker.patch.object(daemon, "DEPLOY_LOG", log)

    daemon.record_deploy("0.0.6")
    daemon.record_deploy("0.0.7")

    lines = log.read_text().splitlines()
    assert len(lines) == 2
    assert lines[0].endswith(" v0.0.6") and lines[1].endswith(" v0.0.7")


def test_bump_version_increments_the_patch(mocker, tmp_path):
    fake = tmp_path / "daemon.py"
    fake.write_text('VERSION = "0.0.3"\nWORKING_PREFIX = "x"\n')
    mocker.patch.object(daemon, "SCRIPT_PATH", fake)

    assert daemon.bump_version() == "0.0.4"
    assert 'VERSION = "0.0.4"' in fake.read_text()


@pytest.mark.asyncio
async def test_publish_upgrade_bumps_version_then_commits_and_pushes(mocker):
    service = daemon.Daemon(mocker.AsyncMock(), daemon.Session("/tmp"), "claude", True)
    mocker.patch.object(daemon, "bump_version", return_value="9.9.9")
    calls = []

    async def fake_run_check(command):
        calls.append(command)
        return (command[:3] != ["git", "diff", "--cached"]), ""

    mocker.patch.object(daemon, "run_check", fake_run_check)
    status = await service.publish_upgrade()

    assert ["git", "add", "-A"] in calls
    assert ["git", "commit", "-m", "teleclaude v9.9.9"] in calls
    assert ["git", "push"] in calls
    assert "9.9.9" in status


@pytest.mark.asyncio
async def test_publish_upgrade_skips_when_nothing_changed(mocker):
    service = daemon.Daemon(mocker.AsyncMock(), daemon.Session("/tmp"), "claude", True)

    async def fake_run_check(command):
        return True, ""

    mocker.patch.object(daemon, "run_check", fake_run_check)
    assert "No code changes" in await service.publish_upgrade()


def test_pick_initial_response_routes_by_topic():
    assert daemon.pick_initial_response("can you do a PR review on tplus-core") in daemon.SASSY_RESPONSES
    assert daemon.pick_initial_response("update the Soulaire evidence for spywear") in daemon.GENTLE_RESPONSES
    # The investigation wins when a prompt trips both sets, so it never gets a sassy opener.
    assert daemon.pick_initial_response("review the spywear transcript") in daemon.GENTLE_RESPONSES
    assert daemon.pick_initial_response("what time is it") in daemon.NEUTRAL_RESPONSES


def test_ordinal_handles_ones_and_teens():
    assert [daemon.ordinal(n) for n in (1, 2, 3, 4, 11, 13, 22, 23)] == \
        ["1st", "2nd", "3rd", "4th", "11th", "13th", "22nd", "23rd"]


@pytest.mark.asyncio
async def test_handle_turn_queues_a_request_behind_a_running_one(mocker):
    channel = mocker.AsyncMock()
    service = daemon.Daemon(channel, daemon.Session("/tmp"), "claude", True)
    stream = FakeStream()

    async def fake_exec(*args, **kwargs):
        return stream

    with mock.patch.object(asyncio, "create_subprocess_exec", fake_exec):
        service.session.pending.append(daemon.Request(7, 1))
        await service.handle_turn(daemon.Request(7, 2), "second")

    service.session.reader.cancel()
    assert any("2nd in line" in call.args[1] for call in channel.send.call_args_list)


@pytest.mark.asyncio
async def test_handle_turn_opens_with_an_initial_response(mocker):
    channel = mocker.AsyncMock()
    service = daemon.Daemon(channel, daemon.Session("/tmp"), "claude", True)
    stream = FakeStream()

    async def fake_exec(*args, **kwargs):
        return stream

    with mock.patch.object(asyncio, "create_subprocess_exec", fake_exec):
        await service.handle_turn(PLAIN_REQUEST, "hello")

    service.session.reader.cancel()
    openers = daemon.SASSY_RESPONSES + daemon.GENTLE_RESPONSES + daemon.NEUTRAL_RESPONSES
    assert any(call.args[1] in openers for call in channel.send.call_args_list)


@pytest.mark.asyncio
async def test_reply_marks_working_lines_apart_from_the_answer(mocker):
    channel = mocker.AsyncMock()
    service = daemon.Daemon(channel, daemon.Session("/tmp"), "claude", True)

    await service.reply(PLAIN_REQUEST, "Reading foo.py…", final=False)
    await service.reply(PLAIN_REQUEST, "here is the answer", final=True)

    working, answer = channel.send.call_args_list
    assert working.args[1] == f"{daemon.WORKING_PREFIX}Reading foo.py…"
    assert answer.args[1] == "here is the answer"


def test_restart_environment_carries_the_conversation(mocker):
    service = daemon.Daemon(mocker.AsyncMock(), daemon.Session("/tmp"), "claude", True)
    service.session.session_id = "abc123"
    service.channel.cursor = 42

    environment = service.restart_environment(7)

    assert environment[daemon.RESTART_KEYS["session"]] == "abc123"
    assert environment[daemon.RESTART_KEYS["cursor"]] == "42"
    assert environment[daemon.RESTART_KEYS["cwd"]] == "/tmp"


@pytest.mark.asyncio
async def test_stop_everything_drops_what_is_in_flight(mocker):
    channel = mocker.AsyncMock()
    service = daemon.Daemon(channel, daemon.Session("/tmp"), "claude", True)
    service.session.pending.extend([daemon.Request(7, 1), daemon.Request(7, 2), daemon.Request(7, 3)])

    assert service.stop_everything() == "Dropped 3 in flight."
    assert not service.session.pending
    assert service.stop_everything() == "Nothing running."


def test_render_tool_hides_the_command_until_debug(mocker):
    service = daemon.Daemon(mocker.AsyncMock(), daemon.Session("/tmp"), "claude", True)
    block = {
        "name": "Bash",
        "input": {"command": "gh pr checks 42 --repo example/api", "description": "Checking the api repo"},
    }

    assert service.render_tool(block) == "Checking the api repo…"

    service.set_debug("on")
    assert service.render_tool(block) == "[Bash] gh pr checks 42 --repo example/api"


def test_narrate_tool_rewrites_the_description_as_an_action():
    def narration(description):
        return daemon.narrate_tool({"name": "Bash", "input": {"command": "ls", "description": description}})

    assert narration("Read daemon.py") == "Reading daemon.py…"
    assert narration("Checking the api repo") == "Checking the api repo…"
    assert narration("Run the test suite") == "Running the test suite…"
    assert narration("Trace the process tree") == "Tracing the process tree…"


def test_narrate_tool_falls_back_when_no_description():
    assert daemon.narrate_tool({"name": "Read", "input": {"file_path": "/home/user/teleclaude/daemon.py"}}) == "Reading daemon.py…"
    assert daemon.narrate_tool({"name": "Grep", "input": {"pattern": "teleclaude"}}) == "Searching for teleclaude…"
    assert daemon.narrate_tool({"name": "Bash", "input": {"command": "ls"}}) == "Running a command…"


def test_test_command_installs_every_declared_dependency():
    installed = {package for flag, package in zip(daemon.TEST_COMMAND, daemon.TEST_COMMAND[1:]) if flag == "--with"}

    assert declared_dependencies("daemon.py") | declared_dependencies("tui.py") | declared_dependencies("teleclaude.py") <= installed


def test_diff_viewer_link_signs_the_expiry_the_viewer_checks():
    link = daemon.diff_viewer_link("https://example.com/", "shhh", "org/repo/my/branch", now=1_000_000)
    parsed = urllib.parse.urlparse(link)
    query = urllib.parse.parse_qs(parsed.query)

    assert parsed.path == "/org/repo/my/branch"
    expires = query["exp"][0]
    assert int(expires) == 1_000_000 + daemon.DIFF_LINK_TTL_SECONDS
    assert query["sig"][0] == hmac.new(b"shhh", expires.encode(), hashlib.sha256).hexdigest()


@pytest.mark.asyncio
async def test_handle_command_diff_sends_a_link_button(mocker):
    channel = mocker.AsyncMock()
    config = Config({"daemon": {"diff_url": "https://example.com", "diff_auth_key": "shhh"}})
    service = daemon.Daemon(channel, daemon.Session("/tmp"), "claude", True, config=config)

    await service.handle_command(PLAIN_REQUEST, "/diff org/repo/branch")

    chat_id, text, label, url = channel.send_link.call_args.args
    assert (chat_id, label) == (7, "Open")
    assert "branch" in text
    assert url.startswith("https://example.com/org/repo/branch?exp=")


@pytest.mark.asyncio
async def test_handle_command_diff_without_config_explains_itself(mocker, monkeypatch):
    monkeypatch.delenv("TELECLAUDE_DIFF_URL", raising=False)
    monkeypatch.delenv("TELECLAUDE_DIFF_AUTH_KEY", raising=False)
    channel = mocker.AsyncMock()
    service = daemon.Daemon(channel, daemon.Session("/tmp"), "claude", True)

    await service.handle_command(PLAIN_REQUEST, "/diff")

    assert "daemon.diff_url" in sent_texts(channel)[0]
    channel.send_link.assert_not_called()


@pytest.mark.asyncio
async def test_handle_turn_starts_one_process_for_concurrent_prompts(mocker):
    channel = mocker.AsyncMock()
    service = daemon.Daemon(channel, daemon.Session("/tmp"), "claude", True)
    stream = FakeStream()
    spawns = 0

    async def fake_exec(*args, **kwargs):
        nonlocal spawns
        spawns += 1
        await asyncio.sleep(0)  # let the other prompt interleave mid-spawn
        return stream

    with mock.patch.object(asyncio, "create_subprocess_exec", fake_exec):
        await asyncio.gather(service.handle_turn(PLAIN_REQUEST, "a"), service.handle_turn(PLAIN_REQUEST, "b"))

    assert spawns == 1
    assert sorted(stream.prompts) == ["a", "b"]
    service.session.reader.cancel()


@pytest.mark.asyncio
async def test_handle_turn_reads_lines_past_asyncio_default_limit(mocker):
    """A single claude event carrying a big tool result must not overrun the reader."""
    channel = mocker.AsyncMock()
    service = daemon.Daemon(channel, daemon.Session("/tmp"), "claude", True)
    stream = FakeStream()
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured.update(kwargs)
        return stream

    with mock.patch.object(asyncio, "create_subprocess_exec", fake_exec):
        await service.handle_turn(PLAIN_REQUEST, "hello")

    assert captured["limit"] > asyncio.streams._DEFAULT_LIMIT
    service.session.reader.cancel()


def test_render_models_marks_the_one_in_use():
    listing = daemon.render_models("sonnet")

    assert listing.startswith("Model: sonnet")
    assert "> sonnet" in listing
    assert "  opus" in listing

    # No selection means the CLI's own default is the active row.
    assert "> default" in daemon.render_models(None)


def test_render_models_shows_an_unlisted_model_as_passed_through():
    listing = daemon.render_models("claude-opus-4-8")

    assert "> claude-opus-4-8" in listing
    assert "passed straight to the CLI" in listing


def test_claude_command_names_the_model_only_when_one_is_chosen():
    session = daemon.Session("/tmp")
    assert "--model" not in daemon.claude_command(session, "claude", False)

    session.model = "opus"
    command = daemon.claude_command(session, "claude", False)
    assert command[command.index("--model") + 1] == "opus"


@pytest.mark.asyncio
async def test_handle_command_model_switches_and_resets(mocker):
    channel = mocker.AsyncMock()
    service = daemon.Daemon(channel, daemon.Session("/tmp"), "claude", True)

    await service.handle_command(PLAIN_REQUEST, "/model haiku")
    assert service.session.model == "haiku"
    assert "> haiku" in sent_texts(channel)[-1]

    await service.handle_command(PLAIN_REQUEST, "/model default")
    assert service.session.model is None
    assert "> default" in sent_texts(channel)[-1]


def make_reader(payload, limit):
    """A stream reader fed `payload`, limited the way the subprocess pipes are."""
    reader = asyncio.StreamReader(limit=limit)
    reader.feed_data(payload)
    reader.feed_eof()
    return reader


@pytest.mark.asyncio
async def test_stream_json_lines_reads_an_event_larger_than_the_stream_limit():
    limit = 4096
    huge = json.dumps({"type": "result", "result": "x" * (limit * 3)}).encode()
    payload = b'{"type":"assistant"}\n' + huge + b"\n" + b'{"type":"done"}\n'

    lines = [
        line async for line in daemon.stream_json_lines(make_reader(payload, limit))
    ]

    assert [json.loads(line)["type"] for line in lines] == ["assistant", "result", "done"]
    assert json.loads(lines[1])["result"] == "x" * (limit * 3)


@pytest.mark.asyncio
async def test_heartbeat_forever_pings_only_once_the_chat_has_gone_quiet(mocker):
    """The heartbeat invariant: a waiting chat hears something even when claude emits nothing."""
    mocker.patch.object(daemon, "HEARTBEAT_TICK_SECONDS", 0.01)
    channel = mocker.AsyncMock()
    service = daemon.Daemon(channel, daemon.Session("/tmp"), "claude", True)
    service.session.pending.append(daemon.Request(7, None))
    service.stream_idle = False
    service.turn_started_at = time.monotonic() - 90
    service.last_sent_at = time.monotonic()
    service.session.stderr_tail.append("API Error: 529 {\"type\":\"overloaded_error\"}\n")

    heartbeat = asyncio.create_task(service.heartbeat_forever())
    await asyncio.sleep(0.05)
    assert sent_texts(channel) == [], "pinged a chat that had just been spoken to"

    service.last_sent_at = time.monotonic() - daemon.HEARTBEAT_SECONDS
    await asyncio.sleep(0.05)
    heartbeat.cancel()

    ping = sent_texts(channel)[0]
    assert "still thinking" in ping
    assert "1m30s" in ping
    assert "529" in ping, "the upstream failure was in stderr but never reached the user"


def test_stall_hint_picks_the_newest_upstream_failure_out_of_the_tail():
    tail = [
        "API Error: 503 service unavailable\n",
        "some ordinary chatter\n",
        "API Error: 529 overloaded_error\n",
        "reading file\n",
    ]

    assert daemon.stall_hint(tail) == "API Error: 529 overloaded_error"
    assert daemon.stall_hint(["reading file\n", "wrote 3 lines\n"]) is None


@pytest.mark.asyncio
async def test_stream_json_lines_yields_a_trailing_event_without_a_newline():
    reader = make_reader(b'{"a":1}\n{"b":2}', 4096)

    lines = [line async for line in daemon.stream_json_lines(reader)]

    assert lines == [b'{"a":1}', b'{"b":2}']


def test_render_clean_status_reports_the_run_and_warns_once_it_is_stale():
    fresh = daemon.render_clean_status(CLEAN_STATUS, now=CLEAN_RAN_AT + 3 * 3600)
    assert "3h ago" in fresh
    assert "9.3G" in fresh and "36.8G free (57% used)" in fresh
    assert "backend 5.0G, frontend 4.2G" in fresh
    assert "docs" not in fresh
    assert "6 merged, 8 conflicted" in fresh
    assert "check the cron" not in fresh

    # A cron that stopped firing is the thing this command exists to surface.
    stale = daemon.render_clean_status(
        CLEAN_STATUS, now=CLEAN_RAN_AT + (daemon.CLEAN_STALE_HOURS + 1) * 3600
    )
    assert "check the cron" in stale


def test_render_clean_status_explains_an_absent_or_unreadable_file(tmp_path):
    assert "never have run" in daemon.render_clean_status(None)
    assert daemon.read_clean_status(tmp_path / "missing.json") is None

    unreadable = tmp_path / "bad.json"
    unreadable.write_text("{not json")
    assert daemon.read_clean_status(unreadable) is None


@pytest.mark.asyncio
async def test_handle_command_clean_shows_the_last_run_then_starts_one(mocker):
    mocker.patch.object(daemon, "read_clean_status", return_value=CLEAN_STATUS)
    run_check = mocker.patch.object(daemon, "run_check", return_value=(True, ""))
    channel = mocker.AsyncMock()
    service = daemon.Daemon(channel, daemon.Session("/tmp"), "claude", True, config=CLEAN_CONFIG)

    await service.handle_command(PLAIN_REQUEST, "/clean")

    # The status comes first, so a run never hides when the last one was.
    assert "cleanup ran" in sent_texts(channel)[0]
    assert run_check.call_args.args[0] == ["/opt/clean.sh", "--force"]


@pytest.mark.asyncio
async def test_handle_command_clean_only_show_runs_nothing(mocker):
    mocker.patch.object(daemon, "read_clean_status", return_value=CLEAN_STATUS)
    run_check = mocker.patch.object(daemon, "run_check", return_value=(True, ""))
    channel = mocker.AsyncMock()
    service = daemon.Daemon(channel, daemon.Session("/tmp"), "claude", True, config=CLEAN_CONFIG)

    await service.handle_command(PLAIN_REQUEST, "/clean --only-show")
    assert "cleanup ran" in sent_texts(channel)[0]
    run_check.assert_not_called()

    # An unrecognised option must not fall through into a clean either.
    await service.handle_command(PLAIN_REQUEST, "/clean --oops")
    run_check.assert_not_called()


@pytest.mark.asyncio
async def test_handle_command_clean_refuses_while_a_turn_is_running(mocker):
    mocker.patch.object(daemon, "read_clean_status", return_value=CLEAN_STATUS)
    run_check = mocker.patch.object(daemon, "run_check", return_value=(True, ""))
    channel = mocker.AsyncMock()
    service = daemon.Daemon(channel, daemon.Session("/tmp"), "claude", True, config=CLEAN_CONFIG)
    mocker.patch.object(type(service.session), "busy", property(lambda self: True))

    await service.handle_command(PLAIN_REQUEST, "/clean")

    run_check.assert_not_called()
    assert "Busy" in sent_texts(channel)[-1]


def test_resume_session_adopts_a_pushed_transcript(mocker, tmp_path):
    mocker.patch.object(daemon, "CLAUDE_PROJECTS_DIR", tmp_path)
    project = tmp_path / "-tmp"
    project.mkdir()
    (project / "abc123.jsonl").write_text("{}\n")
    service = daemon.Daemon(mocker.AsyncMock(), daemon.Session("/tmp"), "claude", True)

    assert "Resumed abc123" in service.resume_session("abc123")
    assert service.session.session_id == "abc123"

    assert "No transcript" in service.resume_session("missing")
    assert service.session.session_id == "abc123"


@pytest.mark.parametrize(
    "text, stopping",
    [
        ("stop", True),
        ("Stop!", True),
        ("please stop", True),
        ("never mind", True),
        ("stop using tabs in that file", False),
        ("why did the deploy stop", False),
        ("", False),
    ],
)
def test_is_stop_request_matches_a_bare_stop_only(text, stopping):
    assert daemon.is_stop_request(text) is stopping


@pytest.mark.asyncio
async def test_enforce_timeout_gives_up_only_on_a_stalled_turn(mocker):
    channel = mocker.AsyncMock()
    service = daemon.Daemon(channel, daemon.Session("/tmp"), "claude", True)
    service.session.process = mocker.Mock(returncode=None)
    service.session.pending.append(daemon.Request(1, 55))
    now = time.monotonic()

    # Long-running but still progressing.
    service.turn_started_at = now - 3 * 3600
    service.last_progress_at = now - 60
    assert await service.enforce_timeout() is False

    service.last_progress_at = now - daemon.STALL_TIMEOUT_SECONDS - 1
    service.session.patient = daemon.mentions_long_wait("monitor the CI run until it finishes")
    assert await service.enforce_timeout() is False

    service.last_progress_at = now - daemon.PATIENT_STALL_TIMEOUT_SECONDS - 1
    service.set_timeout("off")
    assert await service.enforce_timeout() is False
    assert service.session.pending

    service.set_timeout("on")
    assert await service.enforce_timeout() is True
    assert not service.session.pending
    service.session.process.terminate.assert_called_once()
    assert "Gave up after 1h00m without progress" in sent_texts(channel)[-1]
    assert channel.send.call_args.kwargs["reply_to"] == 55


@pytest.mark.asyncio
async def test_track_progress_ignores_a_polling_loop(mocker):
    service = daemon.Daemon(mocker.AsyncMock(), daemon.Session("/tmp"), "claude", True)
    service.session.pending.append(daemon.Request(7, None))
    stale = time.monotonic() - 600

    def tool_event(command):
        block = {"type": "tool_use", "name": "Bash", "input": {"command": command}}
        return {"type": "assistant", "message": {"content": [block]}}

    for command in ("sleep 60", "gh pr checks 42", "sleep 60", "gh pr checks 42"):
        service.last_progress_at = stale
        await service.dispatch(tool_event(command))

    # The loop's first pass was new work; repeating it is not.
    assert service.last_progress_at == stale
    assert service.patient

    await service.dispatch(tool_event("cargo fmt"))
    assert service.last_progress_at > stale
    assert not service.patient


@pytest.mark.asyncio
async def test_dispatch_drops_waiters_a_folded_turn_answered(mocker):
    channel = mocker.AsyncMock()
    service = daemon.Daemon(channel, daemon.Session("/tmp"), "claude", True)
    # Three messages sent mid-turn queue three waiters; the CLI folds them into one turn.
    service.session.pending.extend([daemon.Request(7, 1), daemon.Request(7, 2), daemon.Request(7, 3)])
    service.stream_idle = False

    await service.dispatch({"type": "result", "result": "done"})

    assert service.stream_idle is True
    assert len(service.session.pending) == 2
    assert service.session.busy

    # Nothing is running, so the leftovers are forgotten rather than announced forever.
    service.idle_since = time.monotonic() - daemon.IDLE_DRAIN_SECONDS - 1
    assert service.drop_orphaned_replies() is True
    assert not service.session.busy


@pytest.mark.asyncio
async def test_dispatch_keeps_the_stream_busy_while_tools_run(mocker):
    channel = mocker.AsyncMock()
    service = daemon.Daemon(channel, daemon.Session("/tmp"), "claude", True)
    service.session.pending.append(daemon.Request(7, None))
    service.stream_idle = True

    await service.dispatch({"type": "assistant", "message": {"content": []}})

    assert service.stream_idle is False
    assert service.drop_orphaned_replies() is False
