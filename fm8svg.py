#!/usr/bin/env python3
"""Render an FM8 `.nfm8` patch as a self-contained, FM8-style SVG.

The renderer is deliberately dependency-free.  It combines the parameter
schema (`fm8param`) with the breakpoint-envelope decoder (`fm8env`) and draws:

  * patch metadata and a compact voice summary,
  * the nine operator/filter/pitch envelopes,
  * the full FM matrix (four quadrants per cell, plus OUT and PAN),
  * key-scaling pitch/level/slope curves,
  * filter, noise/saturation, global, LFO, arpeggiator and effect settings.

Usage:
    python fm8svg.py patch.nfm8
    python fm8svg.py patch.nfm8 -o patch.svg
    python fm8svg.py patch.nfm8 --title "Bass patch"

Nothing here needs FM8 or a browser to create the SVG; any SVG viewer can
display the result.  Values come directly from the patch bytes, so no plugin
round-trip is involved.
"""
from __future__ import annotations

import argparse
import math
import os
import re
import struct
import sys
from html import escape
from pathlib import Path

import fm8param
import fm8env

try:  # metadata is optional; fm8param is enough for the synthesis display
    import nfm8
except Exception:  # pragma: no cover - only if the module is moved alone
    nfm8 = None


# ---------------------------------------------------------------------------
# layout and palette
# ---------------------------------------------------------------------------

W = 1640
H = 1460
M = 24

BG = "#0a121d"
PANEL = "#152235"
PANEL_2 = "#1b2c42"
PANEL_3 = "#20344a"
GRID = "#2a4058"
GRID_SOFT = "#21344a"
TEXT = "#e6f1fb"
MUTED = "#8fa6bd"
DIM = "#5d748b"
ACCENT = "#ffb347"
CYAN = "#5fd0ff"
GREEN = "#7fe0a0"
RED = "#ff7f7f"
PURPLE = "#b79cff"
FONT = "Segoe UI, Inter, Arial, Helvetica, sans-serif"

ENV_ORDER = ["A", "B", "C", "D", "E", "F", "X", "Z", "Pitch"]
SRC_ORDER = ["A", "B", "C", "D", "E", "F", "X", "Z", "I"]
DEST_MAIN = ["A", "B", "C", "D", "E", "F", "X", "Z", "I"]
QUADS = ["BL", "BR", "TR", "TL"]

OP_COLORS = {
    "A": "#ffb347",
    "B": "#ffd166",
    "C": "#7fe0a0",
    "D": "#5fd0ff",
    "E": "#b79cff",
    "F": "#ff8fb1",
    "X": "#4fd1c5",
    "Z": "#63b3ed",
    "Pitch": "#f6e05e",
    "I": "#9fb3c8",
}

# Operator waveform menu, in FM8's stored order.  The names are the resource
# strings from the FM8 UI; index 1 really is the short spelling "Parabol".
WAVEFORMS = [
    "Sine", "Parabol", "Triangle", "Square", "Sawtooth", "Soft Square",
    "Soft Tristate", "Short Tristate", "PWM Ramp Mod", "1+3+5 Square",
    "1+2+3 Saw", "1+2", "1+3", "1+4", "1+5", "1+6", "1+7", "1+8",
    "2nd Formant", "3rd Formant", "4th Formant", "5th Formant",
    "6th Formant", "8th Formant", "10th Formant", "TX Wave 2", "TX Wave 3",
    "TX Wave 4", "TX Wave 5", "TX Wave 6", "TX Wave 7", "TX Wave 8",
]

# LFO menu is much smaller than the operator menu.  Unknown values are shown
# numerically rather than guessed.
LFO_WAVES = [
    "Sine", "Triangle", "Square", "Sawtooth", "Soft Square",
    "PWM Ramp Mod", "1+3+5 Square", "1+2+3 Saw", "Sample & Hold",
]

EFFECTS = [
    {"abbr": "Ov", "name": "Overdrive", "on": 30, "mix": 34},
    {"abbr": "TA", "name": "Tube Amp", "on": 35, "mix": 37},
    {"abbr": "Cb", "name": "Cabinet", "on": 38, "mix": None},
    {"abbr": "SQ", "name": "Shelving EQ", "on": 44, "mix": 49},
    {"abbr": "PQ", "name": "Peak EQ", "on": 50, "mix": 57},
    {"abbr": "TW", "name": "Talk Wah", "on": 58, "mix": None},
    {"abbr": "Ph", "name": "Phaser", "on": None, "mix": 70},
    {"abbr": "Fl", "name": "Flanger", "on": None, "mix": 81},
    {"abbr": "Tr", "name": "Tremolo", "on": None, "mix": 84},
    {"abbr": "Rv", "name": "Reverb", "on": 90, "mix": 91},
    {"abbr": "PD", "name": "Psychedelay", "on": 95, "mix": 96},
    {"abbr": "Dl", "name": "Delay", "on": 111, "mix": 112},
]

PARAM_NAMES: dict[int, str] = {}
for _line in open(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "params.txt"),
    encoding="latin-1",
):
    _m = re.match(r"\s*(\d+):\s*(.*?)\s*=", _line)
    if _m:
        PARAM_NAMES[int(_m.group(1))] = _m.group(2)


# ---------------------------------------------------------------------------
# tiny data helpers
# ---------------------------------------------------------------------------

def esc(value) -> str:
    """Escape text/attribute content for XML, including quotes."""
    return escape(str(value), {'"': '&quot;'})


def fmt(value, places=2, dash="—") -> str:
    if value is None:
        return dash
    try:
        value = float(value)
    except (TypeError, ValueError):
        return dash
    if not math.isfinite(value):
        return dash
    if abs(value) >= 1000:
        return f"{value / 1000:.2f}k"
    return f"{value:.{places}f}"


def fmt_time(ms: float) -> str:
    if not math.isfinite(ms):
        return "—"
    if abs(ms) >= 1000:
        return f"{ms / 1000:.2f} s"
    if abs(ms) >= 100:
        return f"{ms:.0f} ms"
    return f"{ms:.1f} ms"


def clamp(value, lo, hi):
    return max(lo, min(hi, value))


def safe_get(patch, index, default=None):
    try:
        return patch.get(index, layout=True)
    except Exception:
        return default


def stored(patch, index, default=None):
    """Stored field value: float for f32, integer for uN/enumN."""
    try:
        meta, off, width = patch._field(index, layout=True)
        raw = bytes(patch.inner[off:off + width])
        if meta["model"] == "f32":
            return struct.unpack("<f", raw)[0]
        return int.from_bytes(raw, "little")
    except Exception:
        return default


def enum_stored(patch, index):
    """(raw index, displayed value list) for an enum parameter, or (None, [])."""
    try:
        meta = patch.schema[index]
        raw = stored(patch, index)
        vals = list(meta.get("values", []))
        return raw, vals
    except Exception:
        return None, []


def enum_value(patch, index, default=None):
    raw, vals = enum_stored(patch, index)
    if raw is None:
        return default
    if 0 <= raw < len(vals):
        return vals[raw]
    return default if default is not None else raw


