import json

import pytest

import daemon
import tui


def transcript_lines(pane):
    return [strip.text.strip() for strip in pane.transcript.lines]


def test_load_actions_falls_back_when_unreadable(tmp_path):
    missing = tmp_path / "absent.json"
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{not json")

    assert tui.load_actions(missing) == tui.DEFAULT_ACTIONS
    assert tui.load_actions(malformed) == tui.DEFAULT_ACTIONS


def test_load_actions_drops_entries_missing_a_field(tmp_path):
    path = tmp_path / "actions.json"
    path.write_text(
        json.dumps(
            [
                {"label": "Keep", "prompt": "do the thing"},
                {"label": "No prompt"},
                {"prompt": "no label"},
            ]
        )
    )

    assert tui.load_actions(path) == [{"label": "Keep", "prompt": "do the thing"}]


@pytest.mark.asyncio
async def test_run_turn_narrates_tools_then_the_answer(mocker, tmp_path):
    async def answer(session, prompt, claude_bin, skip_permissions, on_tool):
        await on_tool({"name": "Bash", "input": {"description": "Check the branch"}})
        return "all clean"

    mocker.patch.object(daemon, "run_claude", side_effect=answer)
    app = tui.TeleclaudeTui(tmp_path, "claude")

    async with app.run_test() as pilot:
        pane = app.active_pane
        pane.run_turn("what is up")
        await pilot.pause()
        await app.workers.wait_for_complete()
        lines = [line for line in transcript_lines(pane) if line]

    assert lines == ["> what is up", "Checking the branch…", "all clean"]


@pytest.mark.asyncio
async def test_run_turn_reports_a_failure_without_crashing(mocker, tmp_path):
    mocker.patch.object(daemon, "run_claude", side_effect=FileNotFoundError("no claude here"))
    app = tui.TeleclaudeTui(tmp_path, "claude")

    async with app.run_test() as pilot:
        pane = app.active_pane
        pane.run_turn("anything")
        await pilot.pause()
        await app.workers.wait_for_complete()
        lines = transcript_lines(pane)
        claimed = pane.session.claimed

    assert "FileNotFoundError: no claude here" in "\n".join(lines)
    assert not claimed


@pytest.mark.asyncio
async def test_run_sidebar_action_runs_the_prompt_after_a_binding_fires(mocker, tmp_path):
    async def answer(session, prompt, claude_bin, skip_permissions, on_tool):
        return f"ran {prompt}"

    mocker.patch.object(daemon, "run_claude", side_effect=answer)
    app = tui.TeleclaudeTui(tmp_path, "claude")

    async with app.run_test() as pilot:
        await pilot.press("ctrl+n")
        await pilot.pause()
        tab_count = app.query_one("#panes").tab_count

        await pilot.click("#action-0")
        await pilot.pause()
        await app.workers.wait_for_complete()
        lines = [line for line in transcript_lines(app.active_pane) if line]

    assert tab_count == 2
    assert lines[-1] == f"ran {tui.DEFAULT_ACTIONS[0]['prompt']}"
