"""check0r3000 — boot-splash and loader animations.

Stdlib-only leaf module (no Textual import): generates the frames for the
startup animation (three variants) and the one-line "hardcore" loader bar
shown while the analyze pipeline runs. Frames are grids of (char, style-key)
cells rendered either to Rich/Textual markup (for the SplashScreen Static and
the status bar) or to raw ANSI (for the standalone terminal demo below).

Demo (plays in the current terminal, no Textual needed):

    python3 scripts/tui_anim.py            # all three variants + loader
    python3 scripts/tui_anim.py 2          # a single variant (1|2|3)
    python3 scripts/tui_anim.py loader     # the pipeline loader bar
    python3 scripts/tui_anim.py --selftest # verify frame generation, exit 0

The TUI picks the startup variant from CHECK0R_SPLASH (1|2|3|random|off).
"""

from __future__ import annotations

import argparse
import math
import random
import sys
import time

# ---------------------------------------------------------------------------
# Canvas + palette
# ---------------------------------------------------------------------------

W, H = 78, 20
LOGO_Y = 6
SUB_Y = 13
SUBTITLE = "» RECHTSSCHUTZ-VERGLEICH «"

Cell = tuple[str, str]  # (char, style key); ("", "") never occurs, blank = (" ", "")
Grid = list[list[Cell]]
_BLANK: Cell = (" ", "")

# style key -> (rich markup style, ANSI SGR params)
STYLES: dict[str, tuple[str, str]] = {
    "logo": ("bold #00d7ff", "1;38;5;45"),
    "logo2": ("bold #ffffff", "1;38;5;231"),
    "hot": ("bold #ff5f00", "1;38;5;202"),
    "fire1": ("#d70000", "38;5;160"),
    "fire2": ("#ff8700", "38;5;208"),
    "fire3": ("#ffd700", "38;5;220"),
    "spark": ("bold #ffff5f", "1;38;5;227"),
    "dim": ("#585858", "38;5;240"),
    "sub": ("#00af87", "38;5;36"),
}

# 5-row block font for exactly the glyphs in "CHECK0R3000"
FONT: dict[str, list[str]] = {
    "C": [" ▄████",
          "██    ",
          "██    ",
          "██    ",
          " ▀████"],
    "H": ["██  ██",
          "██  ██",
          "██████",
          "██  ██",
          "██  ██"],
    "E": ["██████",
          "██    ",
          "█████ ",
          "██    ",
          "██████"],
    "K": ["██  ██",
          "██ ██ ",
          "████  ",
          "██ ██ ",
          "██  ██"],
    "0": [" ████ ",
          "██  ██",
          "██  ██",
          "██  ██",
          " ████ "],
    "R": ["█████ ",
          "██  ██",
          "█████ ",
          "██ ██ ",
          "██  ██"],
    "3": ["█████ ",
          "    ██",
          "  ███ ",
          "    ██",
          "█████ "],
}
LOGO_TEXT = "CHECK0R3000"


def logo_cells() -> list[tuple[int, int, str, int]]:
    """All non-blank logo cells as (x, y, char, glyph_index), centered on the canvas."""
    widths = [len(FONT[c][0]) for c in LOGO_TEXT]
    total = sum(widths) + len(LOGO_TEXT) - 1
    x = (W - total) // 2
    out: list[tuple[int, int, str, int]] = []
    for gi, c in enumerate(LOGO_TEXT):
        for ry, row in enumerate(FONT[c]):
            for rx, ch in enumerate(row):
                if ch != " ":
                    out.append((x + rx, LOGO_Y + ry, ch, gi))
        x += widths[gi] + 1
    return out


def _grid() -> Grid:
    return [[_BLANK] * W for _ in range(H)]


def _put(g: Grid, x: int, y: int, ch: str, style: str) -> None:
    if 0 <= x < W and 0 <= y < H:
        g[y][x] = (ch, style)


def _put_subtitle(g: Grid, reveal: float = 1.0, *, typing: bool = False) -> None:
    """Draw the subtitle; reveal in [0,1] grows center-out (default) or
    left-to-right (typing=True)."""
    x0 = (W - len(SUBTITLE)) // 2
    half = len(SUBTITLE) / 2
    for i, ch in enumerate(SUBTITLE):
        if ch == " ":
            continue
        visible = (i < len(SUBTITLE) * reveal) if typing else (
            abs(i - half) <= half * reveal + 0.5)
        if visible:
            _put(g, x0 + i, SUB_Y, ch, "sub")


