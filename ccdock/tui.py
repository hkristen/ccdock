"""ccdock: Claude Code Session Manager TUI (Textual)"""

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Label, OptionList
from textual.widgets.option_list import Option

from ccdock import __version__
from ccdock.core import (
    SessionInfo,
    _claude_dir,
    _has_tmux,
    _is_session_process,
    _relative_time,
    _strip_control,
    _truncate,
    attach_session,
    collect_projects,
    create_session,
    discover_sessions,
    kill_session,
    session_name_for,
    validate_resume,
)

_REMOTE_MSG = ("Runs on another machine sharing this home; if that machine "
               "crashed, delete its pid file in ~/.claude/sessions")

# How long the in-app Stop waits for the process to exit before giving up.
STOP_TIMEOUT = 5.0
# How long stopping a session outside tmux waits before resuming it in tmux.
RESUME_STOP_TIMEOUT = 10.0


def _claude_on_path() -> bool:
    return shutil.which("claude") is not None


@dataclass
class AppResult:
    action: str  # "attach", "create" (fresh claude) or "resume" (claude --resume)
    tmux_session: str | None = None
    tmux_pane: str | None = None  # for "attach": the pane claude runs in
    path: str | None = None
    name: str | None = None
    session: SessionInfo | None = None  # for "resume": id, pid and start time
    stop_first: bool = False  # for "resume": terminate the live process first


# ---------------------------------------------------------------------------
# Confirm stop modal
# ---------------------------------------------------------------------------

class ConfirmStopScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "confirm", "Yes"),
        Binding("n", "cancel", "No"),
        # Footer lists one key per action, so escape gets its own to stay visible.
        Binding("escape", "back", "Cancel"),
    ]

    DEFAULT_CSS = """
    ConfirmStopScreen {
        align: center middle;
    }
    #confirm-dialog {
        width: 60;
        height: auto;
        border: thick $warning;
        background: $surface;
        padding: 1 2;
    }
    #confirm-dialog Label {
        width: 100%;
        content-align: center middle;
    }
    """

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            # Project names come from the filesystem, so never parse them as markup.
            yield Label(self._message, markup=False)
            yield Label("[b]y[/b]es / [b]n[/b]o", markup=True)
        # A modal cuts the binding chain, so the base footer's q/n/s/r are
        # dead while it is open; show the keys that actually work instead.
        yield Footer()

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_back(self) -> None:
        self.dismiss(False)


# ---------------------------------------------------------------------------
# New session screen
# ---------------------------------------------------------------------------

class NewSessionScreen(ModalScreen[str | None]):
    BINDINGS = [
        Binding("escape", "cancel", "Back"),
    ]

    DEFAULT_CSS = """
    NewSessionScreen {
        align: center middle;
    }
    #new-session-container {
        width: 70;
        height: 80%;
        max-height: 30;
        border: thick $success;
        background: $surface;
        padding: 1 2;
    }
    #new-session-container Label {
        margin-bottom: 1;
    }
    #project-list {
        height: 1fr;
    }
    #path-input {
        display: none;
        dock: bottom;
        margin-top: 1;
    }
    #path-hint {
        display: none;
        dock: bottom;
        color: $warning;
        margin: 0;
    }
    """

    def __init__(self, projects: list[tuple[str, str]]) -> None:
        super().__init__()
        self._projects = projects

    def compose(self) -> ComposeResult:
        with Vertical(id="new-session-container"):
            yield Label("[b]New session[/b]: select a project", markup=True)
            options = []
            for path, name in self._projects:
                # Rich Text is rendered verbatim, so paths are never parsed as markup.
                options.append(Option(Text(f"{name:<20s}  {_strip_control(path)}"), id=path))
            options.append(Option("Other path...", id="__NEW__"))
            yield OptionList(*options, id="project-list")
            yield Label("", id="path-hint", markup=False)
            yield Input(placeholder="Enter project path...", id="path-input")
        yield Footer()

    @on(OptionList.OptionSelected)
    def on_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id == "__NEW__":
            self.query_one("#project-list").display = False
            hint = self.query_one("#path-hint", Label)
            hint.update("Enter a project directory, Esc to go back")
            hint.display = True
            inp = self.query_one("#path-input", Input)
            inp.display = True
            inp.focus()
        else:
            self.dismiss(str(event.option.id))

    @on(Input.Submitted)
    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if not value:
            # Blank input is not a request to leave: say so and keep the prompt.
            self.query_one("#path-hint", Label).update("No path entered")
            return
        self.dismiss(os.path.abspath(os.path.expanduser(value)))

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Session table
# ---------------------------------------------------------------------------

