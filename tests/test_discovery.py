"""Tests for the data/discovery layer in ccdock.core.

Every test runs against a synthetic Claude data directory (CCDOCK_CLAUDE_DIR)
and a fake HOME; tmux is never queried and subprocess.run is replaced so no
real git or tmux runs.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ccdock import core
from ccdock.core import SessionInfo, TmuxInfo

HAS_PROC = os.path.exists("/proc/self/stat")
needs_proc = pytest.mark.skipif(not HAS_PROC, reason="needs /proc")

# Above the largest pid_max Linux allows (2**22), so never a live process.
DEAD_PID = 4_999_999

SID_A = "11111111-1111-1111-1111-111111111111"
SID_B = "22222222-2222-2222-2222-222222222222"
SID_C = "33333333-3333-3333-3333-333333333333"


_REAL_RUN = subprocess.run


def encode(path) -> str:
    """Claude's lossy encoding of a project path into a directory name."""
    return str(path).replace("/", "-").replace(".", "-")


def write_jsonl(path: Path, records, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write((r if isinstance(r, str) else json.dumps(r)) + "\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def write_index(proj_dir: Path, entries) -> None:
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / "sessions-index.json").write_text(json.dumps({"version": 1, "entries": entries}))


def user_msg(text, **extra) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}, **extra}


def own_start_ticks() -> str:
    return core._proc_stat(os.getpid())[1]


def by_id(sessions) -> dict[str, SessionInfo]:
    return {s.session_id: s for s in sessions}


def _no_subprocess(argv, *args, **kwargs):
    """Stand-in for subprocess.run: git reports "not a repository", anything else fails the test."""
    if argv[:1] == ["git"]:
        return subprocess.CompletedProcess(argv, 128, stdout="", stderr="fatal: not a git repository")
    raise AssertionError(f"unexpected subprocess.run: {argv!r}")


@pytest.fixture
def short_root():
    """A directory with a short absolute path.

    _decode_project_dir spends up to 63 probes per path component out of a
    bounded budget, so decode tests need a shallow base rather than pytest's
    deep tmp_path.
    """
    base = "/tmp" if os.path.isdir("/tmp") else None
    d = tempfile.mkdtemp(prefix="ccd", dir=base)
    try:
        yield Path(os.path.realpath(d))
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def claude(tmp_path, monkeypatch):
    d = tmp_path / "claude"
    (d / "projects").mkdir(parents=True)
    (d / "sessions").mkdir()
    monkeypatch.setenv("CCDOCK_CLAUDE_DIR", str(d))
    monkeypatch.setattr(core, "find_cc_tmux_sessions", lambda: {})
    monkeypatch.setattr(subprocess, "run", _no_subprocess)
    return d


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home" / "u"
    h.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    monkeypatch.delenv("CCDOCK_PROJECT_DIRS", raising=False)
    return Path(os.path.realpath(h))


# ---------------------------------------------------------------------------
# _decode_project_dir
# ---------------------------------------------------------------------------

def test_decode_dotted_home_and_hyphenated_project(short_root):
    proj = short_root / "x" / "first.last" / "my-project"
    proj.mkdir(parents=True)
    assert core._decode_project_dir(encode(proj)) == str(proj)


def test_decode_prefers_existing_dotted_name_over_split(short_root):
    (short_root / "a.b").mkdir()
    assert core._decode_project_dir(encode(short_root / "a.b")) == str(short_root / "a.b")


def test_decode_nonexistent_falls_back_absolute(short_root):
    (short_root / "real").mkdir()
    out = core._decode_project_dir(encode(short_root / "real" / "gone-away" / "deeper"))
    assert out.startswith(str(short_root / "real") + "/")
    assert os.path.isabs(out)
    # The unresolved remainder is kept as one segment.
    assert out == str(short_root / "real" / "gone-away-deeper")


def test_decode_without_leading_dash_is_absolute():
    assert core._decode_project_dir("nonexistent-thing-zz") == "/nonexistent-thing-zz"


@pytest.mark.parametrize("name", ["-", "", "--", "---"])
def test_decode_degenerate_input(name):
    out = core._decode_project_dir(name)
    assert os.path.isabs(out)
    assert out.startswith("/")


def test_decode_degenerate_dash_is_root():
    assert core._decode_project_dir("-") == "/"
    assert core._decode_project_dir("") == "/"


def test_decode_long_name_bounded():
    name = "-" + "-".join(["seg"] * 5000)
    t0 = time.monotonic()
    out = core._decode_project_dir(name)
    assert time.monotonic() - t0 < 2
    assert os.path.isabs(out)


def test_decode_budget_exhaustion_stays_absolute(short_root, monkeypatch):
    deep = short_root.joinpath(*[f"d{i}" for i in range(80)])
    deep.mkdir(parents=True)
    calls = 0
    real_exists = os.path.exists

    def counting_exists(p):
        nonlocal calls
        calls += 1
        return real_exists(p)

    monkeypatch.setattr(core.os.path, "exists", counting_exists)
    out = core._decode_project_dir(encode(deep))
    assert calls <= 4096
    assert os.path.isabs(out)
    # Whatever was resolved plus the remainder still spells the same encoding.
    assert encode(out) == encode(deep)