def _flash_and_hold(cells: list[tuple[int, int, str, int]], hold: int = 10) -> list[Grid]:
    """Shared finale: white/hot flash, then the settled logo while the subtitle
    fades in center-out."""
    frames: list[Grid] = []
    for style in ("logo2", "hot", "logo2"):
        g = _grid()
        for x, y, ch, _gi in cells:
            _put(g, x, y, ch, style)
        frames.append(g)
    for i in range(hold):
        g = _grid()
        for x, y, ch, _gi in cells:
            _put(g, x, y, ch, "logo")
        _put_subtitle(g, reveal=(i + 1) / hold)
        frames.append(g)
    return frames


# ---------------------------------------------------------------------------
# Variant 1 — Implosion: cells rush in from everywhere and snap into place
# ---------------------------------------------------------------------------

def frames_implosion(rng: random.Random) -> list[Grid]:
    cells = logo_cells()
    dur, max_delay = 14, 10
    flights = []
    for _x, _y, _ch, _gi in cells:
        ang = rng.uniform(0, 2 * math.pi)
        radius = rng.uniform(34, 72)
        flights.append((
            W / 2 + math.cos(ang) * radius,
            H / 2 + math.sin(ang) * radius * 0.55,
            rng.randint(0, max_delay),
        ))
    frames: list[Grid] = []
    for t in range(dur + max_delay + 1):
        g = _grid()
        for (x, y, ch, _gi), (sx, sy, delay) in zip(cells, flights):
            p = (t - delay) / dur
            if p >= 1:
                _put(g, x, y, ch, "hot" if t - delay - dur <= 1 else "logo")
            elif p > 0:
                e = p * p * p  # ease-in: hang back, then snap in explosively
                fx, fy = sx + (x - sx) * e, sy + (y - sy) * e
                glyph = "·" if p < 0.4 else ("░" if p < 0.7 else "▒")
                style = "dim" if p < 0.4 else ("fire1" if p < 0.7 else "fire2")
                _put(g, round(fx), round(fy), glyph, style)
        frames.append(g)
    return frames + _flash_and_hold(cells)


# ---------------------------------------------------------------------------
# Variant 2 — Big Bang: a core detonates, the shockwave reveals the logo
# ---------------------------------------------------------------------------

def frames_bigbang(rng: random.Random) -> list[Grid]:
    cells = logo_cells()
    cx, cy = (W - 1) / 2, (H - 1) / 2

    def dist(x: float, y: float) -> float:
        # y doubled: terminal cells are ~2:1, this keeps the wave visually round
        return math.hypot(x - cx, (y - cy) * 2.1)

    max_r = dist(0, 0) + 4
    frames: list[Grid] = []
    for t in range(6):  # core build-up
        g = _grid()
        r = 2.0 + t * 1.6
        for y in range(H):
            for x in range(W):
                if dist(x, y) <= r:
                    g[y][x] = (rng.choice("▒▓█▓"),
                               rng.choice(("fire3", "fire2", "logo2")))
        frames.append(g)
    debris = [(rng.uniform(0, 2 * math.pi), rng.uniform(0.8, 2.4), rng.choice("✦*·"))
              for _ in range(70)]
    n_wave = 22
    for t in range(n_wave):
        g = _grid()
        r = 10 + (max_r - 10) * (t / (n_wave - 1))
        for x, y, ch, _gi in cells:  # logo revealed behind the wave
            d = dist(x, y)
            if d < r - 1.5:
                _put(g, x, y, ch, "logo" if d < r - 7 else "hot")
        for y in range(H):  # the shockwave ring itself
            row = g[y]
            for x in range(W):
                d = dist(x, y)
                if abs(d - r) < 1.4 and row[x][0] == " ":
                    row[x] = (rng.choice("▓▒▒░"),
                              "fire2" if abs(d - r) < 0.7 else "fire1")
        age = t / n_wave
        if age < 0.75:  # debris flying outward, fading with age
            for ang, speed, glyph in debris:
                px = cx + math.cos(ang) * speed * (t + 4) * 1.9
                py = cy + math.sin(ang) * speed * (t + 4) * 0.75
                xi, yi = round(px), round(py)
                if 0 <= xi < W and 0 <= yi < H and g[yi][xi][0] == " ":
                    style = "spark" if age < 0.25 else ("fire2" if age < 0.5 else "dim")
                    g[yi][xi] = (glyph if age < 0.5 else "·", style)
        frames.append(g)
    return frames + _flash_and_hold(cells)