class SessionTable(DataTable):
    """The session list.

    Enter is bound here rather than handled through DataTable.RowSelected:
    the focused table consumes Enter, so an app-level binding never fires,
    and RowSelected is also posted by a mouse click on the cursor row, which
    made a second click on a row leave the app.
    """

    BINDINGS = [
        Binding("enter", "attach", "Attach"),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
    ]

    def action_attach(self) -> None:
        self.app.action_attach()


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------

class CcdockApp(App):
    CSS = """
    Screen {
        background: $surface;
    }
    Header {
        background: $success;
        color: $text;
    }
    DataTable {
        background: $surface;
        height: 1fr;
    }
    DataTable > .datatable--cursor {
        background: $warning;
        color: $text;
    }
    DataTable > .datatable--header {
        background: $surface;
        color: $text-muted;
        text-style: bold;
    }
    Footer {
        background: $surface;
    }
    #status-bar {
        /* Not docked: the Footer is docked to the bottom edge, and two widgets
           docked to the same edge overlap, which hid every status message. */
        height: 1;
        background: $surface;
        color: $warning;
        padding: 0 2;
    }
    """

    TITLE = "ccdock"
    SUB_TITLE = "claude code sessions"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("n", "new_session", "New"),
        Binding("s", "stop_session", "Stop"),
        Binding("r", "refresh", "Refresh"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._sessions: list[SessionInfo] = []
        self._tmux_available = _has_tmux()
        # Sweeps are stamped on the UI thread when scheduled; a cancelled
        # discovery thread still runs to completion and posts its result, so
        # _apply_sessions drops anything older than the newest applied stamp.
        self._sweep_scheduled = 0
        self._sweep_applied = 0

    def compose(self) -> ComposeResult:
        yield Header()
        yield SessionTable(id="sessions-table")
        yield Label("", id="status-bar", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("", "PROJECT", "BRANCH", "CONTEXT", "MODIFIED")
        self._load_sessions()
        self.set_interval(30, self._load_sessions)

    def _load_sessions(self, announce: bool = False, keep_status: bool = False) -> None:
        """Schedule a session sweep.

        announce: report the outcome in the status bar (manual refresh).
        keep_status: leave a message set by the action that triggered this
            sweep in place; a plain timer sweep clears the status bar.
        """
        self._sweep_scheduled += 1
        self._discover(self._sweep_scheduled, announce, keep_status)

    @work(thread=True, exclusive=True, group="discover")
    def _discover(self, stamp: int, announce: bool, keep_status: bool) -> None:
        """Discover sessions off the UI thread (spawns tmux, ps and git)."""
        sessions = discover_sessions()
        self.call_from_thread(self._apply_sessions, sessions, announce, stamp, keep_status)

    def _apply_sessions(self, sessions: list[SessionInfo], announce: bool,
                        stamp: int | None = None, keep_status: bool = False) -> None:
        if stamp is not None:
            if stamp < self._sweep_applied:
                return  # a newer sweep already landed; this one is stale
            self._sweep_applied = stamp
        table = self.query_one(DataTable)
        selected = self._selected_session()
        selected_id = selected.session_id if selected else None
        old_row = table.cursor_row
        self._sessions = sessions
        self._populate_table()
        gone = False
        if selected_id:
            for idx, s in enumerate(self._sessions):
                if s.session_id == selected_id:
                    table.move_cursor(row=idx)
                    break
            else:
                # The selected session vanished: stay near where the cursor was
                # instead of jumping back to the top of the list.
                gone = True
                if table.row_count:
                    table.move_cursor(row=min(old_row, table.row_count - 1))
        if announce:
            msg = f"Refreshed: {len(self._sessions)} session(s)"
            self._set_status(f"{msg}; selected session is gone" if gone else msg)
        elif gone:
            self._set_status("Selected session is gone")
        elif not keep_status:
            # Messages from actions live until the next refresh.
            self._set_status(self._idle_status())

    def _idle_status(self) -> str:
        """The status line when no action has anything to report."""
        if not self._sessions:
            where = _strip_control(str(_claude_dir() / "projects"))
            return f"No Claude Code sessions found in {where}. Press n to start one."
        if any(s.tmux_unknown for s in self._sessions):
            return "Could not query tmux; press r to retry"
        if not _claude_on_path():
            return "claude not found on PATH: browsing works, starting or resuming does not"
        return ""

    def _populate_table(self) -> None:
        table = self.query_one(DataTable)
        table.clear()
        # Row keys must be unique or DataTable raises DuplicateKey, which
        # would take the whole app down; keep the first occurrence of an id.
        seen: set[str] = set()
        unique: list[SessionInfo] = []
        for s in self._sessions:
            if s.session_id in seen:
                continue
            seen.add(s.session_id)
            unique.append(s)
        self._sessions = unique
        for s in self._sessions:
            if s.is_alive:
                icon = Text("● ", style="bold green")
            elif s.remote:
                icon = Text("◍ ", style="bold cyan")
            else:
                icon = Text("○ ", style="dim")
            name_style = "bold green" if s.is_alive else "bold cyan" if s.remote else "bold"
            name = Text(_truncate(s.project_name, 20), style=name_style)
            branch = Text(_truncate(s.git_branch, 20) if s.git_branch else "", style="yellow")
            context = Text(_truncate(s.summary or s.first_prompt or "", 60), style="dim")
            modified = Text(_relative_time(s.modified), style="dim")
            table.add_row(icon, name, branch, context, modified, key=s.session_id)

    def _set_status(self, msg: str) -> None:
        self.query_one("#status-bar", Label).update(msg)

    def action_refresh(self) -> None:
        self._load_sessions(announce=True)

    def _selected_session(self) -> SessionInfo | None:
        table = self.query_one(DataTable)
        if not self._sessions or table.row_count == 0:
            return None
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
            for s in self._sessions:
                if s.session_id == row_key.value:
                    return s
        except Exception:
            pass
        idx = table.cursor_row
        if 0 <= idx < len(self._sessions):
            return self._sessions[idx]
        return None

    def action_attach(self) -> None:
        s = self._selected_session()
        if not s:
            return
        if s.remote:
            self._set_status(_REMOTE_MSG)
            return
        if not self._tmux_available:
            self._set_status("tmux not available")
            return
        if s.tmux_session:
            self.exit(AppResult(action="attach", tmux_session=s.tmux_session,
                                tmux_pane=s.tmux_pane))
            return
        if not os.path.isdir(s.project_path):
            self._set_status(f"Project path not found: {_strip_control(s.project_path)}")
            return
        if s.is_alive and s.tmux_unknown:
            # It may well be in tmux; offering to stop and restart it would
            # be wrong then, so wait for a sweep that can tell.
            self._set_status("Could not query tmux, so ccdock cannot tell if this "
                             "session runs in tmux; press r to retry")
            return
        if not _claude_on_path() and not s.is_alive:
            self._set_status("claude not found on PATH, cannot start or resume")
            return
        name = session_name_for(s.project_path)
        if s.is_alive:
            # Running in some other terminal: the only way in is to stop it
            # and resume the transcript inside tmux.
            if not s.transcript_path:
                self._set_status("Session active but not in tmux, cannot attach")
                return
            label = f"{s.project_name} (PID {s.pid}) runs outside tmux. Stop it and resume here?"

            def on_confirm(confirmed: bool) -> None:
                if confirmed:
                    self.exit(AppResult(action="resume", path=s.project_path,
                                        name=name, session=s, stop_first=True))

            self.push_screen(ConfirmStopScreen(label), on_confirm)
        elif s.transcript_path:
            self.exit(AppResult(action="resume", path=s.project_path,
                                name=name, session=s))
        else:
            self.exit(AppResult(action="create", path=s.project_path, name=name))

    def action_new_session(self) -> None:
        if not self._tmux_available:
            self._set_status("tmux not available")
            return
        if not _claude_on_path():
            self._set_status("claude not found on PATH, cannot start a session")
            return
        projects = collect_projects(self._sessions)

        def on_result(path: str | None) -> None:
            if not path:
                return
            if os.path.isdir(path):
                self.exit(AppResult(action="create", path=path,
                                    name=session_name_for(path)))
            else:
                self._set_status(f"Not a directory: {_strip_control(path)}")

        self.push_screen(NewSessionScreen(projects), on_result)

    def action_stop_session(self) -> None:
        s = self._selected_session()
        if not s:
            return
        if s.remote:
            self._set_status(_REMOTE_MSG)
            return
        if not s.is_alive:
            self._set_status("Session is not running")
            return
        label = f"Stop {s.project_name} (PID {s.pid})?"

        def on_confirm(confirmed: bool) -> None:
            if not confirmed:
                return
            # The process may have gone away while the dialog was open.
            if s.pid is None or not _is_session_process(s.pid, s.proc_start):
                self._set_status("Session already exited")
                self._load_sessions(keep_status=True)
                return
            self._set_status(f"Stopping {s.project_name} (PID {s.pid})...")
            self._stop_session(s)

        self.push_screen(ConfirmStopScreen(label), on_confirm)

    @work(thread=True, group="stop")
    def _stop_session(self, s: SessionInfo) -> None:
        """Terminate and wait off the UI thread; kill_session blocks for up to STOP_TIMEOUT."""
        stopped = kill_session(s, timeout=STOP_TIMEOUT)
        self.call_from_thread(self._after_stop, stopped)

    def _after_stop(self, stopped: bool) -> None:
        if stopped:
            self._set_status("Session stopped")
        else:
            self._set_status(f"Session still running after {STOP_TIMEOUT:g}s, left alone")
        self._load_sessions(keep_status=True)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ccdock",
        description="Dashboard for your Claude Code sessions: see what is running, "
                    "and attach, resume, start or stop sessions in tmux.",
        epilog="keys: Enter attach/resume, n new session, s stop, r refresh, q quit\n"
               "environment: CCDOCK_CLAUDE_DIR (default ~/.claude), "
               "CCDOCK_PROJECT_DIRS (extra project directories, colon-separated)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"ccdock {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    # Parsed before the TUI starts, so 'ccdock --help' prints help instead of
    # taking over the terminal.
    _parse_args(argv)
    app = CcdockApp()
    result = app.run()
    if app.return_code:
        # Textual exits with a non-zero code after an unhandled exception
        # (for example in the discovery worker); propagate it.
        return app.return_code
    if not isinstance(result, AppResult):
        return 0
    try:
        if result.action == "attach" and result.tmux_session:
            attach_session(result.tmux_session, pane_id=result.tmux_pane)
        elif result.action == "create" and result.path and result.name:
            create_session(result.path, result.name)
        elif result.action == "resume" and result.path and result.name and result.session:
            s = result.session
            if result.stop_first:
                # Fail here, before the live session is touched, if the resume
                # could not work anyway (bad id, no claude, missing directory).
                validate_resume(result.path, s.session_id)
                print(f"ccdock: stopping PID {s.pid}, waiting for it to exit...",
                      file=sys.stderr)
                if not kill_session(s, timeout=RESUME_STOP_TIMEOUT):
                    print(f"ccdock: PID {s.pid} is still running, not resuming",
                          file=sys.stderr)
                    return 1
            create_session(result.path, result.name, resume_id=s.session_id)
    except RuntimeError as e:
        print(f"ccdock: {e}", file=sys.stderr)
        return 1
    except (OSError, subprocess.SubprocessError) as e:
        print(f"ccdock: tmux failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