def on_off(patch, index):
    """Read a 0/1 field; return None if the parameter is unknown."""
    v = stored(patch, index)
    if v is None:
        return None
    try:
        return bool(round(float(v)))
    except Exception:
        return None


def wave_name(index):
    if index is None:
        return "—"
    try:
        i = int(round(float(index)))
    except Exception:
        return "—"
    if 0 <= i < len(WAVEFORMS):
        return WAVEFORMS[i]
    return f"Wave {i}"


def lfo_wave_name(index):
    if index is None:
        return "—"
    try:
        i = int(round(float(index)))
    except Exception:
        return "—"
    if 0 <= i < len(LFO_WAVES):
        return LFO_WAVES[i]
    return f"Wave {i}"


def matrix_value(patch, index, dest):
    """Decode one matrix field into its displayed value.

    A-F/X/Z/I cells are signed bytes (-100..100); OUT is 0..100; PAN uses the
    wrapped 157+199*v pan law and is shown as -100..100 around centre.
    """
    if dest == "O":
        value = stored(patch, index)
        return int(round(value)) if value is not None else 0
    if dest == "P":
        value = safe_get(patch, index, 0.4975)
        if value is None:
            return 0
        return int(round((float(value) - 0.4975) * 200.0))
    raw = stored(patch, index)
    if raw is None:
        return 0
    raw = int(raw)
    if raw >= 128:
        raw -= 256
    return clamp(raw, -100, 100)


def build_matrix(patch):
    """{(source, destination): {quadrant: parameter index}}."""
    cells: dict[tuple[str, str], dict[str, int]] = {}
    for index, name in PARAM_NAMES.items():
        m = re.match(r"FM-Mtrx\s+(\w+)->(\w+)\s+(BL|BR|TR|TL)$", name)
        if not m:
            continue
        src, dest, quad = m.groups()
        cells.setdefault((src, dest), {})[quad] = index
    return cells


def matrix_cell(patch, cells, src, dest):
    indices = cells.get((src, dest))
    if not indices:
        return None
    values = {}
    for q in QUADS:
        idx = indices.get(q)
        values[q] = matrix_value(patch, idx, dest) if idx is not None else 0
    return values


def matrix_route_count(patch, cells):
    count = 0
    for (src, dest), indices in cells.items():
        if src == "I" and dest == "I":
            continue
        for q, idx in indices.items():
            if matrix_value(patch, idx, dest):
                count += 1
                break
    return count


def source_is_active(patch, cells, src):
    out_idx = {"A": 692, "B": 696, "C": 700, "D": 704, "E": 708, "F": 712,
               "X": 716, "Z": 720}.get(src)
    if out_idx is not None:
        out = stored(patch, out_idx, 0) or 0
        if float(out) > 0:
            return True
    for (s, d), indices in cells.items():
        if s != src or d not in DEST_MAIN + ["O"]:
            continue
        for q, idx in indices.items():
            if matrix_value(patch, idx, d):
                return True
    return False


def destination_color(dest):
    return OP_COLORS.get(dest, CYAN)


def patch_metadata(path):
    """Best-effort metadata from nfm8, with a safe fallback."""
    fallback = {
        "name": Path(path).stem,
        "author": "",
        "comment": "",
        "meta": {},
    }
    if nfm8 is None:
        return fallback
    try:
        summary = nfm8.Patch(path).summary()
        fallback.update({
            "name": summary.get("name") or fallback["name"],
            "author": summary.get("author") or "",
            "comment": summary.get("comment") or "",
            "meta": summary.get("meta") or {},
        })
    except Exception:
        pass
    return fallback


# ---------------------------------------------------------------------------
# SVG primitives
# ---------------------------------------------------------------------------

def rect(x, y, w, h, fill, stroke="none", rx=6, opacity=None, extra=""):
    op = f' fill-opacity="{opacity}"' if opacity is not None else ""
    return (f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
            f'rx="{rx}" fill="{fill}" stroke="{stroke}"{op} {extra}/>')


def line(x1, y1, x2, y2, stroke=GRID, width=1, opacity=None, extra=""):
    op = f' stroke-opacity="{opacity}"' if opacity is not None else ""
    return (f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{stroke}" stroke-width="{width}"{op} {extra}/>')


def text(x, y, value, size=10, fill=TEXT, weight="normal", anchor="start",
         opacity=None, extra=""):
    op = f' opacity="{opacity}"' if opacity is not None else ""
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-family="{FONT}" '
            f'font-size="{size}" fill="{fill}" font-weight="{weight}" '
            f'text-anchor="{anchor}"{op} {extra}>{esc(value)}</text>')


def title_node(value):
    return f"<title>{esc(value)}</title>"


def panel(x, y, w, h):
    return rect(x, y, w, h, "url(#panel)", GRID, 10)


def section_title(x, y, label, note=None):
    out = [rect(x, y - 12, 4, 14, ACCENT, rx=2),
           text(x + 12, y, label, 14, TEXT, "600")]
    if note:
        out.append(text(x + 12, y + 15, note, 9, MUTED))
    return "".join(out)


def pill(x, y, label, on=True, color=GREEN, w=None, h=15):
    width = w if w is not None else max(16, 7 * len(str(label)) + 10)
    fill = color if on else PANEL_3
    fg = "#102030" if on else MUTED
    return "".join([
        rect(x, y, width, h, fill, "none", 7, 0.9),
        text(x + width / 2, y + h * 0.70, label, 8.5, fg, "700", "middle"),
    ])


def badge(x, y, label, color, w=24, h=18, fg="#0d1722"):
    return "".join([
        rect(x, y, w, h, color, "none", 4, 0.92),
        text(x + w / 2, y + h * 0.72, label, 10, fg, "700", "middle"),
    ])


def value_row(x, y, label, value, width=240, color=TEXT):
    return "".join([
        text(x, y, label, 9.5, MUTED),
        text(x + width, y, value, 9.5, color, "600", "end"),
    ])


# ---------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------