def test_decode_deep_existing_path(short_root):
    # short_root is /tmp/<name>, so this is a 16-component path.
    deep = short_root.joinpath(*[f"d{i}" for i in range(14)])
    deep.mkdir(parents=True)
    assert core._decode_project_dir(encode(deep)) == str(deep)


# ---------------------------------------------------------------------------
# discover_sessions
# ---------------------------------------------------------------------------

def test_discover_no_projects_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CCDOCK_CLAUDE_DIR", str(tmp_path / "missing"))
    assert core.discover_sessions() == []


def test_discover_from_index(claude, tmp_path):
    proj = tmp_path / "work" / "api"
    proj.mkdir(parents=True)
    pdir = claude / "projects" / encode(proj)
    write_index(pdir, [{
        "sessionId": SID_A, "projectPath": str(proj), "gitBranch": "feat/x",
        "firstPrompt": "p" * 200, "summary": "Sum", "created": "2026-01-01T00:00:00Z",
        "modified": "2026-01-02T00:00:00Z",
    }])
    write_jsonl(pdir / f"{SID_A}.jsonl", [user_msg("hi")])
    [s] = core.discover_sessions()
    assert s.session_id == SID_A
    assert s.project_path == os.path.realpath(proj)
    assert s.project_name == "api"
    assert s.git_branch == "feat/x"
    assert s.summary == "Sum"
    assert s.first_prompt == "p" * 117 + "..."
    assert s.modified == "2026-01-02T00:00:00Z"
    assert s.transcript_path == pdir / f"{SID_A}.jsonl"
    assert s.pid is None and not s.is_alive


def test_discover_index_entry_without_transcript(claude, tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    write_index(claude / "projects" / encode(proj), [{"sessionId": SID_A, "projectPath": str(proj)}])
    [s] = core.discover_sessions()
    assert s.transcript_path is None


def test_discover_index_field_types_coerced(claude, tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    write_index(claude / "projects" / encode(proj), [{
        "sessionId": SID_A, "projectPath": str(proj), "summary": 5,
        "gitBranch": ["x"], "firstPrompt": "", "modified": {"a": 1},
    }])
    [s] = core.discover_sessions()
    assert s.summary is None and s.git_branch is None
    assert s.first_prompt is None and s.modified is None


@pytest.mark.parametrize("bad_path", ["relative/dir", "{missing}", "{file}", "/ok\0evil"])
def test_discover_index_project_path_not_trusted(claude, tmp_path, bad_path):
    proj = tmp_path / "realproj"
    proj.mkdir()
    (tmp_path / "afile").write_text("x")
    bad_path = bad_path.format(missing=tmp_path / "nope", file=tmp_path / "afile")
    pdir = claude / "projects" / encode(proj)
    write_index(pdir, [{"sessionId": SID_A, "projectPath": bad_path}])
    write_jsonl(pdir / f"{SID_A}.jsonl", [{"type": "x", "cwd": str(proj)}])
    [s] = core.discover_sessions()
    assert s.project_path == os.path.realpath(proj)


def test_discover_index_falls_back_to_decoded_dir(claude, short_root):
    proj = short_root / "first.last" / "my-proj"
    proj.mkdir(parents=True)
    write_index(claude / "projects" / encode(proj), [{"sessionId": SID_A, "projectPath": "rel"}])
    [s] = core.discover_sessions()
    assert s.project_path == str(proj)
    assert s.project_name == "my-proj"


def test_discover_transcript_only(claude, tmp_path):
    proj = tmp_path / "repo"
    proj.mkdir()
    pdir = claude / "projects" / encode(proj)
    write_jsonl(pdir / f"{SID_A}.jsonl", [
        {"type": "summary", "summary": "x"},
        {"type": "user", "cwd": str(proj), "gitBranch": "feature/recorded",
         "message": {"content": [{"type": "text", "text": "fix the bug"}]}},
    ], mtime=1_700_000_000)
    [s] = core.discover_sessions()
    assert s.project_path == os.path.realpath(proj)
    assert s.project_name == "repo"
    assert s.git_branch == "feature/recorded"
    assert s.first_prompt == "fix the bug"
    assert s.modified == datetime.fromtimestamp(1_700_000_000, timezone.utc).isoformat()
    assert s.transcript_path == pdir / f"{SID_A}.jsonl"


def test_discover_transcript_branch_beats_live_git(claude, tmp_path, monkeypatch):
    """The recorded branch is used and git is not even asked."""
    proj = tmp_path / "repo"
    proj.mkdir()
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="live-branch\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    write_jsonl(claude / "projects" / encode(proj) / f"{SID_A}.jsonl",
                [{"type": "user", "cwd": str(proj), "gitBranch": "old-branch"}])
    [s] = core.discover_sessions()
    assert s.git_branch == "old-branch"
    assert calls == []


def test_discover_transcript_without_branch_asks_git(claude, tmp_path, monkeypatch):
    proj = tmp_path / "repo"
    proj.mkdir()
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="live-branch\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    write_jsonl(claude / "projects" / encode(proj) / f"{SID_A}.jsonl",
                [{"type": "user", "cwd": str(proj), "gitBranch": ""}])
    [s] = core.discover_sessions()
    assert s.git_branch == "live-branch"
    assert calls == [["git", "-C", os.path.realpath(proj), "rev-parse", "--abbrev-ref", "HEAD"]]


