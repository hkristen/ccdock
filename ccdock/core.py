"""ccdock: Claude Code Session Manager (data layer + actions)"""

import json
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Data Layer
# ---------------------------------------------------------------------------

@dataclass
class SessionInfo:
    session_id: str
    project_path: str
    project_name: str
    summary: str | None = None
    first_prompt: str | None = None
    git_branch: str | None = None
    created: str | None = None
    modified: str | None = None
    pid: int | None = None
    is_alive: bool = False
    tmux_session: str | None = None  # tmux session id ('$N'), a valid attach target
    tmux_pane: str | None = None
    proc_start: str | None = None  # start ticks of the recorded process, see _pid_is_alive
    transcript_path: Path | None = None  # .jsonl transcript, needed for claude --resume
    # A pid file from another machine (shared home over NFS) claims this
    # session: it is most likely running there, and its PID means nothing here.
    remote: bool = False
    # Alive, but tmux could not be queried, so whether it runs in tmux is unknown.
    tmux_unknown: bool = False


@dataclass
class TmuxInfo:
    session_id: str  # tmux's '$N' id, immune to name mangling (see find_cc_tmux_sessions)
    pane_id: str


@dataclass
class _PidEntry:
    pid: int
    proc_start: str | None  # process start time in clock ticks, as recorded by claude
    remote: bool = False  # recorded by another machine, see _foreign_pid_domain


def _claude_dir() -> Path:
    """The Claude Code data directory, ~/.claude unless CCDOCK_CLAUDE_DIR is set.

    The override exists for demos and tests, so ccdock can be pointed at a
    synthetic tree without touching the real one.
    """
    override = os.environ.get("CCDOCK_CLAUDE_DIR")
    if override:
        return Path(override)
    return Path.home() / ".claude"