def render_header(meta, patch, path, cells):
    m = meta.get("meta", {})
    name = meta.get("name") or Path(path).stem
    author = meta.get("author") or "not set"
    comment = meta.get("comment") or ""
    if len(comment) > 80:
        comment = comment[:77].rstrip() + "…"
    version = m.get("\\@verl", "?")
    soundtype = m.get("\\@soundtype", "?")
    colour = m.get("\\@color", "?")
    routes = matrix_route_count(patch, cells)
    envs = patch.envelopes() if hasattr(patch, "envelopes") else {}
    npoints = sum(len(e.time_values_ms) for e in envs.values()) if envs else 0
    active = sum(1 for s in SRC_ORDER if source_is_active(patch, cells, s))

    out = [rect(M, 24, W - 2 * M, 74, "url(#header)", GRID, 12),
           rect(M + 14, 40, 5, 42, ACCENT, rx=2),
           text(M + 30, 46, "FM8 PATCH DATASHEET", 9, MUTED, "700"),
           text(M + 30, 73, name, 28, TEXT, "700"),
           text(M + 31, 91, f"{author}  ·  {version}  ·  {Path(path).name}", 10, MUTED)]
    if comment:
        out.append(text(M + 430, 64, comment, 10.5, "#b9cce0"))
    else:
        out.append(text(M + 430, 64, "no comment", 10.5, DIM))

    stats = [
        ("VERSION", str(version)),
        ("ROUTES", str(routes)),
        ("POINTS", str(npoints)),
        ("ACTIVE", f"{active}/9"),
        ("SOUNDTYPE", str(soundtype)),
        ("COLOR", str(colour)),
    ]
    sx = W - M - 14 - 6 * 98
    for i, (label, value) in enumerate(stats):
        x = sx + i * 98
        out.append(text(x, 47, label, 7.5, DIM, "700"))
        out.append(text(x, 69, value, 16, TEXT, "600"))
    out.append(text(W - M - 14, 88, "matrix + envelopes + synthesis context", 8.5, MUTED, "normal", "end"))
    return "".join(out)


def render_operators(x, y, w, h, patch, cells, envs):
    out = [panel(x, y, w, h),
           section_title(x + 16, y + 24, "OPERATORS & ENVELOPE DESTINATIONS",
                         "BL quadrant shown · * = quadrants differ · P = pitch env · S = sustain · R = release")]
    cols = [x + 16, x + 58, x + 186, x + 252, x + 330, x + 394, x + 510]
    headers = ["OP", "WAVE / SOURCE", "RATIO / CUT", "OFFSET / RES", "OUT", "FLAGS", "ON"]
    for cx, label in zip(cols, headers):
        out.append(text(cx, y + 57, label, 7.5, DIM, "700"))

    row_h = 25
    top = y + 68
    for i, dest in enumerate(ENV_ORDER):
        ry = top + i * row_h
        if i % 2 == 0:
            out.append(rect(x + 10, ry - 10, w - 20, row_h - 2, PANEL_2, "none", 3, 0.55))
        color = destination_color(dest)
        if dest == "Pitch":
            active = bool(envs.get("Pitch") and envs["Pitch"].flags.get("pitch_env"))
        else:
            active = source_is_active(patch, cells, dest)
        out.append(rect(x + 10, ry - 10, 3, row_h - 2, color if active else GRID, rx=1.5))
        out.append(badge(cols[0], ry - 8, dest, color, 26, 17))

        env = envs.get(dest)
        flags = env.flags if env else {}
        p_env = bool(flags.get("pitch_env", False))
        sus = bool(flags.get("loop", False))
        rel = bool(flags.get("release", False))

        if dest in "ABCDEF":
            idx = "ABCDEF".index(dest)
            wave = stored(patch, 728 + idx * 4)
            ratio = stored(patch, 764 + idx * 4)
            offset = stored(patch, 800 + idx * 4)
            out_level = stored(patch, 692 + idx * 4)
            wave_txt = wave_name(wave)
            ratio_txt = fmt(ratio, 3)
            offset_txt = f"{fmt(offset, 2)} Hz"
            out_txt = f"{fmt(out_level, 0)}%"
            tip = (f"Operator {dest}\nwaveform: {wave_txt} ({wave})\n"
                   f"ratio: {ratio_txt}\noffset: {offset_txt}\nout: {out_txt}")
        elif dest == "X":
            mode = stored(patch, 844, 0)
            cutoff = stored(patch, 852, 0)
            res = stored(patch, 864, 0)
            wave_txt = "Filter 1"
            ratio_txt = f"cut {fmt(cutoff, 0)}"
            offset_txt = f"res {fmt(res, 0)}"
            out_txt = "—"
            tip = f"Filter 1\nmode {fmt(mode, 0)} · cutoff {fmt(cutoff, 0)} · res {fmt(res, 0)}"
        elif dest == "Z":
            mode = stored(patch, 848, 0)
            cutoff = stored(patch, 852, 0)
            res = stored(patch, 868, 0)
            wave_txt = "Filter 2"
            ratio_txt = f"cut {fmt(cutoff, 0)}"
            offset_txt = f"res {fmt(res, 0)}"
            out_txt = "—"
            tip = f"Filter 2\nmode {fmt(mode, 0)} · cutoff {fmt(cutoff, 0)} · res {fmt(res, 0)}"
        else:  # Pitch
            transpose = stored(patch, 119, 0)
            amount = stored(patch, 568, 0)
            wave_txt = "Pitch"
            ratio_txt = f"{fmt(transpose, 1)} st"
            offset_txt = f"env {fmt(amount, 0)}"
            out_txt = "—"
            tip = f"Pitch destination\ntranspose: {fmt(transpose, 1)} st\npitch env amount: {fmt(amount, 0)}"

        out.append(text(cols[1], ry, wave_txt, 9.5, TEXT, "500"))
        out.append(text(cols[2], ry, ratio_txt, 9.5, "#cfe3f5"))
        out.append(text(cols[3], ry, offset_txt, 9.5, "#cfe3f5"))
        out.append(text(cols[4], ry, out_txt, 9.5, "#cfe3f5"))
        # flags
        fx = cols[5]
        out.append(pill(fx, ry - 10, "P", p_env, ACCENT, 18, 14))
        out.append(pill(fx + 21, ry - 10, "S", sus, GREEN, 18, 14))
        out.append(pill(fx + 42, ry - 10, "R", rel, CYAN, 18, 14))
        out.append(pill(fx + 66, ry - 10, "TS" if env and env.tempo_sync else "TS",
                        bool(env and env.tempo_sync), PURPLE, 24, 14))
        # active dot
        out.append(f'<circle cx="{cols[6] + 7:.1f}" cy="{ry - 3:.1f}" r="4" '
                   f'fill="{GREEN if active else GRID}"/>')
        out.append(f'<g>{title_node(tip)}{text(cols[6] + 17, ry, "on" if active else "off", 8.5, MUTED)}</g>')
    return "".join(out)


def arrow_svg(x1, y1, x2, y2, color="#cfe8ff"):
    """Thin arrow used for matrix modulation direction."""
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length < 2:
        return ""
    ux, uy = dx / length, dy / length
    head = 6.0
    px, py = x2, y2
    bx, by = x2 - head * ux, y2 - head * uy
    points = (f"{px:.1f},{py:.1f} "
              f"{bx + 3.0 * uy:.1f},{by - 3.0 * ux:.1f} "
              f"{bx - 3.0 * uy:.1f},{by + 3.0 * ux:.1f}")
    return "".join([
        line(x1, y1, x2 - head * 0.8 * ux, y2 - head * 0.8 * uy, color, 1.0, 0.75),
        f'<polygon points="{points}" fill="{color}" fill-opacity="0.75"/>',
    ])


