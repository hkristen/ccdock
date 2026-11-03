"""Textual pilot tests for CcdockApp with discovery stubbed out."""

import asyncio

import pytest
from textual.widgets import DataTable, Label

from ccdock import tui
from ccdock.core import SessionInfo

SESSIONS = [
    SessionInfo(session_id="11111111-1111-4111-8111-111111111111", project_path="/a",
                project_name="alpha", summary="first", git_branch="main"),
    SessionInfo(session_id="22222222-2222-4222-8222-222222222222", project_path="/b",
                project_name="beta", first_prompt="second"),
]


@pytest.fixture(autouse=True)
def _stub(tmp_path, monkeypatch):
    monkeypatch.setenv("CCDOCK_CLAUDE_DIR", str(tmp_path))
    monkeypatch.setattr(tui, "_has_tmux", lambda: True)
    monkeypatch.setattr(tui, "_claude_on_path", lambda: True)
    monkeypatch.setattr(tui, "discover_sessions", lambda: list(SESSIONS))
    monkeypatch.setattr(tui, "kill_session", lambda *a, **kw: pytest.fail("kill_session called"))


def _run(check):
    async def go():
        app = tui.CcdockApp()
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            await check(app, pilot)
    asyncio.run(go())


def test_rows_render():
    async def check(app, pilot):
        table = app.query_one(DataTable)
        assert table.row_count == 2
        assert str(table.get_row_at(0)[1]) == "alpha"
        assert str(table.get_row_at(1)[3]) == "second"
    _run(check)


def test_stop_not_running():
    async def check(app, pilot):
        await pilot.press("s")
        assert str(app.query_one("#status-bar", Label).content) == "Session is not running"
    _run(check)


def _run_with(monkeypatch, sessions, check):
    monkeypatch.setattr(tui, "discover_sessions", lambda: list(sessions))
    _run(check)


def _status(app) -> str:
    return str(app.query_one("#status-bar", Label).content)


def test_empty_state_message(monkeypatch):
    async def check(app, pilot):
        assert _status(app).startswith("No Claude Code sessions found in ")
    _run_with(monkeypatch, [], check)


def test_remote_session_is_left_alone(monkeypatch, tmp_path):
    s = SessionInfo(session_id="33333333-3333-4333-8333-333333333333",
                    project_path=str(tmp_path), project_name="gamma", pid=4242,
                    remote=True, transcript_path=tmp_path / "t.jsonl")

    async def check(app, pilot):
        await pilot.press("enter")
        assert app.is_running and _status(app) == tui._REMOTE_MSG
        await pilot.press("s")
        assert app.is_running and _status(app) == tui._REMOTE_MSG
    _run_with(monkeypatch, [s], check)


def test_tmux_unknown_does_not_offer_stop(monkeypatch, tmp_path):
    s = SessionInfo(session_id="33333333-3333-4333-8333-333333333333",
                    project_path=str(tmp_path), project_name="gamma", pid=4242,
                    is_alive=True, tmux_unknown=True, transcript_path=tmp_path / "t.jsonl")

    async def check(app, pilot):
        assert _status(app) == "Could not query tmux; press r to retry"
        await pilot.press("enter")
        assert app.is_running and len(app.screen_stack) == 1
        assert "Could not query tmux" in _status(app)
    _run_with(monkeypatch, [s], check)


def test_claude_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(tui, "_claude_on_path", lambda: False)
    s = SessionInfo(session_id="33333333-3333-4333-8333-333333333333",
                    project_path=str(tmp_path), project_name="gamma",
                    transcript_path=tmp_path / "t.jsonl")

    async def check(app, pilot):
        assert _status(app).startswith("claude not found on PATH")
        await pilot.press("enter")
        assert app.is_running
        assert _status(app) == "claude not found on PATH, cannot start or resume"
        await pilot.press("n")
        assert len(app.screen_stack) == 1
        assert _status(app) == "claude not found on PATH, cannot start a session"
    _run_with(monkeypatch, [s], check)
