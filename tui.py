# /// script
# requires-python = ">=3.11"
# dependencies = ["brotli>=1.1", "httpx>=0.27", "textual>=0.80"]
# ///
"""Terminal front end for the same Claude sessions the daemon drives."""

import json
from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Footer, Header, Input, Label, RichLog, TabbedContent, TabPane

import daemon
from config import Config

ACTIONS_PATH = Path(__file__).resolve().parent / "actions.json"
DEFAULT_ACTIONS = [
    {"label": "Status", "prompt": "Summarize git status in one line per changed area."},
    {"label": "Diff", "prompt": "Review the working tree diff and list only real problems."},
    {"label": "Tests", "prompt": "Run this project's test suite and report only failures."},
    {"label": "Catch up", "prompt": "Catch this branch up with main and push the new pins."},
]
ANSWER_STYLE = "bold white"
NARRATION_STYLE = "dim cyan"
PROMPT_STYLE = "bold green"
ERROR_STYLE = "bold red"


def load_actions(path=ACTIONS_PATH):
    """Reusable prompts shown as sidebar buttons."""
    if not path.exists():
        return list(DEFAULT_ACTIONS)

    try:
        actions = json.loads(path.read_text())
    except json.JSONDecodeError:
        return list(DEFAULT_ACTIONS)

    return [entry for entry in actions if entry.get("label") and entry.get("prompt")]


class ClaudePane(Vertical):
    """One Claude conversation with its own transcript and input."""

    def __init__(self, cwd, claude_bin, skip_permissions, **kwargs):
        super().__init__(**kwargs)
        self.session = daemon.Session(str(cwd))
        self.claude_bin = claude_bin
        self.skip_permissions = skip_permissions

    def compose(self) -> ComposeResult:
        yield RichLog(highlight=True, markup=True, wrap=True, id="transcript")
        yield Input(placeholder="Ask Claude…", id="prompt")

    @property
    def transcript(self):
        return self.query_one("#transcript", RichLog)

    def announce(self, text, style):
        self.transcript.write(f"[{style}]{text}[/{style}]")

    async def narrate(self, block):
        self.announce(daemon.narrate_tool(block), NARRATION_STYLE)

    @work(exclusive=True)
    async def run_turn(self, prompt):
        self.announce(f"> {daemon.summarize(prompt)}", PROMPT_STYLE)
        self.session.claimed = True
        try:
            answer = await daemon.run_claude(
                self.session, prompt, self.claude_bin, self.skip_permissions, self.narrate
            )
        except Exception as err:
            self.announce(f"{type(err).__name__}: {err}", ERROR_STYLE)
            return
        finally:
            self.session.claimed = False

        self.announce(answer, ANSWER_STYLE)

    @on(Input.Submitted, "#prompt")
    def submit_prompt(self, event):
        prompt = event.value.strip()
        event.input.value = ""
        if prompt:
            self.run_turn(prompt)


class TeleclaudeTui(App):
    """Concise terminal view over one or more Claude sessions."""

    CSS = """
    #body { height: 1fr; }
    #sidebar { width: 24; border-right: solid $accent; padding: 1; }
    #sidebar Button { width: 100%; margin-bottom: 1; }
    #transcript { height: 1fr; border: round $accent; padding: 0 1; }
    #prompt { dock: bottom; }
    """
    BINDINGS = [
        Binding("ctrl+n", "new_pane", "New session"),
        Binding("ctrl+s", "stop_turn", "Stop"),
        Binding("ctrl+r", "reload_actions", "Reload actions"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, cwd, claude_bin, skip_permissions=True):
        super().__init__()
        self.cwd = Path(cwd).resolve()
        self.claude_bin = claude_bin
        self.skip_permissions = skip_permissions
        self.actions = load_actions()
        self.pane_count = 0

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="body"):
            with VerticalScroll(id="sidebar"):
                yield Label(f"[dim]{self.cwd.name}[/dim]")
                for index, action in enumerate(self.actions):
                    yield Button(action["label"], id=f"action-{index}")

            yield TabbedContent(id="panes")

        yield Footer()

    async def on_mount(self):
        await self.action_new_pane()

    @property
    def active_pane(self):
        panes = self.query_one("#panes", TabbedContent)
        if panes.active_pane is None:
            return None

        return panes.active_pane.query_one(ClaudePane)

    async def action_new_pane(self):
        self.pane_count += 1
        title = f"{self.cwd.name} {self.pane_count}"
        pane = ClaudePane(self.cwd, self.claude_bin, self.skip_permissions)
        await self.query_one("#panes", TabbedContent).add_pane(TabPane(title, pane))

    def action_stop_turn(self):
        pane = self.active_pane
        if pane and pane.session.stop():
            pane.announce("Stopped.", ERROR_STYLE)

    def action_reload_actions(self):
        self.actions = load_actions()
        self.refresh(recompose=True)

    @on(Button.Pressed)
    def run_sidebar_action(self, event):
        if not event.button.id or not event.button.id.startswith("action-"):
            return

        pane = self.active_pane
        if pane:
            pane.run_turn(self.actions[int(event.button.id.removeprefix("action-"))]["prompt"])


def main():
    daemon.load_env(daemon.ENV_PATH)
    config = Config.load()
    skip_permissions = config.get_bool("daemon.skip_permissions", "SKIP_PERMISSIONS", default=True)
    TeleclaudeTui(Path.cwd(), daemon.resolve_claude_bin(config), skip_permissions).run()


if __name__ == "__main__":
    main()