def matrix_route_cell(x, y, size, value, tip):
    """One cell of the FM8 display: a single BL modulation value."""
    out = [rect(x, y, size, size, "#101c2a", GRID_SOFT, 0)]
    if value:
        color = "#2d6a86" if value >= 0 else "#8a4d25"
        frac = clamp(abs(value) / 100.0, 0, 1)
        out.append(rect(x + 2, y + 2, size - 4, size - 4, color, "none", 0,
                        0.05 + 0.25 * frac))
        out.append(text(x + size / 2, y + size / 2 + 3, str(int(value)),
                        8.5, "#f2fbff", "700", "middle"))
    out.append(rect(x, y, size, size, "none", GRID, 1))
    out.append(f"<g>{title_node(tip)}</g>")
    return "".join(out)


def matrix_self_cell(x, y, w, h, value, color, tip):
    """The small self-modulation cell above an operator label."""
    out = [rect(x, y, w, h, "#142235", GRID, 1)]
    if value:
        out.append(rect(x + 1, y + 1, w - 2, h - 2, color, "none", 0, 0.32))
        out.append(text(x + w / 2, y + h / 2 + 2.3, str(int(value)),
                        6.8, "#f2fbff", "700", "middle"))
    out.append(f"<g>{title_node(tip)}</g>")
    return "".join(out)


def matrix_diagonal_svg(x, y, size, tip):
    """Diagonal label-cell background."""
    return "".join([
        rect(x, y, size, size, "#142235", GRID_SOFT, 0),
        rect(x, y, size, size, "none", GRID, 1),
        f"<g>{title_node(tip)}</g>",
    ])


def matrix_output_cell(x, y, w, h, value, tip):
    """One cell of the FM8 output strip; shows the BL value."""
    out = [rect(x, y, w, h, PANEL_2, GRID_SOFT, 0),
           line(x + 2, y + h / 2, x + w - 2, y + h / 2, GRID, 0.7, 0.7)]
    if value:
        color = "#2b7fa4" if value >= 0 else "#a45d2b"
        out.append(rect(x + 2, y + 2, w - 4, h - 4, color, "none", 0,
                        0.12 + 0.55 * clamp(abs(value) / 100.0, 0, 1)))
        out.append(text(x + w / 2, y + h / 2 + 3, str(int(value)),
                        8.5, "#f2fbff", "700", "middle"))
    out.append(f"<g>{title_node(tip)}</g>")
    return "".join(out)


def matrix_pan_cell(x, y, w, h, value, tip):
    on = bool(value)
    color = "#4fc3f7" if value >= 0 else "#ff9f43"
    out = [rect(x, y, w, h, PANEL_2, GRID_SOFT, 0)]
    if on:
        out.append(rect(x + 1, y + 1, w - 2, h - 2, color, "none", 0,
                        0.12 + 0.55 * clamp(abs(value) / 100.0, 0, 1)))
        out.append(text(x + w / 2, y + h - 5, f"{value:+d}", 7.2,
                        "#f2fbff", "700", "middle"))
    out.append(f"<g>{title_node(tip)}</g>")
    return "".join(out)


def render_matrix(x, y, w, h, patch, cells):
    """FM8-style operator matrix.

    Rows are destinations, columns are sources.  Each operator row has a small
    self-modulation cell above its diagonal label.  Arrows point from a route
    value toward its destination operator, exactly as in FM8's matrix.
    """
    out = [panel(x, y, w, h),
           section_title(x + 16, y + 24, "FM MATRIX",
                         "operator colors · self cell above each label · arrows point to destination · later targets below diagonal")]
    size = 38
    gap = 16
    pitch = size + gap
    gx = x + (w - 9 * size) / 2
    gy = y + 58
    route_arrows = []

    # Main grid.  Every row has a 12 px band above it; the diagonal operator's
    # self-modulation cell lives in that band.
    for i, dest in enumerate(SRC_ORDER):
        row_top = gy + i * pitch
        label_y = row_top + gap + 2
        label_cy = label_y + (size - 2) / 2
        self_vals = matrix_cell(patch, cells, dest, dest)
        self_bl = self_vals["BL"] if self_vals else 0
        self_tip = f"{dest} self\n" + (
            "  ".join(f"{q} {self_vals[q]:+d}" for q in QUADS) if self_vals else "no route field")
        out.append(matrix_self_cell(gx + i * size + 1, row_top + 1,
                                    size - 2, gap - 6, self_bl,
                                    destination_color(dest), self_tip))
        for j, src in enumerate(SRC_ORDER):
            vals = matrix_cell(patch, cells, src, dest)
            bl = vals["BL"] if vals else 0
            tip = f"{src} → {dest}\n" + (
                "  ".join(f"{q} {vals[q]:+d}" for q in QUADS) if vals else "no route field")
            cx = gx + j * size + 1
            cy = label_y
            if i == j:
                out.append(matrix_diagonal_svg(cx, cy, size - 2, tip))
            else:
                out.append(matrix_route_cell(cx, cy, size - 2, bl, tip))
                if bl:
                    if j < i:
                        route_arrows.append((cx + size - 2, label_cy,
                                             gx + i * size + 1, label_cy))
                    else:
                        route_arrows.append((cx, label_cy,
                                             gx + (i + 1) * size - 1, label_cy))

    # Modulation arrows, drawn before the labels so the heads sit at the edge.
    for x1, y1, x2, y2 in route_arrows:
        out.append(arrow_svg(x1, y1, x2, y2))

    # Source-output spines.  Every operator column sends straight lines down to
    # nonzero cells below it and to its OUT cell, and up to nonzero cells above
    # it.  Self modulation gets its own loop arrow into the operator.
    oy = gy + 9 * pitch
    for i, src in enumerate(SRC_ORDER):
        xc = gx + i * size + size / 2
        row_top = gy + i * pitch
        label_top = row_top + gap + 2
        label_bottom = label_top + size - 2
        up = []
        down = []
        for r, dest in enumerate(SRC_ORDER):
            if r == i:
                continue
            vals = matrix_cell(patch, cells, src, dest)
            if not (vals and vals["BL"]):
                continue
            target_top = gy + r * pitch + gap + 1
            if r < i:
                up.append(target_top + size - 2)
            else:
                down.append(target_top)
        out_vals = matrix_cell(patch, cells, src, "O")
        if out_vals and out_vals["BL"]:
            down.append(oy + 1)
        color = "#cfe8ff"
        if up:
            out.append(line(xc, label_top, xc, min(up), color, 1.0, 0.45))
        if down:
            out.append(line(xc, label_bottom, xc, max(down), color, 1.0, 0.45))

    # Self modulation: loop out of the self cell and back into the operator,
    # with the arrowhead on the operator edge.
    for i, src in enumerate(SRC_ORDER):
        self_vals = matrix_cell(patch, cells, src, src)
        if not (self_vals and self_vals["BL"]):
            continue
        row_top = gy + i * pitch
        sx = gx + i * size + 1
        sy = row_top + 1
        sw, sh = size - 2, gap - 6
        label_right = gx + (i + 1) * size - 1
        label_mid = row_top + gap + 2 + (size - 2) / 2
        loop_x = label_right + 8
        color = "#cfe8ff"
        label_top = row_top + gap + 2
        out.append(line(sx + sw / 2, sy + sh, sx + sw / 2, label_top,
                        color, 1.0, 0.75))
        out.append(f'<path d="M {sx + sw:.1f} {sy + sh / 2:.1f} '
                   f'H {loop_x:.1f} V {label_mid:.1f} H {label_right - 1:.1f}" '
                   f'fill="none" stroke="{color}" stroke-width="1" '
                   f'stroke-opacity="0.75"/>')
        out.append(f'<polygon points="{label_right - 1:.1f},{label_mid:.1f} '
                   f'{label_right + 5:.1f},{label_mid - 3:.1f} '
                   f'{label_right + 5:.1f},{label_mid + 3:.1f}" '
                   f'fill="{color}" fill-opacity="0.75"/>')

    # Colored operator labels on the diagonal.
    for i, src in enumerate(SRC_ORDER):
        label = "IN" if src == "I" else src
        bw = 26 if label == "IN" else 22
        self_vals = matrix_cell(patch, cells, src, src)
        self_tip = f"{src} self"
        if self_vals:
            self_tip += "\n" + "  ".join(f"{q} {self_vals[q]:+d}" for q in QUADS)
        label_svg = badge(gx + i * size + (size - bw) / 2,
                          gy + i * pitch + gap + (size - 17) / 2,
                          label, destination_color(src), bw, 17,
                          "#102030" if src != "I" else "#e6f1fb")
        out.append(f'<g>{title_node(self_tip)}{label_svg}</g>')

    # Output strip: one BL value per source column.
    oy = gy + 9 * pitch
    out.append(text(gx - 10, oy + size / 2 + 3, "OUT", 8, ACCENT, "700", "end"))
    for j, src in enumerate(SRC_ORDER):
        vals = matrix_cell(patch, cells, src, "O")
        bl = vals["BL"] if vals else 0
        tip = f"{src} → OUT\n" + (
            "  ".join(f"{q} {vals[q]:+d}" for q in QUADS) if vals else "no route field")
        out.append(matrix_output_cell(gx + j * size + 1, oy + 1,
                                      size - 2, size - 2, bl, tip))

    # Slim PAN strip only when the patch uses pan routing.
    pan_cells = []
    for src in SRC_ORDER:
        vals = matrix_cell(patch, cells, src, "P")
        bl = vals["BL"] if vals else 0
        tip = f"{src} → PAN\n" + (
            "  ".join(f"{q} {vals[q]:+d}" for q in QUADS) if vals else "no route field")
        pan_cells.append((bl, tip))
    if any(bl for bl, _ in pan_cells):
        py = oy + size + 4
        out.append(text(gx - 10, py + 14, "PAN", 7, ACCENT, "700", "end"))
        for j, (bl, tip) in enumerate(pan_cells):
            out.append(matrix_pan_cell(gx + j * size + 1, py, size - 2, 20, bl, tip))

    # Legend
    ly = y + h - 4
    out.append(text(x + 16, ly,
                    "above diagonal = affects earlier · below = affects later · "
                    "BL shown · hover for TL/TR/BL/BR · OUT = BL",
                    7.8, MUTED))
    return "".join(out)