# ---------------------------------------------------------------------------
# Variant 3 — Slam assembly: letters hammer in one by one, then a fire sweep
# ---------------------------------------------------------------------------

def frames_slam(rng: random.Random) -> list[Grid]:
    cells = logo_cells()
    n_glyphs = max(gi for _x, _y, _ch, gi in cells) + 1
    frames: list[Grid] = []
    placed: list[tuple[int, int, str, int]] = []
    for gi in range(n_glyphs):
        letter = [c for c in cells if c[3] == gi]
        from_top = gi % 2 == 0
        for step in range(3):
            impact = step == 2
            g = _grid()
            dy_shake = (1 if from_top else -1) if impact else 0
            for x, y, ch, _g in placed:  # already-landed letters jolt on impact
                _put(g, x, y + dy_shake, ch, "logo")
            off = (2 - step) * 5 * (-1 if from_top else 1)
            trail = -1 if from_top else 1
            for x, y, ch, _g in letter:
                _put(g, x, y + off, ch, "hot" if impact else "fire2")
                if not impact:  # motion blur behind the falling letter
                    _put(g, x, y + off + trail * 2, "▒", "fire1")
                    _put(g, x, y + off + trail * 4, "░", "dim")
            if impact:  # spark burst around the impact site
                xs = [c[0] for c in letter]
                ys = [c[1] for c in letter]
                for _ in range(10):
                    _put(g, rng.randint(min(xs) - 2, max(xs) + 2),
                         rng.choice((min(ys) - 1, max(ys) + 1)),
                         rng.choice("✦*·"), "spark")
            frames.append(g)
        placed += letter
    n_sweep = 10
    for t in range(n_sweep):  # fire sweep tempers hot metal into the final color
        g = _grid()
        sweep_x = int((t + 1) / n_sweep * (W + 8)) - 4
        for x, y, ch, _gi in cells:
            style = "logo" if x < sweep_x - 3 else ("logo2" if x <= sweep_x else "hot")
            _put(g, x, y, ch, style)
        frames.append(g)
    for i in range(12):  # subtitle types in, then hold
        g = _grid()
        for x, y, ch, _gi in cells:
            _put(g, x, y, ch, "logo")
        _put_subtitle(g, reveal=min(1.0, (i + 1) / 7), typing=True)
        frames.append(g)
    return frames


VARIANTS = {"1": frames_implosion, "2": frames_bigbang, "3": frames_slam}
VARIANT_NAMES = {"1": "Implosion", "2": "Big Bang", "3": "Slam Assembly"}


# ---------------------------------------------------------------------------
# Renderers (shared run-length encoder → Rich markup / raw ANSI)
# ---------------------------------------------------------------------------

def _render_row(row: list[Cell], fmt: str) -> str:
    parts: list[str] = []
    i, n = 0, len(row)
    while i < n:
        style = row[i][1]
        j = i
        while j < n and row[j][1] == style:
            j += 1
        text = "".join(ch for ch, _s in row[i:j])
        escaped = text.replace("[", "\\[")
        if style and text.strip():
            if fmt == "markup":
                parts.append(f"[{STYLES[style][0]}]{escaped}[/]")
            else:
                parts.append(f"\x1b[{STYLES[style][1]}m{text}\x1b[0m")
        else:
            parts.append(escaped if fmt == "markup" else text)
        i = j
    return "".join(parts)


def frame_to_markup(g: Grid) -> str:
    return "\n".join(_render_row(row, "markup") for row in g)


def frame_to_ansi(g: Grid) -> str:
    return "\n".join(_render_row(row, "ansi") for row in g)


def splash_frames(choice: str = "1") -> list[str]:
    """Markup frames for the boot splash. choice: 1|2|3|random (anything else
    falls back to 1). Seeded RNG: the same variant plays the same movie."""
    if choice == "random":
        choice = random.choice("123")
    gen = VARIANTS.get(choice, frames_implosion)
    return [frame_to_markup(g) for g in gen(random.Random(0xC3000))]


