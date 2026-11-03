"""Build a synthetic Claude Code data tree for demos and screenshots.

Everything here is invented: a fake user, fake projects, fake prompts. The
tree is laid out exactly like ~/.claude so ccdock can be pointed at it with
CCDOCK_CLAUDE_DIR (and HOME, for the project picker) without ever reading the
real one.

Usage as a script:  python3 scripts/demo_fixture.py <root>
creates <root>/home/alice/{.claude,repos} and prints the fake home path.
"""

import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

USER = "alice"

# (project, branch, first prompt, age, live)
# "live" rows are the ones the recorder backs with a real process in tmux.
# The layout is that of current Claude Code: transcripts only, no
# sessions-index.json, so the table shows first prompts as real users see them.
SESSIONS = [
    ("api-gateway", "main",
     "add rate limiting middleware to the auth routes, 100 req/min per key",
     timedelta(minutes=3), True),
    ("data-pipeline", "main",
     "the nightly ETL job OOMs on the parquet merge step, profile it and fix",
     timedelta(minutes=12), True),
    ("api-gateway", "feat/oauth-refresh",
     "why does the refresh token endpoint return 401 right after rotation?",
     timedelta(hours=2), False),
    ("data-pipeline", "fix/schema-drift",
     "incoming CSVs keep changing columns, detect drift before the load step",
     timedelta(days=1), False),
    ("docs-site", "main",
     "migrate the docs from sphinx to mkdocs material, keep the URLs stable",
     timedelta(days=3), False),
    ("ml-experiments", "exp/lr-sweep",
     "run a learning rate sweep on the small model and plot the curves",
     timedelta(days=5), False),
    ("ml-experiments", "main",
     "refactor the dataset loader and add unit tests for the augmentations",
     timedelta(weeks=1), False),
    ("dotfiles", "main",
     "set up tmux and neovim configs for the new laptop",
     timedelta(weeks=2), False),
    ("infra", "terraform/eks-upgrade",
     "upgrade the EKS cluster to 1.31 without downtime, plan it step by step",
     timedelta(weeks=3), False),
    ("personal-site", "main",
     "add an RSS feed to the blog and a dark mode toggle",
     timedelta(days=60), False),
]


@dataclass
class DemoSession:
    session_id: str
    project: str
    project_path: str
    live: bool


@dataclass
class Fixture:
    home: Path
    claude_dir: Path
    sessions: list[DemoSession]


def _encode(path: str) -> str:
    return path.replace("/", "-").replace(".", "-")


def build(root: Path) -> Fixture:
    home = root / "home" / USER
    claude_dir = home / ".claude"
    repos = home / "repos"
    (claude_dir / "sessions").mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    sessions: list[DemoSession] = []

    for project, branch, prompt, age, live in SESSIONS:
        project_path = str(repos / project)
        (repos / project / ".git").mkdir(parents=True, exist_ok=True)
        sid = str(uuid.uuid4())
        modified = now - age
        proj_dir = claude_dir / "projects" / _encode(project_path)
        proj_dir.mkdir(parents=True, exist_ok=True)

        records = [
            {"type": "user", "cwd": project_path, "sessionId": sid, "gitBranch": branch,
             "timestamp": (modified - timedelta(minutes=30)).isoformat(),
             "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}},
            {"type": "assistant", "cwd": project_path, "sessionId": sid, "gitBranch": branch,
             "message": {"role": "assistant", "content": [{"type": "text", "text": "On it."}]}},
        ]
        transcript = proj_dir / f"{sid}.jsonl"
        with open(transcript, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        mtime = modified.timestamp()
        os.utime(transcript, (mtime, mtime))
        sessions.append(DemoSession(sid, project, project_path, live))

    return Fixture(home=home, claude_dir=claude_dir, sessions=sessions)


def mark_alive(fixture: Fixture, session: DemoSession, pid: int) -> None:
    """Write a sessions/<pid>.json so ccdock sees this session as running."""
    proc_start = None
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            proc_start = f.read().rsplit(b")", 1)[-1].split()[19].decode()
    except (OSError, IndexError):
        pass
    data = {"pid": pid, "sessionId": session.session_id, "cwd": session.project_path,
            "startedAt": int(time.time() * 1000), "procStart": proc_start,
            "kind": "interactive", "entrypoint": "cli"}
    with open(fixture.claude_dir / "sessions" / f"{pid}.json", "w", encoding="utf-8") as f:
        json.dump(data, f)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: demo_fixture.py <root>")
    fx = build(Path(sys.argv[1]))
    print(fx.home)