def envelope_points(env, x0, y0, w, h, tmax):
    """Sampled SVG points for one envelope; slope is used as an approximate curve."""
    times = env.abs_times_ms
    levels = env.levels
    pts = []
    if not times:
        return pts
    # The first stored time is absolute.  If it is greater than zero, the
    # envelope has an implicit attack segment from (0, 0) to that first point.
    if (math.isfinite(times[0]) and times[0] > 0
            and levels and levels[0] > 0.0005):
        x1 = x0 + clamp(times[0] / tmax, 0, 1) * w
        y1 = y0 + (1.0 - clamp(levels[0], 0, 1)) * h
        slope = clamp(env.slopes[0] if env.slopes else 0.5, 0, 1)
        gamma = 2.0 ** ((slope - 0.5) * 4.0)
        n = max(2, int(28 * abs(x1 - x0) / max(w, 1)) + 2)
        for k in range(0, n + 1):
            f = k / n
            ff = f ** gamma
            pts.append((x0 + (x1 - x0) * f, (y0 + h) + (y1 - (y0 + h)) * ff))
    for i in range(len(times)):
        t = times[i]
        if not math.isfinite(t) or t < 0:
            continue
        x = x0 + clamp(t / tmax, 0, 1) * w
        y = y0 + (1.0 - clamp(levels[i] if i < len(levels) else 0, 0, 1)) * h
        if i == 0:
            if not pts or pts[-1] != (x, y):
                pts.append((x, y))
            continue
        t0 = times[i - 1]
        l0 = levels[i - 1] if i - 1 < len(levels) else 0
        if not math.isfinite(t0) or t0 < 0:
            pts.append((x, y))
            continue
        x_start = x0 + clamp(t0 / tmax, 0, 1) * w
        y_start = y0 + (1.0 - clamp(l0, 0, 1)) * h
        slope = clamp(env.slopes[i - 1] if i - 1 < len(env.slopes) else 0.5, 0, 1)
        # Slope 0.5 is linear; larger values bend the curve the other way.
        gamma = 2.0 ** ((slope - 0.5) * 4.0)
        n = max(2, int(28 * abs(x - x_start) / max(w, 1)) + 2)
        for k in range(1, n + 1):
            f = k / n
            ff = f ** gamma
            pts.append((x_start + (x - x_start) * f,
                        y_start + (y - y_start) * ff))
    return pts