def test_discover_transcript_branch_real_git(claude, tmp_path, monkeypatch):
    """Against a real throwaway repo whose current branch differs from the recorded one."""
    monkeypatch.setattr(subprocess, "run", _REAL_RUN)
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    for args in (["init", "-q", "-b", "trunk"], ["commit", "-q", "--allow-empty", "-m", "x"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, env=env, capture_output=True)
    pdir = claude / "projects" / encode(repo)
    write_jsonl(pdir / f"{SID_A}.jsonl", [{"type": "user", "cwd": str(repo), "gitBranch": "recorded"}])
    write_jsonl(pdir / f"{SID_B}.jsonl", [{"type": "user", "cwd": str(repo)}])
    got = by_id(core.discover_sessions())
    assert got[SID_A].git_branch == "recorded"
    assert got[SID_B].git_branch == "trunk"


def test_discover_transcript_cwd_untrusted_uses_decoded(claude, short_root):
    proj = short_root / "dotted.name" / "proj-x"
    proj.mkdir(parents=True)
    pdir = claude / "projects" / encode(proj)
    write_jsonl(pdir / f"{SID_A}.jsonl", [{"type": "user", "cwd": "relative/path"}])
    write_jsonl(pdir / f"{SID_B}.jsonl", [{"type": "user", "cwd": str(short_root / "missing")}])
    for s in core.discover_sessions():
        assert s.project_path == str(proj)


def test_discover_decoded_nonexistent_has_no_git_call(claude):
    write_jsonl(claude / "projects" / "-no-such-root-zz-proj" / f"{SID_A}.jsonl", [user_msg("x")])
    [s] = core.discover_sessions()
    assert s.project_path == "/no-such-root-zz-proj"
    assert s.git_branch is None


def test_first_prompt_skips_system_text(claude, tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    write_jsonl(claude / "projects" / encode(proj) / f"{SID_A}.jsonl", [
        {"type": "assistant", "message": {"content": "assistant talk"}},
        user_msg("<command-name>/clear</command-name>"),
        user_msg("[Request interrupted by user]"),
        user_msg([{"type": "tool_result", "content": "out"}, {"type": "text", "text": "   "}]),
        {"type": "user", "message": "not a dict"},
        {"type": "user", "message": {"content": 42}},
        user_msg([{"type": "text", "text": "<system-reminder>x"}, "  the real prompt " + "y" * 200]),
    ])
    [s] = core.discover_sessions()
    assert s.first_prompt == ("the real prompt " + "y" * 200)[:120]


def test_first_user_text_none_when_only_system():
    assert core._first_user_text([user_msg("<x>"), user_msg("[y]")]) is None


def test_discover_sorted_by_modified(claude, tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    pdir = claude / "projects" / encode(proj)
    write_index(pdir, [
        {"sessionId": SID_A, "projectPath": str(proj), "modified": "2026-01-01T00:00:00Z"},
        {"sessionId": SID_B, "projectPath": str(proj), "modified": "2026-03-01T00:00:00"},
        {"sessionId": SID_C, "projectPath": str(proj), "modified": "garbage"},
    ])
    write_jsonl(pdir / "44444444-4444-4444-4444-444444444444.jsonl",
                [{"cwd": str(proj)}], mtime=datetime(2026, 2, 1, tzinfo=timezone.utc).timestamp())
    ids = [s.session_id for s in core.discover_sessions()]
    assert ids == [SID_B, "44444444-4444-4444-4444-444444444444", SID_A, SID_C]


def test_discover_dedups_session_ids(claude, tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    pdir = claude / "projects" / encode(proj)
    write_index(pdir, [
        {"sessionId": SID_A, "projectPath": str(proj), "summary": "first"},
        {"sessionId": SID_A, "projectPath": str(proj), "summary": "dup"},
    ])
    write_jsonl(pdir / f"{SID_A}.jsonl", [{"cwd": str(proj)}])
    # Same id again under another project directory.
    write_jsonl(claude / "projects" / "-other" / f"{SID_A}.jsonl", [{"cwd": str(proj)}])
    sessions = core.discover_sessions()
    assert [s.session_id for s in sessions] == [SID_A]
    assert sessions[0].summary == "first"


@pytest.mark.parametrize("sid", ["../x", "a/b", "..", ".", "x" * 201, "a\0b", "", 7, None])
def test_discover_rejects_unsafe_index_ids(claude, tmp_path, sid):
    proj = tmp_path / "p"
    proj.mkdir()
    write_index(claude / "projects" / encode(proj),
                [{"sessionId": sid, "projectPath": str(proj)},
                 {"sessionId": SID_A, "projectPath": str(proj)}])
    assert [s.session_id for s in core.discover_sessions()] == [SID_A]


def test_safe_session_id_accepts_max_length():
    assert core._safe_session_id("x" * 200)
    assert not core._safe_session_id("x" * 201)


@pytest.mark.parametrize("index_text", [
    "not json", "[1, 2, 3]", '{"entries": {"a": 1}}', '{"entries": [1, "x", null, []]}',
    "[" * 100_000, "\xff\xfe garbage", "",
])
def test_discover_tolerates_malformed_index(claude, tmp_path, index_text):
    proj = tmp_path / "p"
    proj.mkdir()
    pdir = claude / "projects" / encode(proj)
    pdir.mkdir()
    (pdir / "sessions-index.json").write_text(index_text)
    write_jsonl(pdir / f"{SID_A}.jsonl", [{"cwd": str(proj)}])
    assert [s.session_id for s in core.discover_sessions()] == [SID_A]


def test_discover_ignores_oversized_index(claude, tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_JSON_MAX_BYTES", 64)
    proj = tmp_path / "p"
    proj.mkdir()
    pdir = claude / "projects" / encode(proj)
    write_index(pdir, [{"sessionId": SID_B, "projectPath": str(proj), "summary": "s" * 100}])
    write_jsonl(pdir / f"{SID_A}.jsonl", [{"cwd": str(proj)}])
    assert [s.session_id for s in core.discover_sessions()] == [SID_A]


def test_discover_index_is_directory(claude, tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    pdir = claude / "projects" / encode(proj)
    (pdir / "sessions-index.json").mkdir(parents=True)
    write_jsonl(pdir / f"{SID_A}.jsonl", [{"cwd": str(proj)}])
    assert [s.session_id for s in core.discover_sessions()] == [SID_A]


def test_discover_tolerates_malformed_transcript(claude, short_root):
    proj = short_root / "proj"
    proj.mkdir()
    pdir = claude / "projects" / encode(proj)
    write_jsonl(pdir / f"{SID_A}.jsonl", [
        "garbage", "[1, 2]", "42", "[" * 100_000, '{"cwd": 5}', '{"type": "user", "message": null}',
    ])
    (pdir / f"{SID_B}.jsonl").write_bytes(b"\xff\xfe\x00\x01" * 100)
    (pdir / f"{SID_C}.jsonl").mkdir()  # not a file, skipped
    got = by_id(core.discover_sessions())
    assert set(got) == {SID_A, SID_B}
    assert got[SID_A].project_path == str(proj)
    assert got[SID_A].first_prompt is None


def test_transcript_head_huge_line_is_bounded(tmp_path):
    p = tmp_path / "t.jsonl"
    huge = json.dumps({"cwd": "/", "pad": "x" * (core._TRANSCRIPT_HEAD_BYTES * 2)})
    write_jsonl(p, [huge, {"cwd": "/second"}])
    assert core._transcript_head(p) == []


def test_transcript_head_line_limit(tmp_path):
    p = write_jsonl(tmp_path / "t.jsonl", [{"n": i} for i in range(100)])
    assert [r["n"] for r in core._transcript_head(p)] == list(range(core._TRANSCRIPT_HEAD_LINES))


def test_discover_strips_control_from_project_name(claude, tmp_path):
    proj = tmp_path / "evil\x1b]0;pwn\x07\x9bname"
    proj.mkdir()
    write_jsonl(claude / "projects" / "-evil" / f"{SID_A}.jsonl", [{"cwd": str(proj)}])
    [s] = core.discover_sessions()
    assert s.project_name == "evil]0;pwnname"


@needs_proc
def test_discover_links_live_pid_and_tmux(claude, tmp_path, monkeypatch):
    proj = tmp_path / "p"
    proj.mkdir()
    pdir = claude / "projects" / encode(proj)
    write_jsonl(pdir / f"{SID_A}.jsonl", [{"cwd": str(proj)}])
    write_jsonl(pdir / f"{SID_B}.jsonl", [{"cwd": str(proj)}])
    (claude / "sessions" / f"{os.getpid()}.json").write_text(
        json.dumps({"sessionId": SID_A, "procStart": own_start_ticks()}))
    (claude / "sessions" / f"{DEAD_PID}.json").write_text(json.dumps({"sessionId": SID_B}))
    tmux = {os.getpid(): TmuxInfo("work", "%3"), DEAD_PID: TmuxInfo("stale", "%9")}
    monkeypatch.setattr(core, "find_cc_tmux_sessions", lambda: tmux)
    got = by_id(core.discover_sessions())
    a, b = got[SID_A], got[SID_B]
    assert (a.pid, a.is_alive, a.tmux_session, a.tmux_pane) == (os.getpid(), True, "work", "%3")
    assert (b.pid, b.is_alive, b.tmux_session, b.tmux_pane) == (DEAD_PID, False, None, None)


def test_discover_with_demo_fixture(tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    try:
        import demo_fixture
    finally:
        sys.path.pop(0)
    fx = demo_fixture.build(tmp_path)
    monkeypatch.setenv("CCDOCK_CLAUDE_DIR", str(fx.claude_dir))
    monkeypatch.setattr(core, "find_cc_tmux_sessions", lambda: {})
    monkeypatch.setattr(subprocess, "run", _no_subprocess)
    sessions = core.discover_sessions()
    assert len(sessions) == len(demo_fixture.SESSIONS)
    assert [s.git_branch for s in sessions] == [row[1] for row in demo_fixture.SESSIONS]
    assert all(s.transcript_path is not None for s in sessions)


# ---------------------------------------------------------------------------
# _build_pid_map
# ---------------------------------------------------------------------------

def test_pid_map_missing_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CCDOCK_CLAUDE_DIR", str(tmp_path))
    assert core._build_pid_map() == {}


def test_pid_map_ignores_bad_filenames_and_data(claude):
    sd = claude / "sessions"
    good = {"sessionId": SID_A, "procStart": 12345}
    for name in ["abc.json", "0.json", "00.json", "-5.json", "¹².json",
                 "١٢.json", "123.txt", "12.5.json"]:
        (sd / name).write_text(json.dumps({"sessionId": SID_B}))
    (sd / "100.json").write_text(json.dumps(good))
    (sd / "101.json").write_text("[1]")
    (sd / "102.json").write_text(json.dumps({"sessionId": 5}))
    (sd / "103.json").write_text("{broken")
    (sd / "104.json").mkdir()
    pid_map = core._build_pid_map()
    assert set(pid_map) == {SID_A}
    assert pid_map[SID_A] == core._PidEntry(pid=100, proc_start="12345")


def test_pid_map_proc_start_absent(claude):
    (claude / "sessions" / "77.json").write_text(json.dumps({"sessionId": SID_A}))
    assert core._build_pid_map()[SID_A].proc_start is None


@needs_proc
def test_pid_map_duplicate_prefers_live(claude):
    sd = claude / "sessions"
    live = sd / f"{os.getpid()}.json"
    dead = sd / f"{DEAD_PID}.json"
    live.write_text(json.dumps({"sessionId": SID_A, "procStart": own_start_ticks()}))
    dead.write_text(json.dumps({"sessionId": SID_A}))
    os.utime(live, (1000, 1000))
    os.utime(dead, (2000, 2000))  # newer, but dead
    assert core._build_pid_map()[SID_A].pid == os.getpid()


def test_pid_map_duplicate_both_dead_prefers_newer(claude):
    sd = claude / "sessions"
    old, new = sd / f"{DEAD_PID}.json", sd / f"{DEAD_PID + 1}.json"
    old.write_text(json.dumps({"sessionId": SID_A}))
    new.write_text(json.dumps({"sessionId": SID_A}))
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    assert core._build_pid_map()[SID_A].pid == DEAD_PID + 1
    os.utime(old, (3000, 3000))
    assert core._build_pid_map()[SID_A].pid == DEAD_PID


@needs_proc
def test_pid_map_duplicate_reused_pid_counts_as_dead(claude):
    """Our pid with a wrong procStart is a reused pid: both count as dead, newer wins."""
    sd = claude / "sessions"
    reused = sd / f"{os.getpid()}.json"
    other = sd / f"{DEAD_PID}.json"
    reused.write_text(json.dumps({"sessionId": SID_A, "procStart": "1"}))
    other.write_text(json.dumps({"sessionId": SID_A}))
    os.utime(reused, (1000, 1000))
    os.utime(other, (2000, 2000))
    assert core._build_pid_map()[SID_A].pid == DEAD_PID


# ---------------------------------------------------------------------------
# _pid_is_alive
# ---------------------------------------------------------------------------

def test_pid_alive_self():
    assert core._pid_is_alive(os.getpid())


def test_pid_dead():
    assert not core._pid_is_alive(DEAD_PID)


@needs_proc
def test_pid_alive_matching_start():
    ticks = own_start_ticks()
    assert core._pid_is_alive(os.getpid(), ticks)
    assert core._pid_is_alive(os.getpid(), f"  {ticks}\n")
    assert core._pid_is_alive(os.getpid(), int(ticks))


@needs_proc
def test_pid_reused_detected():
    wrong = str(int(own_start_ticks()) + 1)
    assert not core._pid_is_alive(os.getpid(), wrong)


@needs_proc
def test_pid_zombie_is_dead():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        deadline = time.monotonic() + 10
        while (core._proc_stat(child.pid) or ("?",))[0] != "Z":
            assert time.monotonic() < deadline, "child never became a zombie"
            time.sleep(0.02)
        assert not core._pid_is_alive(child.pid)
    finally:
        child.wait()


@needs_proc
def test_proc_stat_parses_comm_with_parens(tmp_path):
    # Our own comm is "python3" or similar; just check the shape.
    state, start = core._proc_stat(os.getpid())
    assert state in ("R", "S") and start.isdigit()
    assert core._proc_stat(DEAD_PID) is None


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def test_strip_control():
    s = "a\x1b[31mred\x9b2Jb\x7fc\x00d\x07e\tf\ng\x85h\x9d"
    assert core._strip_control(s) == "a[31mred2Jbcde\tf\ngh"


def test_strip_control_keeps_unicode():
    assert core._strip_control("café  ☃") == "café  ☃"


def test_truncate():
    assert core._truncate(None, 10) == ""
    assert core._truncate("", 10) == ""
    assert core._truncate(5, 10) == ""
    assert core._truncate("  short  ", 10) == "short"
    assert core._truncate("abcdefghij", 10) == "abcdefghij"
    assert core._truncate("abcdefghijk", 10) == "abcdefgh.."
    assert core._truncate("line1\nline2", 20) == "line1 line2"
    assert core._truncate("\x1b]52;c;ZXZpbA==\x07x\x9b", 30) == "]52;c;ZXZpbA==x"


def _ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


@pytest.mark.parametrize("delta, expected", [
    (dict(seconds=10), r"1\ds ago"),
    (dict(seconds=150), r"2m ago"),
    (dict(hours=2, minutes=5), r"2h ago"),
    (dict(days=3, hours=1), r"3d ago"),
    (dict(days=15), r"2w ago"),
    (dict(days=34), r"4w ago"),
    (dict(days=36), r"1mo ago"),
    (dict(days=65), r"2mo ago"),
    (dict(days=400), r"1y ago"),
    (dict(days=800), r"2y ago"),
    (dict(hours=-1), r"now"),
])
def test_relative_time_buckets(delta, expected):
    assert re.fullmatch(expected, core._relative_time(_ago(**delta)))


def test_relative_time_naive_and_z():
    naive = (datetime.now(timezone.utc) - timedelta(hours=3)).replace(tzinfo=None).isoformat()
    assert core._relative_time(naive) == "3h ago"
    z = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert core._relative_time(z) == "2d ago"


@pytest.mark.parametrize("ts", [None, "", "garbage", "2026-13-45"])
def test_relative_time_invalid(ts):
    assert core._relative_time(ts) == ""


def test_parse_iso():
    z = core._parse_iso("2026-01-02T03:04:05Z")
    assert z == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    naive = core._parse_iso("2026-01-02T03:04:05")
    assert naive.tzinfo is timezone.utc and naive == z
    off = core._parse_iso("2026-01-02T05:04:05+02:00")
    assert off == z
    ms = core._parse_iso("2026-01-02T03:04:05.123Z")
    assert ms.microsecond == 123000


@pytest.mark.parametrize("ts", [None, "", "garbage", "2026-02-30", 12345, ["x"]])
def test_parse_iso_invalid(ts):
    assert core._parse_iso(ts) is None


# ---------------------------------------------------------------------------
# collect_projects / _project_roots
# ---------------------------------------------------------------------------

def sess(path, name=None) -> SessionInfo:
    path = str(path)
    return SessionInfo(session_id="x", project_path=path, project_name=name or os.path.basename(path))


def mkrepo(path: Path) -> Path:
    (path / ".git").mkdir(parents=True)
    return path


def test_project_roots(home, tmp_path, monkeypatch):
    extra = tmp_path / "extra"
    extra.mkdir()
    (home / "notes").mkdir()
    monkeypatch.setenv("CCDOCK_PROJECT_DIRS", f"{extra}::~/notes:")
    sessions = [
        sess(home / "repos" / "a"), sess(home / "repos" / "b"),
        sess(home / "deep" / "x" / "y"),
        sess(home),  # parent of home is outside it
        sess(tmp_path / "outside" / "proj"),
        sess("/"),
    ]
    assert core._project_roots(sessions) == [
        str(home), str(home / "repos"), str(home / "deep" / "x"),
        os.path.realpath(extra), str(home / "notes"),
    ]


def test_project_roots_sibling_prefix_not_inside_home(home):
    """'/.../u2/x' shares a string prefix with home '/.../u' but is not inside it."""
    sibling = Path(str(home) + "2") / "x"
    assert core._project_roots([sess(sibling)]) == [str(home)]


def test_collect_projects(home, tmp_path):
    mkrepo(home / "alpha")
    mkrepo(home / ".dotrepo")
    (home / "plain").mkdir()
    (home / "afile").write_text("x")
    repos = home / "repos"
    beta = mkrepo(repos / "beta")
    mkrepo(repos / "Gamma")
    mkrepo(repos / "delta")
    mkrepo(repos / ".hidden")
    (repos / "notgit").mkdir()
    outside = tmp_path / "outside"
    mkrepo(outside / "proj")
    mkrepo(outside / "sibling")

    sessions = [sess(beta), sess(outside / "proj"), sess(beta)]
    assert core.collect_projects(sessions) == [
        (str(beta), "beta"),
        (str(outside / "proj"), "proj"),
        (str(home / "alpha"), "alpha"),
        (str(repos / "delta"), "delta"),
        (str(repos / "Gamma"), "Gamma"),
    ]


def test_collect_projects_extra_dirs(home, tmp_path, monkeypatch):
    extra = tmp_path / "extra"
    mkrepo(extra / "zeta")
    mkrepo(extra / "evil\x1bname")
    monkeypatch.setenv("CCDOCK_PROJECT_DIRS", f"{extra}:{tmp_path / 'missing'}")
    assert core.collect_projects([]) == [
        (str(extra / "evil\x1bname"), "evilname"),
        (str(extra / "zeta"), "zeta"),
    ]


def test_collect_projects_no_sessions_scans_home_only(home, tmp_path):
    mkrepo(home / "r1")
    mkrepo(home / "repos" / "nested")  # one level only, not recursive
    assert core.collect_projects([]) == [(str(home / "r1"), "r1")]


# ---------------------------------------------------------------------------
# Process identity and pidDomain
# ---------------------------------------------------------------------------

def test_session_process_without_start_time_must_be_claude(monkeypatch):
    # No procStart: a live PID only counts if its command name says claude.
    # Our own process is python, i.e. a stale pid file now pointing elsewhere.
    monkeypatch.setattr(core, "_looks_like_claude", lambda pid: False)
    assert not core._is_session_process(os.getpid(), None)
    monkeypatch.setattr(core, "_looks_like_claude", lambda pid: True)
    assert core._is_session_process(os.getpid(), None)


def test_looks_like_claude_rejects_this_process():
    assert not core._looks_like_claude(os.getpid())


@needs_proc
def test_session_process_with_matching_start_time_skips_name_check(monkeypatch):
    monkeypatch.setattr(core, "_looks_like_claude", lambda pid: pytest.fail("not needed"))
    assert core._is_session_process(os.getpid(), own_start_ticks())


@pytest.mark.parametrize("domain, foreign", [
    ("linux:OTHER:p", True),
    ("linux:LOCAL:p", False),
    ("darwin:whatever", False),
    (None, False),
    (5, False),
    ("linux", False),
])
def test_foreign_pid_domain(monkeypatch, domain, foreign):
    monkeypatch.setattr(core, "_local_machine_id", lambda: "LOCAL")
    assert core._foreign_pid_domain(domain) is foreign


def test_foreign_pid_domain_without_local_machine_id(monkeypatch):
    monkeypatch.setattr(core, "_local_machine_id", lambda: None)
    assert not core._foreign_pid_domain("linux:OTHER:p")


def test_pid_map_marks_other_machines_remote(claude, monkeypatch):
    monkeypatch.setattr(core, "_local_machine_id", lambda: "LOCAL")
    sd = claude / "sessions"
    (sd / "101.json").write_text(json.dumps({"sessionId": SID_A, "pidDomain": "linux:OTHER:p"}))
    (sd / "102.json").write_text(json.dumps({"sessionId": SID_B, "pidDomain": "linux:LOCAL:p"}))
    pid_map = core._build_pid_map()
    assert pid_map[SID_A].remote and not pid_map[SID_B].remote


def test_pid_map_remote_beats_dead_local(claude, monkeypatch):
    # Crashed here, then resumed on another machine: it runs there now.
    monkeypatch.setattr(core, "_local_machine_id", lambda: "LOCAL")
    sd = claude / "sessions"
    (sd / f"{DEAD_PID}.json").write_text(json.dumps({"sessionId": SID_A}))
    (sd / "101.json").write_text(json.dumps({"sessionId": SID_A, "pidDomain": "linux:OTHER:p"}))
    assert core._build_pid_map()[SID_A].remote


@needs_proc
def test_pid_map_live_local_beats_remote(claude, monkeypatch):
    monkeypatch.setattr(core, "_local_machine_id", lambda: "LOCAL")
    sd = claude / "sessions"
    (sd / "101.json").write_text(json.dumps({"sessionId": SID_A, "pidDomain": "linux:OTHER:p"}))
    (sd / f"{os.getpid()}.json").write_text(
        json.dumps({"sessionId": SID_A, "procStart": own_start_ticks()}))
    entry = core._build_pid_map()[SID_A]
    assert (entry.pid, entry.remote) == (os.getpid(), False)


@needs_proc
def test_discover_remote_session_is_not_alive(claude, tmp_path, monkeypatch):
    # The remote PID is this very process: it must not be checked or reported.
    monkeypatch.setattr(core, "_local_machine_id", lambda: "LOCAL")
    proj = tmp_path / "p"
    proj.mkdir()
    write_jsonl(claude / "projects" / encode(proj) / f"{SID_A}.jsonl", [{"cwd": str(proj)}])
    (claude / "sessions" / f"{os.getpid()}.json").write_text(json.dumps(
        {"sessionId": SID_A, "pidDomain": "linux:OTHER:p", "procStart": own_start_ticks()}))
    s = core.discover_sessions()[0]
    assert (s.remote, s.is_alive, s.tmux_session) == (True, False, None)


@needs_proc
def test_discover_marks_tmux_unknown(claude, tmp_path, monkeypatch):
    monkeypatch.setattr(core, "find_cc_tmux_sessions", lambda: None)
    proj = tmp_path / "p"
    proj.mkdir()
    pdir = claude / "projects" / encode(proj)
    write_jsonl(pdir / f"{SID_A}.jsonl", [{"cwd": str(proj)}])
    write_jsonl(pdir / f"{SID_B}.jsonl", [{"cwd": str(proj)}])
    (claude / "sessions" / f"{os.getpid()}.json").write_text(
        json.dumps({"sessionId": SID_A, "procStart": own_start_ticks()}))
    got = by_id(core.discover_sessions())
    assert got[SID_A].tmux_unknown and got[SID_A].is_alive
    assert not got[SID_B].tmux_unknown


def _fake_tmux(monkeypatch, result=None, exc=None):
    def run(argv, *args, **kwargs):
        if exc is not None:
            raise exc
        return result
    monkeypatch.setattr(subprocess, "run", run)


def test_tmux_query_timeout_is_unknown(monkeypatch):
    _fake_tmux(monkeypatch, exc=subprocess.TimeoutExpired(["tmux"], 5))
    assert core.find_cc_tmux_sessions() is None


def test_tmux_query_error_is_unknown(monkeypatch):
    _fake_tmux(monkeypatch, subprocess.CompletedProcess(["tmux"], 1, "", "server exited unexpectedly"))
    assert core.find_cc_tmux_sessions() is None


@pytest.mark.parametrize("stderr", [
    "no server running on /tmp/tmux-1000/default",
    "error connecting to /tmp/tmux-1000/default (No such file or directory)",
])
def test_tmux_no_server_is_empty(monkeypatch, stderr):
    _fake_tmux(monkeypatch, subprocess.CompletedProcess(["tmux"], 1, "", stderr))
    assert core.find_cc_tmux_sessions() == {}


def test_tmux_not_installed_is_empty(monkeypatch):
    _fake_tmux(monkeypatch, exc=FileNotFoundError("tmux"))
    assert core.find_cc_tmux_sessions() == {}


# ---------------------------------------------------------------------------
# Titles and first prompts

def title(kind, text):
    key = "customTitle" if kind == "custom" else "aiTitle"
    return {"type": f"{kind}-title", key: text, "sessionId": SID_A}


@pytest.mark.parametrize("records, expected", [
    ([title("ai", "old ai"), title("ai", "new ai")], "new ai"),
    ([title("custom", "mine"), title("ai", "generated")], "mine"),
    ([title("custom", "old"), title("custom", "renamed")], "renamed"),
    ([title("custom", "  "), title("ai", "generated")], "generated"),
    ([{"type": "custom-title", "customTitle": 5}], None),
    ([user_msg("hi")], None),
])
def test_transcript_title(tmp_path, records, expected):
    path = write_jsonl(tmp_path / "t.jsonl", records)
    assert core._transcript_title(path) == expected


def test_transcript_title_reads_only_the_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_TRANSCRIPT_TAIL_BYTES", 4096)
    filler = [user_msg("x" * 1000) for _ in range(20)]
    path = write_jsonl(tmp_path / "t.jsonl", [title("custom", "early"), *filler, title("ai", "late")])
    assert core._transcript_title(path) == "late"


def test_discover_uses_title_as_summary(claude, tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    write_jsonl(claude / "projects" / encode(proj) / f"{SID_A}.jsonl",
                [{"cwd": str(proj)}, user_msg("the prompt"), title("ai", "A title")])
    s = core.discover_sessions()[0]
    assert (s.summary, s.first_prompt) == ("A title", "the prompt")


@pytest.mark.parametrize("texts, expected", [
    (['\n\n<pasted_content id="ab12">\nthe pasted text\n</pasted_content>\nand more'],
     "the pasted text\n\nand more"),
    (["<local-command-caveat>Caveat: run directly</local-command-caveat>",
      "<command-name>/clear</command-name>\n  <command-message>clear</command-message>\n"
      "  <command-args></command-args>",
      "real work"], "real work"),
    (["<system-reminder>x</system-reminder>\nfix the bug"], "fix the bug"),
    (["<unclosed>text"], None),
    (["[Request interrupted by user]"], None),
])
def test_first_user_text_strips_injected_tags(texts, expected):
    assert core._first_user_text([user_msg(t) for t in texts]) == expected
