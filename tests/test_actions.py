"""Tests for the action layer: naming, validation, attach, tmux startup, stop.

Nothing here touches the user's tmux server or processes it did not spawn:
tmux calls are monkeypatched, or run against a private server (see
private_tmux), and every signalled process is a child started by the test.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time

import pytest

from ccdock import core, tui
from ccdock.core import SessionInfo

UUID = "12345678-abcd-4ef0-9abc-0123456789ab"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("CCDOCK_CLAUDE_DIR", str(tmp_path / "claude"))


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path,expected", [
    ("/home/u/my.project", "my-project"),
    ("/home/u/a:b", "a-b"),
    ("/home/u/back\\slash", "back-slash"),
    ("/home/u/ctl\x01\x1bchars", "ctl-chars"),
    ("/home/u/projekt-über", "projekt-über"),
    ("/home/u/日本語", "日本語"),
    ("/home/u/trailing/", "trailing"),
    ("/home/u/..dots..", "dots"),
    ("/home/u/" + "x" * 30, "x" * 20),
    ("/home/u/" + "a" * 19 + ".b", "a" * 19),  # cut at the '-', then stripped
    ("", "claude"),
    ("/", "claude"),
    ("/home/u/...", "claude"),
])
def test_session_name_for(path, expected):
    assert core.session_name_for(path) == expected


def test_tmux_target_is_exact_match():
    assert core._tmux_target("api") == "=api"


def test_tmux_target_passes_session_ids_through():
    assert core._tmux_target("$3") == "$3"


def test_unique_session_name(monkeypatch):
    taken = {"proj", "proj-2"}
    monkeypatch.setattr(core, "_tmux_session_exists", lambda n: n in taken)
    assert core._unique_session_name("free") == "free"
    assert core._unique_session_name("proj") == "proj-3"
    assert core._unique_session_name("free", frozenset({"free"})) == "free-2"
    assert core._unique_session_name("proj", frozenset({"proj-3"})) == "proj-4"


def test_unique_session_name_gives_up(monkeypatch):
    monkeypatch.setattr(core, "_tmux_session_exists", lambda n: True)
    with pytest.raises(RuntimeError, match="too many"):
        core._unique_session_name("proj")


# ---------------------------------------------------------------------------
# validate_resume / _claude_command
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "claude"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    return exe


def test_validate_resume_ok(fake_claude, tmp_path):
    core.validate_resume(str(tmp_path), UUID)
    core.validate_resume(str(tmp_path), None)


@pytest.mark.parametrize("bad", [
    "not-a-uuid", "-" + UUID[1:], "--dangerously-skip-permissions",
    UUID + "\n", UUID + "0", "",
])
def test_validate_resume_rejects_non_uuid(fake_claude, tmp_path, bad):
    with pytest.raises(RuntimeError, match="not a claude session id"):
        core.validate_resume(str(tmp_path), bad)


def test_validate_resume_without_claude(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(RuntimeError, match="claude not found"):
        core.validate_resume(str(tmp_path), None)


def test_validate_resume_missing_dir(fake_claude, tmp_path):
    with pytest.raises(RuntimeError, match="does not exist"):
        core.validate_resume(str(tmp_path / "gone"), UUID)


def test_claude_command():
    assert core._claude_command(None) == ["claude"]
    assert core._claude_command(UUID) == ["claude", "--resume", UUID]


# ---------------------------------------------------------------------------
# attach_session
# ---------------------------------------------------------------------------

@pytest.fixture
def execvp(monkeypatch):
    calls = []
    monkeypatch.setattr(core.os, "execvp", lambda f, args: calls.append(args))
    return calls


@pytest.fixture
def tmux_quiet(monkeypatch):
    calls = []
    monkeypatch.setattr(core, "_tmux_quiet", lambda *a, **kw: calls.append(a))
    return calls


def test_attach_missing_session(monkeypatch, execvp, tmux_quiet):
    monkeypatch.setattr(core, "_tmux_session_exists", lambda n: False)
    with pytest.raises(RuntimeError, match="no longer exists"):
        core.attach_session("gone", pane_id="%1")
    assert execvp == [] and tmux_quiet == []


def test_attach_outside_tmux(monkeypatch, execvp, tmux_quiet):
    monkeypatch.setattr(core, "_tmux_session_exists", lambda n: True)
    monkeypatch.delenv("TMUX", raising=False)
    core.attach_session("proj", pane_id="%3")
    assert tmux_quiet == [("select-window", "-t", "%3"), ("select-pane", "-t", "%3")]
    assert execvp == [["tmux", "attach", "-t", "=proj"]]


def test_attach_inside_tmux(monkeypatch, execvp, tmux_quiet):
    monkeypatch.setattr(core, "_tmux_session_exists", lambda n: True)
    monkeypatch.setenv("TMUX", "/tmp/fake,1,0")
    core.attach_session("proj")
    assert tmux_quiet == []
    assert execvp == [["tmux", "switch-client", "-t", "=proj"]]


# ---------------------------------------------------------------------------
# _start_tmux_session against a private tmux server
# ---------------------------------------------------------------------------

@pytest.fixture
def private_tmux(tmp_path, monkeypatch):
    """Point every tmux call at a throwaway server; kill it at teardown."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux not installed")
    # Socket paths are limited to ~108 bytes, so keep the dir short.
    sockdir = tempfile.mkdtemp(prefix="ccd-")
    monkeypatch.setenv("TMUX_TMPDIR", sockdir)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    # Keep the user's tmux.conf out of the private server.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    env = dict(os.environ)
    yield lambda *args: subprocess.run(["tmux", *args], capture_output=True,
                                       text=True, timeout=5, env=env)
    subprocess.run(["tmux", "kill-server"], capture_output=True, timeout=5, env=env)
    shutil.rmtree(sockdir, ignore_errors=True)