def render_envelope_card(x, y, w, h, dest, env):
    color = destination_color(dest)
    out = [rect(x, y, w, h, PANEL_2, GRID_SOFT, 8),
           rect(x, y, 4, h, color, rx=2),
           badge(x + 12, y + 10, dest, color, 28 if dest != "Pitch" else 48, 18),
           text(x + (48 if dest != "Pitch" else 68), y + 23,
                f"{len(env.time_values_ms)} BP", 8.5, MUTED)]
    valid_times = [t for t in env.abs_times_ms
                   if math.isfinite(t) and 0 <= t <= 1_000_000]
    tmax = max(valid_times) if valid_times else 1000.0
    tmax = max(tmax, 1.0)
    # keep the last point visible even when a sentinel clamps it
    px, py, pw, ph = x + 34, y + 36, w - 46, h - 96
    if env.has_sentinel:
        out.append(pill(x + w - 66, y + 11, "TIME !", True, "#ff9f43", 52, 14))
    out.append(rect(px, py, pw, ph, "#0f1926", "none", 4))
    # grid
    for frac in (0.25, 0.5, 0.75):
        out.append(line(px + frac * pw, py, px + frac * pw, py + ph,
                        GRID, 0.8, 0.55))
    for frac in (0.25, 0.5, 0.75):
        out.append(line(px, py + frac * ph, px + pw, py + frac * ph,
                        GRID, 0.8, 0.55))
    out.append(line(px, py + ph, px + pw, py + ph, GRID, 1))
    # axis text
    out.append(text(px - 5, py + 3, "1", 7.5, MUTED, "normal", "end"))
    out.append(text(px - 5, py + ph / 2 + 3, "0.5", 7.5, MUTED, "normal", "end"))
    out.append(text(px - 5, py + ph + 3, "0", 7.5, MUTED, "normal", "end"))
    # Mark every breakpoint time on the time axis.  Short envelopes get
    # readable horizontal labels on two staggered rows; dense envelopes use
    # rotated labels so every point still gets a tick and a time.
    bps = env.breakpoints
    if len(bps) <= 6:
        entries = []
        for bp in bps:
            bx = px + clamp(bp.abs_time_ms / tmax, 0, 1) * (pw - 6)
            label = fmt_time(bp.abs_time_ms)
            out.append(line(bx, py + ph, bx, py + ph + 3, color, 0.7, 0.8))
            entries.append((bx, label, len(label) * 3.4))
        last = [-1e9, -1e9]
        rows = []
        for bx, label, width in entries:
            left, right = bx - width / 2, bx + width / 2
            row = 0
            while row < 2 and left <= last[row] + 2:
                row += 1
            if row == 2:
                row = 0 if last[0] <= last[1] else 1
            last[row] = right
            rows.append(row)
        for (bx, label, _), row in zip(entries, rows):
            ty = py + ph + 13 + row * 11
            anchor = "middle"
            tx = bx
            if bx < px + 12:
                anchor, tx = "start", px
            elif bx > px + pw - 12:
                anchor, tx = "end", px + pw
            out.append(text(tx, ty, label, 6.2, MUTED, "normal", anchor))
    else:
        for bp in bps:
            bx = px + clamp(bp.abs_time_ms / tmax, 0, 1) * (pw - 6)
            out.append(line(bx, py + ph, bx, py + ph + 3, color, 0.7, 0.8))
            ty = py + ph + 26
            out.append(f'<text x="{bx:.1f}" y="{ty:.1f}" '
                       f'transform="rotate(-90 {bx:.1f} {ty:.1f})" '
                       f'font-family="{FONT}" font-size="5.6" fill="{MUTED}">'
                       f'{esc(fmt_time(bp.abs_time_ms))}</text>')

    pts = envelope_points(env, px, py, pw - 6, ph, tmax)
    if pts:
        d = "M " + " L ".join(f"{a:.2f} {b:.2f}" for a, b in pts)
        fill = f"M {pts[0][0]:.2f} {py + ph:.2f} L " + \
               " L ".join(f"{a:.2f} {b:.2f}" for a, b in pts) + \
               f" L {pts[-1][0]:.2f} {py + ph:.2f} Z"
        out.append(f'<path d="{fill}" fill="{color}" fill-opacity="0.10"/>')
        out.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2.1" '
                   f'stroke-linejoin="round" stroke-linecap="round"/>')
        # breakpoint handles and tooltips
        for i, bp in enumerate(env.breakpoints):
            bx = px + clamp(bp.abs_time_ms / tmax, 0, 1) * (pw - 6)
            by = py + (1.0 - clamp(bp.level, 0, 1)) * ph
            bp_tip = ("BP %d\\nabs %s\\nrel %s\\nlevel %.3f\\nslope %.3f"
                      % (i + 1, fmt_time(bp.abs_time_ms), fmt_time(bp.rel_time_ms),
                         bp.level, bp.slope))
            out.append(f'<g>{title_node(bp_tip)}'
                       f'<circle cx="{bx:.1f}" cy="{by:.1f}" r="2.7" fill="{color}" '
                       f'stroke="#0f1926" stroke-width="1.2"/></g>')
        if len(env.breakpoints) > 1 and env.release:
            lx = px + clamp(env.breakpoints[-1].abs_time_ms / tmax, 0, 1) * (pw - 6)
            out.append(line(lx, py + 2, lx, py + ph, color, 0.8, 0.35,
                            'stroke-dasharray="3,3"'))
    else:
        out.append(text(px + pw / 2, py + ph / 2, "no drawable time data",
                        9, RED, "normal", "middle"))

    # flags / settings
    fy = y + h - 22
    out.append(pill(x + 12, fy, "P", bool(env.flags.get("pitch_env")), ACCENT, 18, 14))
    out.append(pill(x + 33, fy, "S", bool(env.flags.get("loop")), GREEN, 18, 14))
    out.append(pill(x + 54, fy, "R", bool(env.flags.get("release")), CYAN, 18, 14))
    out.append(pill(x + 75, fy, "TS", bool(env.tempo_sync), PURPLE, 24, 14))
    out.append(text(x + 108, fy + 11, f"K {env.key_scaling_display if env.key_scaling_display is not None else '—'}",
                    8, MUTED))
    out.append(text(x + 148, fy + 11, f"V {env.velocity_scaling_display if env.velocity_scaling_display is not None else '—'}",
                    8, MUTED))
    out.append(text(x + w - 10, fy + 11, env.mode, 8, DIM, "normal", "end"))
    env_tip = ("%s envelope\\n%d breakpoints\\ntempo sync %s\\nkey scale %s\\n"
               "velocity scale %s" % (dest, len(env.time_values_ms), env.tempo_sync,
                                      env.key_scaling_display, env.velocity_scaling_display))
    out.append(f'<g>{title_node(env_tip)}</g>')
    return "".join(out)


def render_envelopes(x, y, w, h, patch, envs):
    out = [panel(x, y, w, h),
           section_title(x + 16, y + 24, "BREAKPOINT ENVELOPES",
                         "absolute time · levels 0..1 · P/S/R/TS flags · K/V scaling amounts")]
    card_w = (w - 24 - 20) / 3
    card_h = 220
    gap = 10
    top = y + 42
    for i, dest in enumerate(ENV_ORDER):
        col, row = i % 3, i // 3
        cx = x + 12 + col * (card_w + gap)
        cy = top + row * (card_h + gap)
        env = envs.get(dest)
        if env is None:
            out.append(rect(cx, cy, card_w, card_h, PANEL_2, GRID_SOFT, 8))
            out.append(badge(cx + 12, cy + 10, dest, destination_color(dest), 28, 18))
            out.append(text(cx + 50, cy + 24, "not decoded", 9, RED))
        else:
            out.append(render_envelope_card(cx, cy, card_w, card_h, dest, env))
    return "".join(out)


def _polyline(values, x, y, w, h, vmin, vmax, color):
    if not values:
        return ""
    vmin, vmax = min(vmin, vmax), max(vmin, vmax)
    span = vmax - vmin or 1.0
    pts = []
    n = len(values)
    for i, value in enumerate(values):
        px = x + (i / max(n - 1, 1)) * w
        py = y + (1.0 - clamp((value - vmin) / span, 0, 1)) * h
        pts.append((px, py))
    d = "M " + " L ".join(f"{a:.1f} {b:.1f}" for a, b in pts)
    out = [f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.6" '
           f'stroke-linejoin="round" stroke-linecap="round"/>']
    if n <= 10:
        for a, b in pts:
            out.append(f'<circle cx="{a:.1f}" cy="{b:.1f}" r="1.8" fill="{color}"/>')
    return "".join(out)


