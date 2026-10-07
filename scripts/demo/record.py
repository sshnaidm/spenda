"""Record the README demo animation from synthetic data.

    uv run --with playwright --with pillow python scripts/demo/record.py

Generates invented agent history (scripts/demo/make_data.py), ingests it into
a throwaway database, serves the dashboard on it, drives the dashboard with
Playwright, and encodes the screenshots into docs/demo.webp (or a GIF with
``--output docs/demo.gif``, which needs ffmpeg).  Real agent history is never
read: every source path points into a temporary directory.  Needs Chromium or
Google Chrome.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parents[2]

# A pointer drawn into the page: screenshots do not include the OS cursor.  It
# hangs off <html> so zooming <body> does not scale or move it.
CURSOR_JS = """
([x, y]) => {
  const c = document.createElement('div');
  c.id = 'demo-cursor';
  c.innerHTML = '<svg width="24" height="24" viewBox="0 0 22 22"><path d="M3 2 L3 18 L7.5 13.8 L10.6 20.5 ' +
    'L13.4 19.2 L10.4 12.6 L16.5 12.6 Z" fill="#111" stroke="#fff" stroke-width="1.6"/></svg>';
  Object.assign(c.style, {position: 'fixed', left: '0', top: '0', zIndex: 2147483647,
    pointerEvents: 'none', transform: `translate(${x}px, ${y}px)`});
  document.documentElement.appendChild(c);
}
"""
FRAME = 0.07  # seconds per motion frame (about 14 fps keeps the GIF small)


def _ease(t: float) -> float:
    return t * t * (3 - 2 * t)


class Recorder:
    """Collect screenshots with display durations and encode them as a GIF.

    Every step animates (pointer glides, smooth scrolls, zooms), and pauses
    stay short so the GIF never sits still for long.
    """

    def __init__(self, page: Page, frames: Path):
        self.page, self.frames = page, frames
        self.shots: list[tuple[Path, float]] = []
        self.cursor = (640.0, 420.0)
        self.zoom = (1.0, 0.0, 0.0, 0.0)  # scale, origin x, origin y, pan

    def shot(self, seconds: float = FRAME) -> None:
        if not self.page.locator("#demo-cursor").count():
            self.page.evaluate(CURSOR_JS, list(self.cursor))
        path = self.frames / f"{len(self.shots):04d}.png"
        self.page.screenshot(path=path)
        self.shots.append((path, seconds))

    def _point(self, x: float, y: float) -> None:
        self.cursor = (x, y)
        self.page.evaluate(
            "([x, y]) => document.getElementById('demo-cursor').style.transform = `translate(${x}px, ${y}px)`",
            [x, y],
        )
        self.page.mouse.move(x, y)  # real hover styles follow the drawn pointer

    def open(self, url: str, seconds: float) -> None:
        self.page.goto(url)
        self.page.wait_for_load_state("networkidle")
        self.shot(seconds)

    def glide(self, x: float, y: float, steps: int = 10) -> None:
        start = self.cursor
        for step in range(1, steps + 1):
            t = _ease(step / steps)
            self._point(start[0] + (x - start[0]) * t, start[1] + (y - start[1]) * t)
            self.shot()

    def center(self, selector: str) -> tuple[float, float]:
        box = self.page.locator(selector).first.bounding_box()
        if box is None:
            raise RuntimeError(f"not visible: {selector}")
        return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

    def click(self, selector: str, seconds: float = 0.4, steps: int = 10) -> None:
        self.glide(*self.center(selector), steps=steps)
        self.shot(0.12)
        self.page.locator(selector).first.click()
        self.page.wait_for_load_state("networkidle")
        self.shot(seconds)

    def scroll(self, target: float, steps: int = 12) -> None:
        start = self.page.evaluate("window.scrollY")
        for step in range(1, steps + 1):
            self.page.evaluate("(y) => window.scrollTo(0, y)", start + (target - start) * _ease(step / steps))
            self.shot()

    def scroll_to(self, selector: str, steps: int = 12, offset: float = 70) -> None:
        self.scroll(self.page.locator(selector).first.evaluate(
            "(el, offset) => el.getBoundingClientRect().top + window.scrollY - offset", offset
        ), steps)

    def _apply_zoom(self, scale: float, ox: float, oy: float, pan: float) -> None:
        self.zoom = (scale, ox, oy, pan)
        self.page.evaluate(
            """([s, ox, oy, pan]) => {
                 const b = document.body;
                 b.style.transformOrigin = `${ox}px ${oy + window.scrollY}px`;
                 b.style.transform = s === 1 && pan === 0 ? '' : `translateY(${-pan}px) scale(${s})`;
               }""",
            [scale, ox, oy, pan],
        )

    def zoom_into(self, left: float, right: float, top: float, steps: int = 18, max_scale: float = 1.9) -> None:
        """Zoom onto screen columns [left, right], with ``top`` near the viewport top."""
        width = self.page.viewport_size["width"]
        scale = min(max_scale, width * 0.96 / (right - left))
        # The origin is the point that stays put; choose it so the region's
        # right edge lands 40px from the viewport edge (any spare width shows
        # the columns to its left) and its top edge 90px below the top.
        ox = (width - 40 - scale * right) / (1 - scale)
        oy = (90 - scale * top) / (1 - scale)
        for step in range(1, steps + 1):
            self._apply_zoom(1 + (scale - 1) * _ease(step / steps), ox, oy, 0.0)
            self.shot()

    def screen(self, x: float, y: float) -> tuple[float, float]:
        """Map an unzoomed screen point to where it appears under the current zoom."""
        scale, ox, oy, pan = self.zoom
        return ox + scale * (x - ox), oy + scale * (y - oy) - pan

    def pan(self, distance: float, steps: int, follow_x: float) -> None:
        """Pan down while the pointer walks down the column at unzoomed x ``follow_x``."""
        scale, ox, oy, start = self.zoom
        x0, y0 = self.cursor
        x1 = self.screen(follow_x, 0)[0]
        for step in range(1, steps + 1):
            t = step / steps
            self._apply_zoom(scale, ox, oy, start + distance * _ease(t))
            # The pointer drifts down the visible rows as they scroll past.
            self._point(x0 + (x1 - x0) * _ease(min(1.0, t * 3)), y0 + 300 * _ease(t))
            self.shot()

    def zoom_out(self, steps: int = 10) -> None:
        scale, ox, oy, pan = self.zoom
        for step in range(1, steps + 1):
            t = _ease(step / steps)
            self._apply_zoom(scale + (1 - scale) * t, ox, oy, pan * (1 - t))
            self.shot()
        self._apply_zoom(1.0, 0.0, 0.0, 0.0)

    def encode(self, output: Path, width: int) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.suffix == ".webp":
            self._encode_webp(output, width)
        else:
            self._encode_gif(output, width)

    def _encode_webp(self, output: Path, width: int) -> None:
        # Animated WebP is several times smaller than GIF at better quality.
        from PIL import Image

        frames = []
        for path, _seconds in self.shots:
            with Image.open(path) as image:
                height = round(image.height * width / image.width)
                frames.append(image.convert("RGB").resize((width, height), Image.LANCZOS))
        frames[0].save(
            output, save_all=True, append_images=frames[1:], loop=0, quality=70, method=6,
            duration=[round(seconds * 1000) for _path, seconds in self.shots],
        )

    def _encode_gif(self, output: Path, width: int) -> None:
        listing = self.frames / "frames.txt"
        lines = [f"file '{path.name}'\nduration {seconds}\n" for path, seconds in self.shots]
        # The concat demuxer ignores the last duration unless the file repeats.
        lines.append(f"file '{self.shots[-1][0].name}'\n")
        listing.write_text("".join(lines), encoding="utf-8")
        scale = f"scale={width}:-1:flags=lanczos"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
             # The UI is flat colors: no dithering keeps frames sharp and small.
             "-vf", f"{scale},split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];"
                    "[b][p]paletteuse=dither=none:diff_mode=rectangle",
             "-loop", "0", str(output)],
            check=True,
        )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _isolated_env(work: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in {"CLAUDE_CONFIG_DIR", "CODEX_HOME"}}
    env.update(
        SPENDA_PRICE_FETCH="0", CURSOR_HOME=str(work / "none"), CURSOR_USER_DIR=str(work / "none"),
        OPENCODE_DB=str(work / "none.sqlite"), SPENDA_DB=str(work / "demo.sqlite"),
    )
    return env


def _spenda(*args: str) -> list[str]:
    return [sys.executable, "-m", "spenda.cli", *args]


def scenario(rec: Recorder, base: str) -> None:
    rec.open(f"{base}/", 0.5)
    rec.glide(*rec.center(".card"))
    rec.scroll_to("h2:text-is('Model composition')", steps=20)
    rec.scroll(0, steps=9)
    rec.click(".source-nav:not(.backend-nav) a:text-is('Claude Code')")
    rec.click(".primary-nav a:text-is('Sessions')")
    # A session priced from list prices shows a cost on every call.
    rec.click("tr:has-text('est.') a[href^='/sessions/']", 0.3)
    rec.scroll_to("h2:text-is('Auditable usage records')", steps=16, offset=20)

    # The per-call record with its cost and action label is the core feature:
    # zoom onto those columns and walk down the calls.
    header = "th:text-is('Call / action')"
    cost = rec.page.locator("th:text-is('Cost')").last.bounding_box()
    calls = rec.page.locator(header).first.bounding_box()
    # Frame the cost and the label text, not the column's empty right side.
    labels = rec.page.locator("td.exact summary").evaluate_all(
        "els => Math.max(...els.slice(0, 20).map(e => e.getBoundingClientRect().right))"
    )
    rec.glide(calls["x"] + 80, calls["y"] + 55)
    rec.zoom_into(cost["x"] - 10, labels + 10, calls["y"] - 8, steps=14, max_scale=2.1)
    rec.shot(0.3)
    rec.pan(380, steps=36, follow_x=calls["x"] + 110)
    rec.shot(0.4)
    rec.zoom_out()

    rec.scroll(0, steps=10)
    rec.click(".primary-nav a:text-is('Models')", 0.5)
    rec.click("#theme-toggle", 0.3)
    rec.click(".primary-nav a:text-is('Overview')", 0.3)
    rec.click(".source-nav:not(.backend-nav) a:text-is('All')", 1.2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, default=ROOT / "docs" / "demo.webp", help=".webp (default) or .gif")
    parser.add_argument("--width", type=int, default=1000, help="output width in pixels")
    parser.add_argument("--keep", action="store_true", help="keep the temporary data and frames")
    args = parser.parse_args()
    if args.output.suffix != ".webp" and not shutil.which("ffmpeg"):
        sys.exit("ffmpeg is required for GIF output")

    work = Path(tempfile.mkdtemp(prefix="spenda-demo-"))
    env = _isolated_env(work)
    sources = ["--codex-home", str(work / "codex"), "--claude-home", str(work / "claude"),
               "--database", str(work / "demo.sqlite"), "--claude-billing", "api"]
    subprocess.run([sys.executable, str(ROOT / "scripts" / "demo" / "make_data.py"), str(work)], check=True)
    subprocess.run(_spenda("ingest", "--all", *sources), env=env, check=True, stdout=subprocess.DEVNULL)

    port = _free_port()
    server = subprocess.Popen(
        _spenda("serve", "--port", str(port), "--interval", "3600", *sources), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"{base}/healthz", timeout=1)
                break
            except OSError:
                time.sleep(0.2)
        frames = work / "frames"
        frames.mkdir()
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception:
                browser = playwright.chromium.launch(channel="chrome")
            page = browser.new_page(viewport={"width": 1280, "height": 760}, color_scheme="light")
            rec = Recorder(page, frames)
            scenario(rec, base)
            browser.close()
        rec.encode(args.output, args.width)
    finally:
        server.terminate()
        server.wait(timeout=10)
        if args.keep:
            print(f"kept {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)
    size = args.output.stat().st_size / 1e6
    print(f"wrote {args.output} ({size:.1f} MB, {sum(s for _, s in rec.shots):.1f}s)")


if __name__ == "__main__":
    main()