def session_name(tmux, session_id: str) -> str:
    return tmux("display-message", "-p", "-t", session_id, "#{session_name}").stdout.strip()


def test_start_creates_session_in_project_dir(private_tmux, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    sid = core._start_tmux_session("proj", str(proj), ["sleep", "60"])
    assert sid.startswith("$")
    assert session_name(private_tmux, sid) == "proj"
    r = private_tmux("display-message", "-p", "-t", sid, "#{pane_current_path}")
    assert os.path.realpath(r.stdout.strip()) == os.path.realpath(proj)


def test_start_suffixes_existing_name(private_tmux, tmp_path):
    private_tmux("new-session", "-d", "-s", "proj", "sleep", "60")
    sid = core._start_tmux_session("proj", str(tmp_path), ["sleep", "60"])
    assert session_name(private_tmux, sid) == "proj-2"


def test_start_retries_on_duplicate(private_tmux, tmp_path, monkeypatch):
    # Simulate losing the race: the probe says the name is free, tmux disagrees.
    private_tmux("new-session", "-d", "-s", "proj", "sleep", "60")
    monkeypatch.setattr(core, "_unique_session_name",
                        lambda n, exclude: n if n not in exclude else f"{n}-2")
    sid = core._start_tmux_session("proj", str(tmp_path), ["sleep", "60"])
    assert session_name(private_tmux, sid) == "proj-2"


def test_start_missing_dir(private_tmux, tmp_path):
    with pytest.raises(RuntimeError, match="does not exist"):
        core._start_tmux_session("proj", str(tmp_path / "gone"), ["sleep", "60"])
    assert private_tmux("has-session", "-t", "=proj").returncode != 0


def test_start_command_exits_immediately(private_tmux, tmp_path):
    with pytest.raises(RuntimeError, match="exited immediately"):
        core._start_tmux_session("proj", str(tmp_path), ["true"])


# ---------------------------------------------------------------------------
# kill_session (only ever signals children spawned here)
# ---------------------------------------------------------------------------

def _spawn(ignore_term: bool = False) -> subprocess.Popen:
    code = "import signal, sys, time\n"
    if ignore_term:
        code += "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    code += "print('ready', flush=True)\ntime.sleep(60)\n"
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    p.stdout.readline()  # handler installed before anyone signals it
    return p


@pytest.fixture
def children():
    procs = []
    yield lambda **kw: procs.append(_spawn(**kw)) or procs[-1]
    for p in procs:
        if p.poll() is None:
            p.kill()
        p.wait(timeout=5)
        p.stdout.close()


# The start-time check that keeps these tests from signalling a reused PID
# reads /proc.
needs_proc = pytest.mark.skipif(not os.path.exists("/proc/self/stat"), reason="needs /proc")


def _session(p: subprocess.Popen, **kw) -> SessionInfo:
    return SessionInfo(session_id=UUID, project_path="/x", project_name="x",
                       pid=p.pid, is_alive=True,
                       proc_start=core._proc_stat(p.pid)[1], **kw)


@pytest.fixture
def no_tmux(monkeypatch):
    def refuse(*a, **kw):
        raise AssertionError(f"unexpected tmux call {a}")
    monkeypatch.setattr(core, "_tmux_quiet", refuse)
    monkeypatch.setattr(core, "_pane_pid", refuse)


@needs_proc
def test_kill_cooperative_child(children, no_tmux):
    p = children()
    assert core.kill_session(_session(p), timeout=5) is True
    assert p.wait(timeout=5) == -15


@needs_proc
def test_kill_leaves_stubborn_child_alone(children, no_tmux):
    p = children(ignore_term=True)
    start = time.monotonic()
    assert core.kill_session(_session(p), timeout=0.5) is False
    assert time.monotonic() - start < 3
    time.sleep(0.2)
    assert p.poll() is None  # no SIGKILL followed


@needs_proc
def test_kill_skips_reused_pid(children, no_tmux):
    p = children()
    s = _session(p)
    s.proc_start = str(int(s.proc_start) + 1)  # recorded process was a different one
    core.kill_session(s, timeout=0.5)
    time.sleep(0.2)
    assert p.poll() is None


@needs_proc
def test_kill_not_alive_session_sends_nothing(children, no_tmux):
    p = children()
    s = _session(p)
    s.is_alive = False
    core._terminate(s)
    time.sleep(0.2)
    assert p.poll() is None


@needs_proc
def test_kill_closes_own_pane(children, monkeypatch):
    p = children()
    killed = []
    monkeypatch.setattr(core, "_pane_pid", lambda pane: p.pid)
    monkeypatch.setattr(core, "_tmux_quiet", lambda *a, **kw: killed.append(a))
    assert core.kill_session(_session(p, tmux_pane="%4"), timeout=5) is True
    assert killed == [("kill-pane", "-t", "%4")]


@needs_proc
def test_kill_keeps_pane_when_child_survives(children, monkeypatch):
    p = children(ignore_term=True)
    killed = []
    monkeypatch.setattr(core, "_pane_pid", lambda pane: p.pid)
    monkeypatch.setattr(core, "_tmux_quiet", lambda *a, **kw: killed.append(a))
    assert core.kill_session(_session(p, tmux_pane="%4"), timeout=0.5) is False
    assert killed == []


# ---------------------------------------------------------------------------
# _close_pane_if_safe
# ---------------------------------------------------------------------------

def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


@pytest.mark.parametrize("pane_pid,closes", [
    ("session", True),       # the pane still runs the (exited) session process
    ("dead", True),          # remain-on-exit pane whose process is gone
    ("other", False),        # pane id reused by an unrelated live process
    (None, False),           # no such pane
])
def test_close_pane_if_safe(monkeypatch, pane_pid, closes):
    session_pid = _dead_pid()
    resolved = {"session": session_pid, "dead": _dead_pid(),
                "other": os.getpid(), None: None}[pane_pid]
    killed = []
    monkeypatch.setattr(core, "_pane_pid", lambda pane: resolved)
    monkeypatch.setattr(core, "_tmux_quiet", lambda *a, **kw: killed.append(a))
    s = SessionInfo(session_id=UUID, project_path="/x", project_name="x",
                    pid=session_pid, tmux_pane="%2")
    core._close_pane_if_safe(s)
    assert killed == ([("kill-pane", "-t", "%2")] if closes else [])


def test_close_pane_without_pane(monkeypatch):
    monkeypatch.setattr(core, "_pane_pid", lambda pane: pytest.fail("probed"))
    core._close_pane_if_safe(SessionInfo(session_id=UUID, project_path="/x",
                                         project_name="x", pid=1))


# ---------------------------------------------------------------------------
# tui.main result dispatch
# ---------------------------------------------------------------------------

@pytest.fixture
def dispatch(monkeypatch):
    """Run tui.main with a given AppResult; record the actions it takes."""
    calls = []
    monkeypatch.setattr(tui, "_has_tmux", lambda: True)
    monkeypatch.setattr(tui, "attach_session", lambda *a, **kw: calls.append(("attach", a, kw)))
    monkeypatch.setattr(tui, "create_session", lambda *a, **kw: calls.append(("create", a, kw)))
    monkeypatch.setattr(tui, "kill_session", lambda *a, **kw: calls.append(("kill", a, kw)) or True)
    monkeypatch.setattr(tui, "validate_resume", lambda *a: calls.append(("validate", a)))

    def run(result):
        monkeypatch.setattr(tui.CcdockApp, "run", lambda self: result)
        return tui.main([]), calls
    return run


def _resume_result(**kw):
    s = SessionInfo(session_id=UUID, project_path="/p", project_name="p", pid=42)
    return tui.AppResult(action="resume", path="/p", name="p", session=s, **kw)


def test_main_no_result(dispatch):
    assert dispatch(None) == (0, [])


def test_main_attach(dispatch):
    rc, calls = dispatch(tui.AppResult(action="attach", tmux_session="t", tmux_pane="%1"))
    assert rc == 0 and calls == [("attach", ("t",), {"pane_id": "%1"})]


def test_main_resume_stop_first_order(dispatch):
    rc, calls = dispatch(_resume_result(stop_first=True))
    assert rc == 0
    assert [c[0] for c in calls] == ["validate", "kill", "create"]
    assert calls[2] == ("create", ("/p", "p"), {"resume_id": UUID})


def test_main_resume_validate_fails_before_stop(dispatch, monkeypatch, capsys):
    def fail(*a):
        raise RuntimeError("claude not found on PATH")
    monkeypatch.setattr(tui, "validate_resume", fail)
    rc, calls = dispatch(_resume_result(stop_first=True))
    assert rc == 1 and calls == []
    assert "ccdock: claude not found on PATH" in capsys.readouterr().err


def test_main_resume_still_running(dispatch, monkeypatch, capsys):
    monkeypatch.setattr(tui, "kill_session", lambda *a, **kw: False)
    rc, calls = dispatch(_resume_result(stop_first=True))
    assert rc == 1 and [c[0] for c in calls] == ["validate"]
    assert "still running, not resuming" in capsys.readouterr().err


def test_main_runtime_error(dispatch, monkeypatch, capsys):
    def fail(*a, **kw):
        raise RuntimeError("tmux session 't' no longer exists")
    monkeypatch.setattr(tui, "attach_session", fail)
    rc, _ = dispatch(tui.AppResult(action="attach", tmux_session="t"))
    assert rc == 1
    assert capsys.readouterr().err == "ccdock: tmux session 't' no longer exists\n"


def test_help_and_version_do_not_start_the_tui(monkeypatch, capsys):
    monkeypatch.setattr(tui.CcdockApp, "run", lambda self: pytest.fail("TUI started"))
    for flag in ("--help", "--version"):
        with pytest.raises(SystemExit) as exc:
            tui.main([flag])
        assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "usage: ccdock" in out and "ccdock 0.1.0" in out


def test_unknown_argument_is_an_error(monkeypatch):
    monkeypatch.setattr(tui.CcdockApp, "run", lambda self: pytest.fail("TUI started"))
    with pytest.raises(SystemExit) as exc:
        tui.main(["--bogus"])
    assert exc.value.code == 2