def render_keyscales(x, y, w, h, patch):
    try:
        keyscales = patch.keyscales()
    except Exception:
        keyscales = {}
    out = [panel(x, y, w, h),
           section_title(x + 16, y + 24, "KEY SCALING CURVES",
                         "x: MIDI key 0..127 · pitch 0..127 · level dB · slope 0..1 · ! = out of range")]
    if not keyscales:
        out.append(text(x + 16, y + 60, "key-scaling data could not be decoded", 10, RED))
        return "".join(out)
    rows = [("pitch", "PITCH", CYAN, 0, 127),
            ("level", "LEVEL dB", ACCENT, -80, 0),
            ("slope", "SLOPE", GREEN, 0, 1)]
    label_w = 78
    col_w = (w - label_w - 28) / 9
    top = y + 60
    row_h = 25
    for j, dest in enumerate(ENV_ORDER):
        cx = x + label_w + 14 + j * col_w
        out.append(text(cx + col_w / 2, top - 5, dest, 8, "#c9dced", "700", "middle"))
    for r, (run, label, color, vmin, vmax) in enumerate(rows):
        ry = top + r * row_h
        out.append(text(x + 16, ry + 14, label, 8, color, "700"))
        for j, dest in enumerate(ENV_ORDER):
            cx = x + label_w + 14 + j * col_w
            values = keyscales.get(run, {}).get(dest)
            if not values:
                out.append(text(cx + col_w / 2, ry + 14, "—", 8, DIM, "normal", "middle"))
                continue
            val_txt = " ".join(f"{fmt(v, 1)}" for v in values[:8])
            valid = all(vmin - 0.5 <= v <= vmax + 0.5 for v in values)
            mini_w = col_w - 14
            mini_h = 17
            out.append(rect(cx, ry + 2, mini_w, mini_h, "#0f1926", "none", 2))
            if valid:
                out.append(_polyline(values, cx + 4, ry + 4, mini_w - 8, mini_h - 4,
                                     vmin, vmax, color))
            else:
                out.append(rect(cx, ry + 2, mini_w, mini_h, "#3a2a1d", "none", 2))
                out.append(text(cx + mini_w / 2, ry + 14, "!", 9, "#ff9f43", "700", "middle"))
            ks_tip = "%s keyscale %s\\n%s%s" % (
                dest, run, val_txt, "" if valid else "\\n(out of range / sentinel)")
            out.append(f'<g>{title_node(ks_tip)}</g>')
    return "".join(out)


def render_voice(x, y, w, h, patch, cells):
    out = [rect(x, y, w, h, PANEL_2, GRID_SOFT, 7),
           text(x + 12, y + 20, "FILTER / NOISE / SATURATION / GLOBAL", 10, TEXT, "700")]
    cutoff = stored(patch, 852, 0)
    mode1 = stored(patch, 844, 0)
    mode2 = stored(patch, 848, 0)
    res1 = stored(patch, 864, 0)
    res2 = stored(patch, 868, 0)
    env_amt = stored(patch, 860, 0)
    serial = stored(patch, 836, 0)
    mix = stored(patch, 840, 0)
    noise_cut = stored(patch, 872, 0)
    noise_res = stored(patch, 876, 0)
    noise_amt = stored(patch, 880, 0)
    sat_level = stored(patch, 888, 0)
    sat_asym = stored(patch, 892, 0)
    master = stored(patch, 116, 0)
    input_v = stored(patch, 117, 0)
    transpose = stored(patch, 119, 0)
    detune = stored(patch, 121, 0)
    mono = stored(patch, 122, 0)
    porta = stored(patch, 125, 0)
    glide = stored(patch, 127, 0)

    left = x + 12
    right = x + w / 2 + 8
    yy = y + 42
    rows_l = [
        ("Filter cutoff", fmt(cutoff, 0)),
        ("Filter 1 res", fmt(res1, 0)),
        ("Filter 2 res", fmt(res2, 0)),
        ("Filter 1 mode", fmt(mode1, 0)),
        ("Filter 2 mode", fmt(mode2, 0)),
        ("Filter env", f"{fmt(env_amt, 0)}"),
        ("Serial / mix", f"{fmt(serial, 0)} / {fmt(mix, 0)}"),
        ("Noise cut / res", f"{fmt(noise_cut, 0)} / {fmt(noise_res, 0)}"),
        ("Noise amount", fmt(noise_amt, 0)),
        ("Sat level / asym", f"{fmt(sat_level, 0)} / {fmt(sat_asym, 0)}"),
    ]
    for i, (label, value) in enumerate(rows_l):
        value_row(left, yy + i * 21, label, value, w / 2 - 24)
    rows_r = [
        ("Master volume", f"{fmt(master, 0)}%"),
        ("Input volume", f"{fmt(input_v, 0)}%"),
        ("Pitch transpose", f"{fmt(transpose, 1)} st"),
        ("Unison detune", fmt(detune, 0)),
        ("Mono", "yes" if mono else "no"),
        ("Portamento", "on" if porta else "off"),
        ("Glide time", fmt(glide, 0)),
    ]
    for i, (label, value) in enumerate(rows_r):
        value_row(right, yy + i * 21, label, value, w / 2 - 24)
    routes = []
    for (src, dest), indices in cells.items():
        if dest not in DEST_MAIN + ["O"]:
            continue
        vals = []
        for q in QUADS:
            idx = indices.get(q)
            if idx is not None:
                vals.append(matrix_value(patch, idx, dest))
        if vals:
            best = max(vals, key=lambda v: abs(v))
            if best:
                routes.append((src, dest, best))
    routes.sort(key=lambda item: -abs(item[2]))
    ry = y + h - 56
    out.append(text(x + 12, ry, "TOP ROUTES", 7.5, DIM, "700"))
    if not routes:
        out.append(text(x + 12, ry + 14, "no active matrix routes", 8.5, DIM))
    else:
        for i, (src, dest, value) in enumerate(routes[:3]):
            out.append(text(x + 12, ry + 14 + i * 12,
                            f"{src} → {dest}  {value:+d}", 8.5,
                            "#cfe3f5" if value >= 0 else "#ffb07a"))
    return "".join(out)