def _proc_stat(pid: int) -> tuple[str, str] | None:
    """Return (state, starttime) from /proc/<pid>/stat, or None if unavailable."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read()
    except OSError:
        return None
    # The comm field is wrapped in parentheses and may contain spaces, so split
    # on the last ')' and count fields from there: rest[0] is state (field 3),
    # rest[19] is starttime (field 22).
    rest = data.rsplit(b")", 1)[-1].split()
    if len(rest) < 20:
        return None
    return rest[0].decode(errors="replace"), rest[19].decode(errors="replace")


def _pid_is_alive(pid: int, proc_start: str | None = None) -> bool:
    """Check that a PID is alive and still belongs to the recorded process.

    PIDs get reused after a process exits, and a stale session file would
    otherwise make an unrelated process look like a live Claude session (and
    become the target of a stop). When claude recorded the process start time,
    it is compared against /proc so a reused PID is treated as dead. Zombies
    are treated as dead too. On systems without /proc only the signal check is
    available.
    """
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    stat = _proc_stat(pid)
    if stat is None:
        # The signal check passed but /proc/<pid>/stat is gone: either there
        # is no procfs at all, or the process exited between the two calls.
        return not os.path.exists("/proc/self/stat")
    state, start_ticks = stat
    if state == "Z":
        return False
    if proc_start and str(proc_start).strip() != start_ticks:
        return False
    return True


def _looks_like_claude(pid: int) -> bool:
    """Check by command name that pid is a claude process, via ps.

    Used when the start time cannot be compared (no /proc, or a pid file
    without procStart): a stale pid file whose PID now belongs to some other
    process must not make that process a target for SIGTERM.
    """
    try:
        r = subprocess.run(["ps", "-p", str(pid), "-o", "comm="],
                           capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and "claude" in os.path.basename(r.stdout.strip()).lower()


def _is_session_process(pid: int, proc_start: str | None) -> bool:
    """Check that pid is alive and is the claude process a pid file recorded.

    With /proc and a recorded start time the start-time comparison in
    _pid_is_alive settles it. Otherwise the identity cannot be proven, and the
    process only counts if its command name says claude.
    """
    if not _pid_is_alive(pid, proc_start):
        return False
    if proc_start and _proc_stat(pid) is not None:
        return True
    return _looks_like_claude(pid)


def _local_machine_id() -> str | None:
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(path, encoding="ascii", errors="replace") as f:
                value = f.read().strip()
        except OSError:
            continue
        if value:
            return value
    return None


def _foreign_pid_domain(domain) -> bool:
    """True if a pid file's pidDomain names another machine.

    Claude records 'linux:<machine-id>:...'. With a home directory shared
    between hosts (NFS), pid files of other machines sit next to the local
    ones, and their PIDs mean nothing here. Unknown formats are not filtered.
    """
    if not isinstance(domain, str):
        return False
    parts = domain.split(":")
    if len(parts) < 2 or parts[0] != "linux":
        return False
    local = _local_machine_id()
    return local is not None and parts[1] != local


# Upper bounds on what is read from ~/.claude. The pid files and index files
# are a few KiB; a transcript's first records (the only ones inspected) are
# far smaller than the head budget in practice. Anything larger is either
# corrupt or crafted, and reading it whole would exhaust memory.
_JSON_MAX_BYTES = 16 * 1024 * 1024
_TRANSCRIPT_HEAD_BYTES = 256 * 1024
_TRANSCRIPT_HEAD_LINES = 30
# Session titles are appended (and re-appended on every rename) as the
# session goes on, so the newest one is near the end of the transcript.
_TRANSCRIPT_TAIL_BYTES = 256 * 1024


def _read_json(path: Path):
    # is_file keeps a FIFO or directory from blocking or failing open(); the
    # JSON decoder raises RecursionError (not JSONDecodeError) on deeply
    # nested input, and JSONDecodeError is a ValueError.
    try:
        if not path.is_file() or path.stat().st_size > _JSON_MAX_BYTES:
            return None
        with open(path, encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except (OSError, ValueError, RecursionError):
        return None


def _transcript_head(path: Path) -> list[dict]:
    """Return the dict records among the first lines of a .jsonl transcript.

    At most _TRANSCRIPT_HEAD_BYTES are read, so one enormous line cannot pull
    hundreds of MB into memory; a line cut off by the budget simply fails to
    parse and is skipped.
    """
    try:
        if not path.is_file():
            return []
        with open(path, "rb") as f:
            head = f.read(_TRANSCRIPT_HEAD_BYTES)
    except OSError:
        return []
    records: list[dict] = []
    for line in head.decode("utf-8", errors="replace").splitlines()[:_TRANSCRIPT_HEAD_LINES]:
        try:
            data = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(data, dict):
            records.append(data)
    return records


def _transcript_title(path: Path) -> str | None:
    """Return the session's title from the end of a .jsonl transcript.

    Claude Code records a title the user set ('custom-title', from /rename)
    and one it generated itself ('ai-title'); the newest of each wins, and a
    user's title beats a generated one. Only the last _TRANSCRIPT_TAIL_BYTES
    are read; the first line of that window is usually cut off and skipped.
    """
    try:
        with open(path, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - _TRANSCRIPT_TAIL_BYTES))
            tail = f.read(_TRANSCRIPT_TAIL_BYTES)
    except OSError:
        return None
    ai_title = None
    for line in reversed(tail.decode("utf-8", errors="replace").splitlines()):
        # Cheap pre-filter: most lines are large and none of them titles.
        if '-title"' not in line:
            continue
        try:
            data = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get("type") == "custom-title":
            title = _opt_str(data.get("customTitle"))
            if title and title.strip():
                return title.strip()
        elif data.get("type") == "ai-title" and ai_title is None:
            title = _opt_str(data.get("aiTitle"))
            if title and title.strip():
                ai_title = title.strip()
    return ai_title


def _opt_str(value) -> str | None:
    """Coerce an untrusted JSON field to a non-empty str or None."""
    return value if isinstance(value, str) and value else None


# C0 controls except tab and newline, DEL, and the C1 range. Transcript text
# and directory names are displayed verbatim, and a raw ESC or a C1 CSI/OSC
# introducer would reach the terminal as an escape sequence (title spoofing,
# OSC 52 clipboard writes).
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _strip_control(s: str) -> str:
    """Remove control characters that could form terminal escape sequences."""
    return _CONTROL_RE.sub("", s)


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None
    if dt.tzinfo is None:
        # Naive datetimes cannot be compared with aware ones, so assume UTC.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _decode_project_dir(dirname: str) -> str:
    """Convert a claude projects directory name back to a filesystem path.

    Claude encodes a project path by replacing both '/' and '.' with '-', so
    '/home/user.name/my-project' becomes '-home-user-name-my-project'. The
    encoding is lossy, so the path is reconstructed segment by segment by
    trying every combination of '-' and '.' as separators and keeping the
    first candidate that exists on disk. Longer candidates go first, so a
    component costs up to 63 probes; the overall budget allows a few dozen
    components, deeper than any realistic project path.

    The result is always an absolute path. The callers hand it to 'git -C'
    and use it as the working directory for a new claude session, and a
    relative path there would resolve against ccdock's own cwd. A directory
    name without the leading '-' (not produced by claude) is taken as the
    encoding of a path directly under '/', and when the reconstruction gives
    up part way the unresolved remainder is appended as one segment.
    """
    parts = (dirname[1:] if dirname.startswith("-") else dirname).split("-")
    if not any(parts):
        return "/"
    path = "/"
    remaining = list(parts)
    result_parts: list[str] = []
    budget = 4096
    while remaining:
        found = False
        max_end = min(len(remaining), 6)
        for end in range(max_end, 0, -1):
            segments = remaining[:end]
            n_seps = end - 1
            for mask in range(1 << n_seps):
                budget -= 1
                if budget <= 0:
                    result_parts.append("-".join(remaining))
                    return "/" + "/".join(result_parts)
                candidate = segments[0]
                for i in range(n_seps):
                    sep = "." if (mask >> i) & 1 else "-"
                    candidate += sep + segments[i + 1]
                test_path = os.path.join(path, candidate)
                if os.path.exists(test_path):
                    path = test_path
                    result_parts.append(candidate)
                    remaining = remaining[end:]
                    found = True
                    break
            if found:
                break
        if not found:
            result_parts.append("-".join(remaining))
            break
    return "/" + "/".join(result_parts)


def _pid_entry_rank(entry: _PidEntry) -> int:
    if entry.remote:
        return 1
    return 2 if _is_session_process(entry.pid, entry.proc_start) else 0


def _build_pid_map() -> dict[str, _PidEntry]:
    """Map session_id -> PID entry from ~/.claude/sessions/*.json."""
    sessions_dir = _claude_dir() / "sessions"
    pid_map: dict[str, _PidEntry] = {}
    if not sessions_dir.is_dir():
        return pid_map
    try:
        entries = list(sessions_dir.iterdir())
    except OSError:
        return pid_map
    mtimes: dict[str, float] = {}
    for p in entries:
        # str.isdigit accepts Unicode digits (superscripts, for one) that
        # int() rejects, and pid 0 would make os.kill(0, 0) target the whole
        # process group.
        if p.suffix != ".json" or not (p.stem.isascii() and p.stem.isdigit()):
            continue
        pid = int(p.stem)
        if pid <= 0:
            continue
        data = _read_json(p)
        if not isinstance(data, dict):
            continue
        sid = data.get("sessionId")
        if not isinstance(sid, str) or not sid:
            continue
        proc_start = data.get("procStart")
        entry = _PidEntry(
            pid=pid,
            proc_start=str(proc_start) if proc_start is not None else None,
            remote=_foreign_pid_domain(data.get("pidDomain")),
        )
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0.0
        prev = pid_map.get(sid)
        if prev is not None:
            # Two session files claim the same id (typically a stale one left
            # behind by a crashed process): prefer a live local process, then
            # another machine's file (it cannot be checked from here, so it
            # may be running), then the more recently written file.
            new_rank, prev_rank = _pid_entry_rank(entry), _pid_entry_rank(prev)
            if new_rank != prev_rank:
                keep_new = new_rank > prev_rank
            else:
                keep_new = mtime > mtimes[sid]
            if not keep_new:
                continue
        pid_map[sid] = entry
        mtimes[sid] = mtime
    return pid_map


def find_cc_tmux_sessions() -> dict[int, TmuxInfo] | None:
    """Map Claude Code PID -> TmuxInfo by walking tmux pane process trees.

    Returns None when tmux is installed but could not be queried (a timeout
    on a loaded machine, an unexpected error): live sessions then cannot be
    told apart into "in tmux" and "outside tmux", and treating them as
    outside would offer to stop and restart a session that is attachable.
    No tmux binary, or no tmux server running, is a definite empty answer.

    Sessions are recorded by id ('$N'), not name: tmux prints non-ASCII
    names as '_' under a non-UTF-8 locale (typical over SSH), and a target
    built from such a name would not match the session.

    Every pane's process and all of its descendants map to that pane. The
    process tree is taken from one 'ps' snapshot rather than one 'pgrep -P'
    per process: a refresh on a busy server otherwise spawned dozens of
    processes.
    """
    result: dict[int, TmuxInfo] = {}
    try:
        out = subprocess.run(
            ["tmux", "list-panes", "-a", "-F",
             "#{session_id}:#{pane_id}:#{pane_pid}"],
            capture_output=True, text=True, timeout=5,
        )
    except FileNotFoundError:
        return result
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        # tmux reports a missing server as "no server running on ..." or,
        # when the socket directory is gone, "error connecting to ...".
        err = out.stderr.lower()
        if "no server running" in err or "error connecting" in err:
            return result
        return None

    panes: list[tuple[int, TmuxInfo]] = []
    for line in out.stdout.strip().splitlines():
        parts = line.split(":")
        if len(parts) != 3:
            continue
        sess_id, pane_id, pane_pid_str = parts
        try:
            pane_pid = int(pane_pid_str)
        except ValueError:
            continue
        panes.append((pane_pid, TmuxInfo(session_id=sess_id, pane_id=pane_id)))
    if not panes:
        return result

    children = _process_children()
    for pane_pid, info in panes:
        # tmux execs the pane command directly (and shells exec a lone command
        # too), so claude is usually the pane process itself, not a descendant.
        for cpid in [pane_pid, *_get_descendants(pane_pid, children)]:
            result[cpid] = info
    return result


def _process_children() -> dict[int, list[int]]:
    """Map parent PID -> child PIDs for every process, from one 'ps' call.

    Without a usable 'ps' the map is empty, so only the pane processes
    themselves are matched.
    """
    children: dict[int, list[int]] = {}
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,ppid="],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode != 0:
            return children
    except (OSError, subprocess.TimeoutExpired):
        return children
    for line in out.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        try:
            pid, ppid = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append(pid)
    return children


def _get_descendants(pid: int, children: dict[int, list[int]]) -> list[int]:
    """All descendant PIDs of pid according to a parent -> children map."""
    descendants: list[int] = []
    seen: set[int] = {pid}
    stack = list(children.get(pid, []))
    while stack:
        cpid = stack.pop()
        if cpid in seen:
            continue
        seen.add(cpid)
        descendants.append(cpid)
        stack.extend(children.get(cpid, []))
    return descendants


def _git_branch(project_path: str) -> str | None:
    """Get the current git branch for a project directory."""
    try:
        out = subprocess.run(
            ["git", "-C", project_path, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=3,
        )
        if out.returncode == 0:
            return out.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def _transcript_cwd(path: Path) -> str | None:
    """Return the project directory recorded in a .jsonl transcript."""
    return _records_cwd(_transcript_head(path))


def _records_cwd(records: list[dict]) -> str | None:
    """Return the project directory recorded in transcript records.

    Most transcript records carry a top-level "cwd". It is the authoritative
    project path: the projects directory name only holds a lossy encoding of
    it (see _decode_project_dir). The value is only trusted when it names an
    existing absolute directory: the path ends up in 'git -C' and as the
    working directory of 'claude --resume', so a crafted transcript must not
    be able to point those anywhere. It is returned in realpath form.
    """
    for data in records:
        cwd = data.get("cwd")
        if isinstance(cwd, str) and cwd:
            return _valid_project_dir(cwd)
    return None


def _records_branch(records: list[dict]) -> str | None:
    """Return the git branch recorded in transcript records.

    This is the branch the session ran on, which the repository's current
    branch (what 'git rev-parse' reports today) need not be.
    """
    for data in records:
        branch = _opt_str(data.get("gitBranch"))
        if branch:
            return branch
    return None


def _valid_project_dir(cwd: str) -> str | None:
    """Return cwd normalised with realpath if it is an existing absolute
    directory, else None."""
    if "\0" in cwd or not os.path.isabs(cwd):
        return None
    try:
        real = os.path.realpath(cwd)
        if not os.path.isdir(real):
            return None
    except (OSError, ValueError):
        return None
    return real


def _first_user_text(records: list[dict]) -> str | None:
    """Extract the first user text message from transcript records."""
    for data in records:
        if data.get("type") != "user":
            continue
        message = data.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content", [])
        if isinstance(content, str):
            content = [content]
        if not isinstance(content, list):
            continue
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                text = c.get("text")
            elif isinstance(c, str):
                text = c
            else:
                continue
            if not isinstance(text, str):
                continue
            text = _strip_leading_tags(text)
            # Skip system-injected content and interrupts
            if not text or text.startswith(("<", "[")):
                continue
            return text[:120]
    return None


# A leading <tag ...>...</tag> block. Claude Code wraps injected context
# (command caveats, slash-command echoes, system reminders) this way.
_LEADING_TAG_RE = re.compile(r"\s*<([A-Za-z][\w-]*)(?:\s[^>]*)?>(.*?)</\1>", re.DOTALL)
# Wrappers around text the user wrote or pasted: their content is the prompt.
_CONTENT_TAGS = frozenset({"pasted_content"})


def _strip_leading_tags(text: str) -> str:
    """Drop leading injected tag blocks, unwrapping pasted user content."""
    for _ in range(20):
        m = _LEADING_TAG_RE.match(text)
        if not m:
            break
        if m.group(1) in _CONTENT_TAGS:
            text = m.group(2) + text[m.end():]
        else:
            text = text[m.end():]
    return text.strip()


def _safe_session_id(sid: str) -> bool:
    """A session id from an index file is used as a file name; refuse anything
    that could escape the project directory or exceed a file name's length."""
    return (
        len(sid) <= 200
        and os.path.basename(sid) == sid
        and sid not in (".", "..")
        and "\0" not in sid
    )


def _make_session(
    sid: str,
    project_path: str,
    proj_dir: Path,
    pid_map: dict[str, _PidEntry],
    tmux_map: dict[int, TmuxInfo] | None,
    **fields,
) -> SessionInfo:
    # The basename is displayed verbatim, so control characters in a
    # directory name are dropped here.
    project_name = _strip_control(
        os.path.basename(project_path.rstrip("/")) or proj_dir.name
    )
    entry = pid_map.get(sid)
    pid = entry.pid if entry else None
    remote = bool(entry and entry.remote)
    is_alive = (
        _is_session_process(entry.pid, entry.proc_start)
        if entry and not remote else False
    )
    tmux_info = tmux_map.get(pid) if (pid and is_alive and tmux_map) else None
    transcript = proj_dir / f"{sid}.jsonl"
    try:
        # Path.is_file only swallows some errnos; an over-long id raises
        # ENAMETOOLONG out of it.
        has_transcript = transcript.is_file()
    except OSError:
        has_transcript = False
    return SessionInfo(
        session_id=sid,
        project_path=project_path,
        project_name=project_name,
        pid=pid,
        is_alive=is_alive,
        tmux_session=tmux_info.session_id if tmux_info else None,
        tmux_pane=tmux_info.pane_id if tmux_info else None,
        proc_start=entry.proc_start if entry else None,
        transcript_path=transcript if has_transcript else None,
        remote=remote,
        tmux_unknown=is_alive and tmux_map is None,
        **fields,
    )


def discover_sessions() -> list[SessionInfo]:
    """Discover all Claude Code sessions from ~/.claude/projects/."""
    projects_dir = _claude_dir() / "projects"
    if not projects_dir.is_dir():
        return []

    pid_map = _build_pid_map()
    tmux_map = find_cc_tmux_sessions()
    sessions: list[SessionInfo] = []
    seen_sids: set[str] = set()

    try:
        # Sorted, so which row wins when an id appears twice does not depend
        # on the order the filesystem lists directories in.
        proj_dirs = sorted(p for p in projects_dir.iterdir() if p.is_dir())
    except OSError:
        return []

    # Decoding a directory name probes the filesystem, so it is done at most
    # once per project directory and only when no transcript names the path.
    decoded_cache: dict[str, str] = {}

    def decoded_path(proj_dir: Path) -> str:
        if proj_dir.name not in decoded_cache:
            decoded_cache[proj_dir.name] = _decode_project_dir(proj_dir.name)
        return decoded_cache[proj_dir.name]

    for proj_dir in proj_dirs:
        # 1. Read from sessions-index.json if it exists
        index_data = _read_json(proj_dir / "sessions-index.json")
        entries = index_data.get("entries") if isinstance(index_data, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            sid = entry.get("sessionId")
            if not isinstance(sid, str) or not sid or not _safe_session_id(sid):
                continue
            # An index may list the same id twice; the table keys rows by id.
            if sid in seen_sids:
                continue
            seen_sids.add(sid)

            # projectPath gets the same checks as a transcript cwd: it ends up
            # as the working directory of a new claude session too.
            index_path = _opt_str(entry.get("projectPath"))
            project_path = (
                (_valid_project_dir(index_path) if index_path else None)
                or _transcript_cwd(proj_dir / f"{sid}.jsonl")
                or decoded_path(proj_dir)
            )

            first_prompt = _opt_str(entry.get("firstPrompt"))
            if first_prompt and len(first_prompt) > 120:
                first_prompt = first_prompt[:117] + "..."

            sessions.append(_make_session(
                sid, project_path, proj_dir, pid_map, tmux_map,
                summary=_opt_str(entry.get("summary")),
                first_prompt=first_prompt,
                git_branch=_opt_str(entry.get("gitBranch")),
                created=_opt_str(entry.get("created")),
                modified=_opt_str(entry.get("modified")),
            ))

    # 2. Discover sessions from .jsonl files not in any index. This runs
    # after every index is read: an index entry carries more than a bare
    # transcript, so it wins when the same id shows up in both.
    for proj_dir in proj_dirs:
        for jsonl_path in sorted(proj_dir.glob("*.jsonl")):
            sid = jsonl_path.stem
            if sid in seen_sids or not jsonl_path.is_file():
                continue
            seen_sids.add(sid)

            records = _transcript_head(jsonl_path)
            project_path = _records_cwd(records) or decoded_path(proj_dir)

            try:
                mtime = jsonl_path.stat().st_mtime
                modified = datetime.fromtimestamp(mtime, timezone.utc).isoformat()
            except (OSError, ValueError, OverflowError):
                # Far-future mtimes fall outside the datetime range.
                modified = None

            # Older transcripts may not record the branch; the repository's
            # current one is the best remaining guess.
            git_branch = _records_branch(records) or (
                _git_branch(project_path) if os.path.isdir(project_path) else None
            )
            sessions.append(_make_session(
                sid, project_path, proj_dir, pid_map, tmux_map,
                summary=_transcript_title(jsonl_path),
                first_prompt=_first_user_text(records),
                git_branch=git_branch,
                modified=modified,
            ))

    def sort_key(s: SessionInfo):
        dt = _parse_iso(s.modified)
        return dt.timestamp() if dt else 0

    sessions.sort(key=sort_key, reverse=True)
    return sessions


# ---------------------------------------------------------------------------
# Formatting Helpers
# ---------------------------------------------------------------------------

def _relative_time(iso_ts: str | None) -> str:
    dt = _parse_iso(iso_ts)
    if not dt:
        return ""
    now = datetime.now(timezone.utc)
    diff = now - dt
    secs = int(diff.total_seconds())
    if secs < 0:
        return "now"
    if secs < 60:
        return f"{secs}s ago"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 7:
        return f"{days}d ago"
    weeks = days // 7
    if weeks < 5:
        return f"{weeks}w ago"
    months = days // 30
    if months < 12:
        return f"{months}mo ago"
    years = months // 12
    return f"{years}y ago"


def _truncate(s: str | None, width: int) -> str:
    if not isinstance(s, str) or not s:
        return ""
    s = _strip_control(s.replace("\n", " ")).strip()
    if len(s) <= width:
        return s
    return s[: width - 2] + ".."


def _has_tmux() -> bool:
    try:
        r = subprocess.run(["tmux", "-V"], capture_output=True, timeout=3)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _in_tmux() -> bool:
    return bool(os.environ.get("TMUX"))


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

_SESSION_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _tmux_target(name: str) -> str:
    """tmux target for a session id ('$N') or an exact session name.

    Without the '=' prefix tmux falls back to prefix matching, so a target of
    'api' could silently resolve to a session called 'api-server'. Ids are
    exact already (session_name_for never produces a name starting with '$').
    """
    return name if name.startswith("$") else f"={name}"


def _tmux_session_exists(name: str) -> bool:
    try:
        r = subprocess.run(
            ["tmux", "has-session", "-t", _tmux_target(name)],
            capture_output=True, timeout=3,
        )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def session_name_for(project_path: str) -> str:
    """Derive a tmux session name from a project path.

    Only word characters (letters and digits in any script, underscore) and
    '-' survive: tmux rejects '.' and ':' outright, and it stores backslashes
    and control characters "visified" (as escape sequences), so a has-session
    probe for such a name would never match the session tmux actually
    created. Every other run of characters becomes a single '-'.
    """
    base = os.path.basename(project_path.rstrip("/")) or "claude"
    name = re.sub(r"[^\w-]+", "-", base).strip("-")[:20].strip("-")
    return name or "claude"


def _unique_session_name(name: str, exclude: frozenset[str] = frozenset()) -> str:
    """Return name, or the first name-N (N >= 2) that no tmux session uses.

    Names in exclude are skipped even if tmux reports them free; create_session
    passes the names it already tried so a lost race moves on to the next one.
    """
    if name not in exclude and not _tmux_session_exists(name):
        return name
    for n in range(2, 100):
        candidate = f"{name}-{n}"
        if candidate not in exclude and not _tmux_session_exists(candidate):
            return candidate
    raise RuntimeError(f"too many tmux sessions named {name!r}")


def _tmux_quiet(*args: str, timeout: float = 3) -> None:
    """Run a tmux command whose failure does not matter."""
    try:
        subprocess.run(["tmux", *args], capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        pass


def attach_session(tmux_session: str, pane_id: str | None = None) -> None:
    """Attach to a tmux session (replaces current process).

    The session is checked to exist first: once exec has replaced the
    process there is nobody left to report that tmux found nothing to
    attach to (a session listed a moment ago may have exited since). With
    pane_id (a tmux pane id like '%3') the window and pane holding that pane
    are selected first, so the client lands on claude rather than on
    whatever window the session showed last. Failures there are ignored: the
    pane may be gone, and attaching still makes sense.
    """
    if not _tmux_session_exists(tmux_session):
        raise RuntimeError(f"tmux session {tmux_session!r} no longer exists")
    target = _tmux_target(tmux_session)
    if pane_id:
        _tmux_quiet("select-window", "-t", pane_id)
        _tmux_quiet("select-pane", "-t", pane_id)
    if _in_tmux():
        os.execvp("tmux", ["tmux", "switch-client", "-t", target])
    else:
        os.execvp("tmux", ["tmux", "attach", "-t", target])


def _claude_command(resume_id: str | None) -> list[str]:
    return ["claude"] + (["--resume", resume_id] if resume_id else [])


def _start_tmux_session(session_name: str, project_path: str, cmd: list[str],
                        attempts: int = 5) -> str:
    """Start a detached tmux session running cmd; return its id ('$N').

    The id rather than the name is returned for attaching, for the reason
    given in find_cc_tmux_sessions.

    Between the has-session probe and new-session someone else may take the
    name (tmux reports "duplicate session"), in which case the next free
    suffix is tried, a bounded number of times. Once the session is up it is
    probed again shortly after: a command that exits at once takes its
    session with it, and reporting that beats attaching to nothing.
    """
    tried: set[str] = set()
    for _ in range(attempts):
        name = _unique_session_name(session_name, frozenset(tried))
        tried.add(name)
        # Checked right before new-session: tmux does not complain about a
        # missing -c directory, it silently starts the command in $HOME.
        if not os.path.isdir(project_path):
            raise RuntimeError(f"project directory {project_path!r} does not exist")
        r = subprocess.run(
            ["tmux", "new-session", "-d", "-P", "-F", "#{session_id}",
             "-s", name, "-c", project_path, *cmd],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            session_id = r.stdout.strip()
            time.sleep(0.5)
            if not session_id or not _tmux_session_exists(session_id):
                raise RuntimeError(
                    f"{cmd[0]} exited immediately inside tmux session {name!r} "
                    "(is it on PATH for the tmux server?)"
                )
            return session_id
        if "duplicate session" in r.stderr:
            continue
        raise RuntimeError(
            f"tmux could not create session {name!r}: {r.stderr.strip() or r.returncode}"
        )
    raise RuntimeError(f"tmux session name {session_name!r} kept being taken")


def validate_resume(project_path: str, resume_id: str | None) -> None:
    """Raise RuntimeError if create_session(project_path, ..., resume_id) cannot work.

    Meant to be called before anything irreversible (such as stopping the
    live session that is about to be resumed), so a resume that would fail
    anyway does not first terminate a running claude. resume_id must be a
    UUID: 'claude --resume' takes an optional value, so anything else (in
    particular a value starting with '-', which a crafted transcript filename
    could produce) would be parsed as a claude flag rather than a session id.
    """
    if resume_id is not None and not _SESSION_ID_RE.fullmatch(resume_id):
        raise RuntimeError(f"cannot resume: {resume_id!r} is not a claude session id")
    if shutil.which("claude") is None:
        raise RuntimeError("claude not found on PATH")
    if not os.path.isdir(project_path):
        raise RuntimeError(f"project directory {project_path!r} does not exist")


def create_session(project_path: str, session_name: str,
                   resume_id: str | None = None) -> None:
    """Create a tmux session running claude in the project dir and attach.

    With resume_id the conversation with that session id is resumed instead of
    starting a fresh one. The preconditions are those of validate_resume,
    which is checked first; callers that stop a live session beforehand
    should call it themselves before doing so.
    """
    validate_resume(project_path, resume_id)
    session_id = _start_tmux_session(session_name, project_path, _claude_command(resume_id))
    attach_session(session_id)


def _terminate(session: SessionInfo) -> None:
    """Send SIGTERM to the session's process if it is still the same process.

    Re-checks right before signalling: the PID may have been recycled since
    discovery, and the start-time comparison catches that.
    """
    if session.pid and session.is_alive and _is_session_process(session.pid, session.proc_start):
        try:
            os.kill(session.pid, signal.SIGTERM)
        except OSError:
            pass


def _wait_for_exit(session: SessionInfo, timeout: float, interval: float = 0.2) -> bool:
    """Wait until the session's process is gone; True if it exited in time."""
    if not session.pid:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _is_session_process(session.pid, session.proc_start):
            return True
        time.sleep(interval)
    return not _is_session_process(session.pid, session.proc_start)


def _pane_pid(pane_id: str) -> int | None:
    """Return the pid of the process running in a tmux pane, None if no such pane.

    Depending on the tmux version a missing pane is reported either by a
    non-zero exit or by exit 0 with the format expanding to nothing, so
    anything that is not a number counts as "no such pane".
    """
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane_id, "#{pane_pid}"],
            capture_output=True, text=True, timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        return int(r.stdout.strip())
    except ValueError:
        return None


def _close_pane_if_safe(session: SessionInfo) -> None:
    """Close the tmux pane claude ran in, but only if it still is that pane.

    Pane ids ('%3') are unique across a tmux server, but they start over at
    %0 when the server restarts, so a recorded id can name an unrelated pane
    by the time a session is stopped. The pane is therefore re-resolved
    first, and closed only when its process is the session's (now exited)
    process or is gone altogether (a pane kept by remain-on-exit); a pane
    hosting some other live process is left alone, as is a pane that no
    longer exists. Only the pane goes, never the session: claude may well be
    one window of the user's everyday tmux session (possibly the very one
    ccdock runs in).
    """
    if not session.tmux_pane:
        return
    pane_pid = _pane_pid(session.tmux_pane)
    if pane_pid is None:
        return
    if pane_pid != session.pid and _pid_is_alive(pane_pid):
        return
    _tmux_quiet("kill-pane", "-t", session.tmux_pane, timeout=5)


def kill_session(session: SessionInfo, timeout: float = 5.0) -> bool:
    """Stop a session's process, then close its tmux pane; True once it is gone.

    Only SIGTERM is sent, and the pane is closed only after the process has
    exited: closing it earlier SIGHUPs a claude that is still flushing its
    transcript, which loses data, and a forced kill could truncate it
    mid-write. If the process is still running after the timeout nothing more
    is done (the pane included) and False is returned, so the caller can
    report it and leave it to the user. The start-time check in _pid_is_alive
    keeps the wait from latching onto a reused PID.
    """
    _terminate(session)
    if not _wait_for_exit(session, timeout):
        return False
    _close_pane_if_safe(session)
    return True


def _project_roots(sessions: list[SessionInfo]) -> list[str]:
    """Directories whose git repositories the project picker offers.

    These are the home directory, every directory inside home that holds a
    project with a session (so ~/repos is searched once any repo in it has
    been used with claude), and the entries of CCDOCK_PROJECT_DIRS
    (colon-separated). Parents outside home are skipped: the parent of a
    home directory is typically a directory of all users' homes.
    """
    home = os.path.realpath(Path.home())
    roots = [home]
    for s in sessions:
        parent = os.path.dirname(os.path.realpath(s.project_path.rstrip("/") or "/"))
        if parent.startswith(home + os.sep):
            roots.append(parent)
    for entry in os.environ.get("CCDOCK_PROJECT_DIRS", "").split(os.pathsep):
        if entry:
            roots.append(os.path.realpath(os.path.expanduser(entry)))
    return list(dict.fromkeys(roots))


def collect_projects(sessions: list[SessionInfo]) -> list[tuple[str, str]]:
    """Collect project paths from sessions, then git repos in the project roots."""
    seen: set[str] = set()
    projects: list[tuple[str, str]] = []
    for s in sessions:
        if s.project_path not in seen:
            seen.add(s.project_path)
            projects.append((s.project_path, s.project_name))
    found: list[tuple[str, str]] = []
    for root in _project_roots(sessions):
        try:
            entries = os.listdir(root)
        except OSError:
            continue
        for entry in entries:
            if entry.startswith("."):
                continue  # dotfile tooling (~/.fzf and the like), not projects
            full = os.path.join(root, entry)
            if full not in seen and os.path.isdir(os.path.join(full, ".git")):
                seen.add(full)
                found.append((full, _strip_control(entry)))
    found.sort(key=lambda p: p[1].lower())
    return projects + found
