"""Record the README demo GIF and screenshot from synthetic data.

Runs ccdock headlessly against the fixture from demo_fixture.py (never the
real ~/.claude), backs two rows with real processes in an isolated tmux
server so they show as live and attached, plays a scripted key sequence,
exports one SVG per step, and assembles screenshots/demo.gif with
ImageMagick. Requires tmux and magick on PATH.

Usage:  python3 scripts/record_demo.py
"""

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import demo_fixture  # noqa: E402  (same directory)

SIZE = (132, 20)
PNG_DENSITY = 80  # 96 is roughly 1:1 with the SVG's nominal size


def tmux(*args: str) -> str:
    return subprocess.run(["tmux", *args], capture_output=True, text=True, check=True).stdout


async def record(frames_dir: Path) -> list[tuple[Path, int]]:
    from textual.widgets import DataTable
    from ccdock.tui import CcdockApp

    frames: list[tuple[Path, int]] = []

    def snap(app: CcdockApp, delay_cs: int) -> None:
        path = frames_dir / f"frame-{len(frames):02d}.svg"
        path.write_text(app.export_screenshot(title="ccdock"), encoding="utf-8")
        frames.append((path, delay_cs))

    app = CcdockApp()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        snap(app, 260)
        # Browse down three rows, then back up to the live data-pipeline row.
        for _ in range(3):
            await pilot.press("j")
            await pilot.pause()
            snap(app, 45)
        for _ in range(2):
            await pilot.press("k")
            await pilot.pause()
            snap(app, 45)
        snap(app, 80)
        # Stop asks for confirmation; decline.
        await pilot.press("s")
        await pilot.pause()
        snap(app, 240)
        await pilot.press("n")
        await pilot.pause()
        snap(app, 100)
        # Manual refresh reports in the status bar.
        await pilot.press("r")
        await app.workers.wait_for_complete()
        await pilot.pause()
        snap(app, 220)
        await pilot.press("k")
        await pilot.pause()
        snap(app, 240)
        await pilot.press("q")
    return frames


def main() -> int:
    for tool in ("tmux", "magick"):
        if shutil.which(tool) is None:
            sys.exit(f"{tool} not found on PATH")

    root = Path(tempfile.mkdtemp(prefix="ccdock-demo-"))
    private_tmux = False  # set once every tmux call is bound to the private server
    try:
        fx = demo_fixture.build(root)
        tmux_dir = root / "tmux"
        tmux_dir.mkdir()
        # Isolate everything: the app reads the fixture, the project picker
        # lists the fake home, tmux talks to a private server.
        os.environ["CCDOCK_CLAUDE_DIR"] = str(fx.claude_dir)
        os.environ["HOME"] = str(fx.home)
        os.environ.pop("TMUX", None)
        os.environ["TMUX_TMPDIR"] = str(tmux_dir)
        private_tmux = True

        for s in fx.sessions:
            if s.live:
                tmux("new-session", "-d", "-s", s.project, "-c", s.project_path, "sleep 600")
                pane_pid = int(tmux("list-panes", "-t", f"={s.project}", "-F", "#{pane_pid}").strip())
                demo_fixture.mark_alive(fx, s, pane_pid)

        frames_dir = root / "frames"
        frames_dir.mkdir()
        frames = asyncio.run(record(frames_dir))

        out_dir = REPO / "screenshots"
        out_dir.mkdir(exist_ok=True)
        shutil.copy(frames[0][0], out_dir / "main-view.svg")

        pngs: list[Path] = []
        for svg, _ in frames:
            png = svg.with_suffix(".png")
            subprocess.run(["magick", "-density", str(PNG_DENSITY), str(svg), str(png)], check=True)
            pngs.append(png)
        cmd = ["magick", "-loop", "0"]
        for (_, delay), png in zip(frames, pngs):
            cmd += ["-delay", str(delay), str(png)]
        cmd += ["-layers", "optimize", str(out_dir / "demo.gif")]
        subprocess.run(cmd, check=True)
        size = (out_dir / "demo.gif").stat().st_size
        print(f"wrote {out_dir / 'demo.gif'} ({size // 1024} KiB, {len(frames)} frames) and main-view.svg")
        return 0
    finally:
        # Only ever kill the private server: before the environment above is
        # set, a bare kill-server would hit the user's own tmux.
        if private_tmux:
            subprocess.run(["tmux", "kill-server"], capture_output=True)
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