def render_lfo_arp(x, y, w, h, patch):
    out = [rect(x, y, w, h, PANEL_2, GRID_SOFT, 7),
           text(x + 12, y + 20, "LFO / ARPEGGIATOR", 10, TEXT, "700")]
    yy = y + 42
    for n in (1, 2):
        base = 1078 if n == 1 else 1086
        wave = stored(patch, base + 1, 0)
        rate = stored(patch, base + 2, 0)
        delay = stored(patch, base + 3, 0)
        free = stored(patch, base + 4, 0)
        sync = stored(patch, base + 5, 0)
        vel = stored(patch, base + 6, 0)
        key = stored(patch, base + 7, 0)
        out.append(text(x + 12, yy, f"LFO {n}", 9, PURPLE, "700"))
        out.append(text(x + 64, yy, f"{lfo_wave_name(wave)}", 9, TEXT))
        out.append(text(x + 174, yy, f"rate {fmt(rate, 0)}", 8.5, MUTED))
        out.append(text(x + 246, yy, f"delay {fmt(delay, 0)}", 8.5, MUTED))
        out.append(text(x + 328, yy, f"sync {'y' if sync else 'n'}  free {'y' if free else 'n'}", 8, MUTED))
        out.append(text(x + 420, yy, f"K {fmt(key, 0)} V {fmt(vel, 0)}", 8, DIM, "normal", "end"))
        yy += 20
    yy += 4
    out.append(line(x + 12, yy - 8, x + w - 12, yy - 8, GRID_SOFT))
    arp_on = on_off(patch, 136)
    steps = enum_value(patch, 145, "—")
    mode = stored(patch, 146, 0)
    length = enum_value(patch, 137, "—")
    shuffle = safe_get(patch, 141, None)
    hold = on_off(patch, 142)
    out.append(text(x + 12, yy + 8, "ARP", 9, ACCENT, "700"))
    out.append(pill(x + 54, yy - 4, "ON" if arp_on else "OFF", bool(arp_on), GREEN, 36, 14))
    out.append(text(x + 100, yy + 8, f"steps {steps}  mode {fmt(mode, 0)}", 8.5, TEXT))
    out.append(text(x + 230, yy + 8, f"length {length}  shuffle {fmt(shuffle, 2)}", 8.5, MUTED))
    out.append(text(x + w - 12, yy + 8, f"hold {'y' if hold else 'n'}", 8, DIM, "normal", "end"))
    yy += 24
    out.append(text(x + 12, yy + 8, "Transpose and LFO rates are raw patch values (0..100 scale).",
                    7.5, DIM))
    return "".join(out)


def render_effects(x, y, w, h, patch):
    out = [rect(x, y, w, h, PANEL_2, GRID_SOFT, 7),
           text(x + 12, y + 20, "EFFECT RACK", 10, TEXT, "700")]
    amts = [stored(patch, i, 0) for i in (26, 27, 28, 29)]
    amt = sum(v for v in amts if v is not None) / 4.0
    out.append(text(x + w - 12, y + 20, f"FX amount {fmt(amt, 0)}%", 8.5, MUTED, "normal", "end"))
    col_w = (w - 24) / 2
    row_h = 37
    top = y + 36
    for i, fx in enumerate(EFFECTS):
        col = i // 6
        row = i % 6
        bx = x + 12 + col * col_w
        by = top + row * row_h
        known = fx["on"] is not None and on_off(patch, fx["on"]) is not None
        on = on_off(patch, fx["on"]) if known else None
        if on is True:
            out.append(rect(bx, by - 1, col_w - 12, row_h - 4, "#1b3550", "none", 4, 0.7))
        out.append(badge(bx, by, fx["abbr"], ACCENT if on else GRID, 25, 16))
        out.append(text(bx + 33, by + 12, fx["name"], 9, TEXT if on else MUTED, "500"))
        if known:
            out.append(pill(bx + col_w - 110, by, "ON" if on else "OFF",
                            bool(on), GREEN, 30, 14))
        else:
            out.append(pill(bx + col_w - 110, by, "?", False, MUTED, 18, 14))
        if fx["mix"] is not None:
            mix = stored(patch, fx["mix"], 0)
            if mix is not None and mix <= 1.0:
                mix *= 100.0
            out.append(text(bx + col_w - 16, by + 12, f"{fmt(mix, 0)}%", 8.5,
                            "#cfe3f5", "600", "end"))
    out.append(text(x + 12, y + h - 10,
                    "Phaser / Flanger / Tremolo on/off fields are not in the measured schema; shown as ?",
                    7.5, DIM))
    return "".join(out)


def render_footer(path):
    return "".join([
        line(M, H - 24, W - M, H - 24, GRID_SOFT, 0.8),
        text(M, H - 9, "Generated by nfm8-format · fm8svg.py", 8, DIM),
        text(W - M, H - 9, str(path), 8, DIM, "normal", "end"),
    ])


# ---------------------------------------------------------------------------
# document
# ---------------------------------------------------------------------------

def render(path, title=None):
    patch = fm8param.Patch(path)
    meta = patch_metadata(path)
    if title:
        meta["name"] = title
    envs = patch.envelopes()
    cells = build_matrix(patch)

    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" role="img">',
        f"<title>{esc(meta.get('name') or Path(path).stem)} — FM8 patch</title>",
        f"<desc>{esc('FM8 patch visualization: operator matrix, breakpoint envelopes, key scaling and synthesis settings.')}</desc>",
        "<defs>",
        '<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="#0d1826"/><stop offset="0.55" stop-color="{BG}"/>'
        f'<stop offset="1" stop-color="#08101a"/></linearGradient>',
        '<linearGradient id="header" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="#1d3148"/><stop offset="1" stop-color="#101d2c"/></linearGradient>',
        '<linearGradient id="panel" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="{PANEL}"/><stop offset="1" stop-color="#111d2c"/></linearGradient>',
        '<filter id="shadow" x="-20%" y="-20%" width="140%" height="140%">'
        '<feDropShadow dx="0" dy="3" stdDeviation="6" flood-color="#000" flood-opacity="0.35"/>'
        '</filter>',
        "<style><![CDATA[",
        "  text { paint-order: stroke; stroke: transparent; }",
        "  .mono { font-variant-numeric: tabular-nums; }",
        "]]></style>",
        "</defs>",
        f'<rect width="{W}" height="{H}" fill="url(#bg)"/>',
        render_header(meta, patch, path, cells),
        render_operators(M, 112, 620, 300, patch, cells, envs),
        render_matrix(M, 426, 620, 620, patch, cells),
        render_envelopes(660, 112, 956, 726, patch, envs),
        render_keyscales(660, 854, 956, 140, patch),
        # bottom full-width context
        panel(M, 1066, W - 2 * M, 360),
        section_title(M + 16, 1090, "VOICE · MODULATION · EFFECTS",
                      "right-hand context for the matrix and envelopes"),
        render_voice(M + 12, 1106, 500, 300, patch, cells),
        render_lfo_arp(M + 524, 1106, 420, 300, patch),
        render_effects(M + 956, 1106, 612, 300, patch),
        render_footer(path),
        "</svg>",
    ]
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Render an FM8 .nfm8 patch as a self-contained FM8-style SVG.")
    ap.add_argument("patch", help="input .nfm8 patch")
    ap.add_argument("-o", "--output", help="output SVG (default: next to the patch)")
    ap.add_argument("--title", help="override the displayed patch name")
    args = ap.parse_args(argv)

    out = Path(args.output) if args.output else Path(args.patch).with_suffix(".svg")
    try:
        svg = render(args.patch, title=args.title)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    out.write_text(svg, encoding="utf-8")
    print(f"wrote {out} ({len(svg):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