# ---------------------------------------------------------------------------
# Loader bar — the one-line "hardcore" animation for pipeline wait times
# ---------------------------------------------------------------------------

LOADER_W = 12


def _loader_cells(tick: int) -> list[Cell]:
    span = LOADER_W - 1
    ph = tick % (2 * span)
    pos = ph if ph <= span else 2 * span - ph  # bouncing fire packet
    cells: list[Cell] = [("⚡", "spark" if tick % 2 == 0 else "fire2")]
    for i in range(LOADER_W):
        d = abs(i - pos)
        if d == 0:
            cells.append(("█", "logo2"))
        elif d == 1:
            cells.append(("▓", "fire3"))
        elif d == 2:
            cells.append(("▒", "fire2"))
        elif d == 3:
            cells.append(("░", "fire1"))
        else:
            cells.append(("░", "dim"))
    cells.append(("⚡", "fire2" if tick % 2 == 0 else "spark"))
    return cells


def loader_markup(tick: int) -> str:
    return _render_row(_loader_cells(tick), "markup")


def loader_ansi(tick: int) -> str:
    return _render_row(_loader_cells(tick), "ansi")


# ---------------------------------------------------------------------------
# Terminal demo player + selftest
# ---------------------------------------------------------------------------

def _play(frames: list[Grid], fps: int, title: str) -> None:
    out = sys.stdout
    out.write("\x1b[?25l\x1b[2J")
    try:
        for g in frames:
            out.write("\x1b[H" + frame_to_ansi(g) + "\n")
            out.write(f"\x1b[2m  {title}\x1b[0m\n")
            out.flush()
            time.sleep(1 / fps)
        time.sleep(0.8)
    finally:
        out.write("\x1b[0m\x1b[?25h")
        out.flush()


def _play_loader(seconds: float = 5.0) -> None:
    out = sys.stdout
    out.write("\x1b[?25l")
    try:
        for tick in range(int(seconds / 0.09)):
            out.write("\r" + loader_ansi(tick)
                      + " \x1b[2m[2/4] Extract  haiku --filter läuft …\x1b[0m ")
            out.flush()
            time.sleep(0.09)
    finally:
        out.write("\x1b[0m\x1b[?25h\n")
        out.flush()


def _selftest() -> int:
    ok = True
    for v, gen in VARIANTS.items():
        frames = splash_frames(v)
        n_lines = {f.count("\n") for f in frames}
        good = len(frames) >= 30 and n_lines == {H - 1} and "█" in frames[-1]
        print(f"variant {v} ({VARIANT_NAMES[v]}): {len(frames)} frames "
              f"{'OK' if good else 'FAIL'}")
        ok &= good
        _ = gen  # exercised via splash_frames
    distinct = {loader_markup(t) for t in range(2 * (LOADER_W - 1))}
    good = len(distinct) >= LOADER_W
    print(f"loader: {len(distinct)} distinct frames {'OK' if good else 'FAIL'}")
    ok &= good
    return 0 if ok else 1


def main() -> None:
    ap = argparse.ArgumentParser(
        description="check0r3000 boot/loader animation demo (stdlib-only, plays "
                    "in the current terminal).")
    ap.add_argument("variant", nargs="?", default="all",
                    choices=["1", "2", "3", "loader", "all"],
                    help="which animation to play (default: all three + loader)")
    ap.add_argument("--fps", type=int, default=22, help="playback speed")
    ap.add_argument("--selftest", action="store_true",
                    help="verify frame generation, then exit")
    args = ap.parse_args()
    if args.selftest:
        sys.exit(_selftest())
    seq = ["1", "2", "3"] if args.variant in ("all",) else (
        [] if args.variant == "loader" else [args.variant])
    for v in seq:
        frames = [g for g in VARIANTS[v](random.Random(0xC3000))]
        _play(frames, args.fps,
              f"V{v} — {VARIANT_NAMES[v]}   (CHECK0R_SPLASH={v})")
    if args.variant in ("all", "loader"):
        print("loader bar (pipeline status line):")
        _play_loader()


if __name__ == "__main__":
    main()
