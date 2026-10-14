#!/usr/bin/env python3
"""Scope Trace Digitizer

Turns Keysight DSO-X screen captures into x,y CSV files, and CSV files into graphs.

    python3 scope_digitizer.py FOLDER_OR_IMAGES            CSVs, check pictures and graphs in one go
    python3 scope_digitizer.py convert FOLDER_OR_IMAGES    images -> CSV only
    python3 scope_digitizer.py plot FOLDER_OR_CSVS         CSV -> graph pictures (add --show for zoomable windows)

Each trace becomes its own file, e.g. NAME_ch1_yellow.csv, holding two columns: x (seconds
from the trigger) and y (volts). The scales and measurements read from each screenshot go
into summary.csv, one row per image, together with a status and anything worth checking.

Needs numpy, Pillow and matplotlib. Text is read with the Mac's built-in recogniser.
"""
import argparse
import csv
import io
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

# A Keysight InfiniiVision capture is 800 px wide; its grid is 10 x 8 divisions.
DIVX, DIVY, COLS, ROWS = 64, 50, 640, 400
# Trace kinds: 1 to 4 are the channels, MATH is a math (function) waveform.
MATH, ORANGE = 5, 9
TRACES = (1, 2, 3, 4, MATH)
COLOURS = {1: "yellow", 2: "green", 3: "blue", 4: "pink", MATH: "purple"}
MARK_BRIGHT = 170


def trace_label(n):
    return "Math" if n == MATH else f"CH{n}"


def trace_key(n):
    return "math" if n == MATH else f"ch{n}"


def trace_file(stem, ch):
    return f"{stem}_{trace_key(ch.n)}_{ch.colour}.csv"
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp"}
# Scope channel colours stepped to read on a light chart surface (checked for colour-blind separation).
SERIES = {"yellow": "#eda100", "green": "#008300", "blue": "#2a78d6", "pink": "#e87ba4", "purple": "#4a3aa7"}
SLOTS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]


# ---------------------------------------------------------------- numbers and units

PREFIX = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3, "": 1.0, "k": 1e3, "M": 1e6, "G": 1e9}
UNIT = {"hz": "Hz", "v": "V", "a": "A", "s": "s", "w": "W", "%": "%"}
NUM_RE = r"([<>]?)\s*([-+]?(?:\d+(?:\.\d+)?|\.\d+))\s*([pnumkKMG]?)(Hz|HZ|hz|[VvYy¥]+|[AasSWw%])"
# Look-alike letters a recogniser may return from other alphabets (it does this even when told to read English).
LOOKALIKES = str.maketrans("АВЕЗКМНОРСТУХЧЬІЅабезкморсухчьіѕμµ₩", "ABE3KMHOPCTYX4bISa6e3kmopcyx4bisuuV")


def tidy(text):
    """Undo a text reader's usual slips on this font: 5 as S, 6 as b, 0 as O, 1 as l, stray quote marks."""
    s = str(text or "").translate(LOOKALIKES)
    s = re.sub(r"[−–—]", "-", s)
    s = re.sub(r"[\\`'\"]", "", s)
    s = re.sub(r"S(?=\d)", "5", s)
    s = re.sub(r"\.[Oo]", ".0", s)
    s = re.sub(r"\b[Oo]\.", "0.", s)
    s = re.sub(r"(\d)hs", r"\1ns", s)  # "10.00ns" read as "10.00hs"
    s = re.sub(r"([\d.])b(?=[\dA-Za-z])", r"\g<1>6", s)
    for _ in range(2):
        s = re.sub(r"(\d)[Oo]", r"\g<1>0", s)
        s = re.sub(r"[Oo](\d)", r"0\1", s)
        s = re.sub(r"(\d)[lI|]", r"\g<1>1", s)
        s = re.sub(r"[lI|](\d)", r"1\1", s)
    return s


def _quantity(m, kind):
    # A run of V/Y shapes counts as volts: readers often return the scope's "V" as "Y".
    unit = "V" if re.fullmatch(r"[VvYy¥]+", m.group(4)) else UNIT[m.group(4).lower()]
    pre = "k" if m.group(3) == "K" else m.group(3)
    if pre == "M" and unit not in ("Hz", "W"):
        pre = "m"
    if kind == "V" and unit not in ("V", "A"):
        return None
    if kind in ("s", "Hz") and unit != kind:
        return None
    return {"value": float(m.group(2)) * PREFIX[pre], "mant": abs(float(m.group(2))), "unit": unit,
            "shown": f"{m.group(1)}{m.group(2)} {pre}{unit}"}


def parse_quantities(text, kind=None):
    """Every quantity found in the text. kind: 'V' (volts or amps), 's', 'Hz', or None for any unit."""
    found = (_quantity(m, kind) for m in re.finditer(NUM_RE, tidy(text)))
    return [q for q in found if q]


def parse_whole(text, kind=None):
    """Strict form used for voting: the whole text, spaces removed, must be exactly one quantity."""
    m = re.fullmatch(NUM_RE + r"[\\/#!|f]*", re.sub(r"\s+", "", tidy(text)))
    return _quantity(m, kind) if m else None


def parse_typed(text, kind):
    """For values a person typed: a bare number takes the expected unit."""
    qs = parse_quantities(text, kind)
    if qs:
        return qs[0]
    t = re.sub(r"\s+", "", tidy(text))
    if re.fullmatch(r"[-+]?(\d+(\.\d+)?|\.\d+)", t):
        return {"value": float(t), "mant": abs(float(t)), "unit": kind, "shown": t}
    return None


def fmt_si(value, unit, digits=5, plain=False):
    if value is None or not math.isfinite(value):
        return ""
    if value == 0:
        return f"0 {unit}"
    e = max(-12, min(9, math.floor(math.log10(abs(value)) / 3 + 1e-9) * 3))
    if not unit or unit == "%":
        e = 0
    pre = {-12: "p", -9: "n", -6: "u" if plain else "µ", -3: "m", 0: "", 3: "k", 6: "M", 9: "G"}[e]
    return f"{float(f'{value / 10 ** e:.{digits}g}'):g} {pre}{unit}"


def is125(v):
    if not v or v <= 0:
        return False
    m = v / 10 ** math.floor(math.log10(v) + 1e-9)
    return any(abs(m - t) < 0.011 * t for t in (1, 2, 5, 10))


def vote(items):
    """items: [(key, payload) or None]. The most common key wins; 'sure' needs at least 60% of the readable ones."""
    tally = {}
    for it in items:
        if it is not None:
            tally.setdefault(it[0], [0, it[1]])[0] += 1
    if not tally:
        return None
    valid = sum(n for n, _ in tally.values())
    n, payload = max(tally.values(), key=lambda t: t[0])
    return SimpleNamespace(item=payload, n=n, valid=valid, sure=n >= 2 and n > valid - n and n >= 0.6 * valid)


MEAS_NAMES = ["Freq", "Period", "Rise", "Fall", "Max", "Min", "Pk-Pk", "Ampl", "Top", "Base", "Over", "Pre", "Avg", "RMS",
              "DC RMS", "AC RMS", "+Width", "-Width", "Duty", "+Duty", "-Duty", "Delay", "Phase", "Std Dev", "Rate", "Area",
              "Counter", "X at Max", "X at Min", "Bit Rate", "Burst Width"]


def _edit_distance(a, b):
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, cb in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (ca != cb))
    return row[-1]


def parse_meas_label(text):
    """'Freq(1):' -> ('Freq', '1'). A name one letter off a real measurement name is pulled back to it.
    The source is '?' when only the bare name was readable."""
    t = tidy(text).strip()
    m = re.fullmatch(r"(.+?)\s*[(\[{]\s*([^)\]}]{1,6}?)\s*[)\]}]?\s*[:;.,]?", t)
    if m and re.search(r"[A-Za-z]", m.group(1)):
        name = re.sub(r"\s+", " ", m.group(1)).strip()
        best = min(MEAS_NAMES, key=lambda k: _edit_distance(name.lower(), k.lower()))
        dist = _edit_distance(name.lower(), best.lower())
        if dist == 0 or (dist == 1 and len(name) >= 4):
            name = best
        elif re.fullmatch(r"[xy][12]", name, re.I):  # cursor readouts
            name = name.upper()
        return name, re.sub(r"[Oo]", "0", re.sub(r"[lI|]", "1", m.group(2))).strip()
    # Cursor panel: the reader returns the delta sign as "&", "A", "B" or a Cyrillic letter.
    delta = "[&дДABΔ∆△]"
    m = re.fullmatch(rf"(1/)?{delta}([XY])(/{delta}X)?\s*[:;]?", t)
    if m:
        return ("1/dX" if m.group(1) else "dY/dX" if m.group(3) else "d" + m.group(2)), ""
    bare = re.sub(r"[:;.,\s]+$", "", t)
    for known in sorted(MEAS_NAMES, key=len, reverse=True):
        if bare.lower().startswith(known.lower()) and len(bare) - len(known) <= 2:
            src = re.sub(r"[iIl|]", "1", bare[len(known):])
            return known, src if src in ("1", "2", "3", "4") else "?"
    return None


def meas_label(m):
    return f"{m['name']}({m['src']})" if m["src"] else m["name"]


# ---------------------------------------------------------------- reading the pixels

def classify(rgb, loose=False):
    """0 = nothing, 1..4 = channel colour, MATH = the paler purple of a math waveform,
    ORANGE = trigger and cursor markers.
    'loose' is for the thin marker glyphs, which image compression washes out more than the traces."""
    a = rgb.astype(np.int16)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    mx, mn = a.max(-1), a.min(-1)
    d = mx - mn
    ok = (mx >= 96) & (d >= mx * (0.16 if loose else 0.35))
    dd = np.where(d == 0, 1, d).astype(np.float32)
    h = np.where(mx == r, (g - b) / dd, np.where(mx == g, (b - r) / dd + 2, (r - g) / dd + 4)) * 60
    h = np.where(h < 0, h + 360, h)
    cls = np.zeros(mx.shape, np.uint8)
    for value, lo, hi in ((1, 48, 78), (2, 78, 165.001), (3, 190, 265.001), (MATH, 270, 312), (4, 312, 350.001), (ORANGE, 15, 48)):
        cls[ok & (h >= lo) & (h < hi)] = value
    return cls


def _thin_peaks(frac, level, reach):
    """Indices where a white line crosses: high coverage on the line, low a few pixels to either side."""
    hits = [int(i) for i in np.nonzero(frac > level)[0]
            if frac[max(0, i - reach)] < level * 0.6 and frac[min(len(frac) - 1, i + reach)] < level * 0.6]
    groups = []
    for i in hits:
        if groups and i - groups[-1][-1] <= 2:
            groups[-1].append(i)
        else:
            groups.append([i])
    return [g[len(g) // 2] for g in groups]


def locate_grid(rgb):
    """Find the white frame around the grid in an image of any size, even with a menu covering one side.
    Returns (left or None, right, top, bottom) or None. The frame is 641 px wide and 399 px tall on the scope."""
    H, W = rgb.shape[:2]
    white = rgb.min(-1) > 120
    reach = max(3, round(W * 0.006))
    col_frac = white[int(H * 0.25):int(H * 0.8)].mean(0)
    cols = _thin_peaks(col_frac, 0.45, reach)
    best = None
    for r in cols:
        if r < W * 0.45:
            continue
        row_frac = white[:, max(0, r - int(W * 0.3)):max(1, r - 3)].mean(1)
        rows = _thin_peaks(row_frac, 0.6, reach)
        for t in rows:
            for b in rows:
                h = b - t
                if h < H * 0.45:
                    continue
                w = h * 641 / 399
                want = r - w
                if want < -2:
                    continue
                near = [c for c in cols if abs(c - want) <= max(2, 0.012 * w)]
                l = min(near, key=lambda c: abs(c - want)) if near else None
                score = col_frac[r] + row_frac[t] + row_frac[b] + (col_frac[l] if l is not None else 0)
                if best is None or score > best[0]:
                    best = (score, l, r, t, b)
    return best[1:] if best else None


def normalise(image):
    """Bring a capture to the scope's own scale, whatever size it was saved or resized to.
    Returns (image, geometry); geometry.found is False when no grid could be located."""
    box = locate_grid(np.asarray(image))
    if box:
        l, r, t, b = box
        scale = 641 / (r - l) if l is not None else 399 / (b - t)
        if abs(scale - 1) > 0.004:
            image = image.resize((round(image.width * scale), round(image.height * scale)), Image.LANCZOS)
            box = locate_grid(np.asarray(image)) or tuple(None if v is None else round(v * scale) for v in box)
            l, r, t, b = box
        inside = np.asarray(image)[max(0, b - ROWS + 2):b, max(0, r - COLS + 1):r]
        if inside.size and (inside.max(-1) < 90).mean() >= 0.5:  # a photo of something else is not this dark
            return image, SimpleNamespace(gx0=r - COLS, gy0=b - ROWS, found=True, covered=l is None, scope=True)
        return image, SimpleNamespace(gx0=17, gy0=51, found=False, covered=False, scope=False)
    # No frame seen. A file with the scope's exact proportions is still treated as a capture in the standard layout.
    for height, gy0 in ((512, 51), (480, 19)):
        if abs(image.width / image.height - 800 / height) < 0.012:
            if image.width != 800:
                image = image.resize((800, height), Image.LANCZOS)
            return image, SimpleNamespace(gx0=17, gy0=gy0, found=False, covered=False, scope=True)
    return image, SimpleNamespace(gx0=17, gy0=51, found=False, covered=False, scope=False)


def overlay_mask(rgb, geom):
    """Menus, dialogs and pop-ups drawn over the grid. They are solid grey panels; the grid itself is black.
    Returns a mask the size of the image, or None when nothing sizeable covers the grid."""
    H, W = rgb.shape[:2]
    y0, y1, x0, x1 = max(0, geom.gy0 + 2), min(H, geom.gy0 + ROWS), max(0, geom.gx0), min(W, geom.gx0 + COLS)
    if y1 - y0 < 50 or x1 - x0 < 50:
        return None
    a = rgb[y0:y1, x0:x1].astype(np.int16)
    top = a.max(-1)
    grey = (top >= 24) & (top <= 150) & (top - a.min(-1) <= 20)

    def window_sum(m, half):
        c = np.pad(m.astype(np.int32), ((half + 1, half), (half + 1, half))).cumsum(0).cumsum(1)
        n = 2 * half + 1
        return c[n:, n:] - c[:-n, n:] - c[n:, :-n] + c[:-n, :-n]

    core = window_sum(grey, 4) >= 0.7 * 81
    if core.sum() < 1500:
        return None
    out = np.zeros((H, W), bool)
    out[y0:y1, x0:x1] = window_sum(core, 7) > 0
    return out


def extract_trace(cls, mx, geom, n, covered=None):
    """One point per pixel column: centre of the line, its top and bottom edge, and a flag."""
    H, W = cls.shape
    gx0, gy0 = geom.gx0, geom.gy0
    y_top, y_bot = max(0, gy0 + 2), min(H - 1, gy0 + ROWS - 1)
    if gx0 < 0 or gx0 + COLS > W or y_bot - y_top < 100:
        return None
    area = (slice(y_top, y_bot + 1), slice(gx0, gx0 + COLS))
    mask = cls[area] == n
    if covered is not None:
        mask &= ~covered[area]
    if not mask.any():
        return None
    # With grid labels switched on, the scope prints dim text in the channel colour inside the grid
    # ("400m", "3.36u"). Keep only pixels joined to a bright part of the line, which that text never is.
    level = mx[area]
    keep = mask & (level >= 0.62 * np.percentile(level[mask], 95))
    for _ in range(600):
        grown = keep.copy()
        grown[1:] |= keep[:-1]
        grown[:-1] |= keep[1:]
        grown[:, 1:] |= grown[:, :-1].copy()
        grown[:, :-1] |= grown[:, 1:].copy()
        grown &= mask
        if (grown == keep).all():
            break
        keep = grown
    mask = keep
    # Compression can tint the edge of the orange trigger triangles with a channel colour.
    orange = cls == ORANGE
    near = np.zeros_like(orange)
    near[:, 1:] |= orange[:, :-1]
    near[:, :-1] |= orange[:, 1:]
    near[1:] |= orange[:-1]
    near[:-1] |= orange[1:]
    guard = near[area].copy()
    guard[max(0, gy0 + 11 - y_top):] = False
    mask &= ~guard
    cnt = mask.sum(0)
    has = cnt > 0
    if has.sum() < 60 or cnt.sum() < 150:
        return None
    weight = mx[area].astype(np.float64) * mask
    sw = weight.sum(0)
    rows = np.arange(y_top, y_bot + 1, dtype=np.float64)[:, None]
    yc = np.where(has, (weight * rows).sum(0) / np.where(sw == 0, 1, sw), 0.0)
    ymin = np.where(has, y_top + mask.argmax(0), 0).astype(np.float64)
    ymax = np.where(has, y_bot - mask[::-1].argmax(0), 0).astype(np.float64)
    flag = np.where(has, 0, 1).astype(np.uint8)
    cursor_row = (cls[:, gx0:gx0 + COLS] == ORANGE).sum(1) >= 100
    for k in np.nonzero(has)[0]:
        x, lo, hi = gx0 + k, int(ymin[k]), int(ymax[k])
        above = cls[lo - 1, x] if lo - 1 >= y_top else 0
        below = cls[hi + 1, x] if hi + 1 <= y_bot else 0
        if (1 <= above <= MATH and above != n) or (1 <= below <= MATH and below != n):
            flag[k] = 2
        # A horizontal cursor line drawn over the trace hides one row of it.
        if above == ORANGE and cursor_row[lo - 1]:
            ymin[k] = lo - 1
            yc[k] = (cnt[k] * yc[k] + lo - 1) / (cnt[k] + 1)
        if below == ORANGE and cursor_row[hi + 1]:
            ymax[k] = hi + 1
            yc[k] = (cnt[k] * yc[k] + hi + 1) / (cnt[k] + 1)
    # Columns with no visible line take a straight-line value between their neighbours (flag 1).
    # Before the first and after the last visible column there is no trace at all (flag 3): those are left out.
    good, gaps = np.nonzero(has)[0], np.nonzero(~has)[0]
    for arr in (yc, ymin, ymax):
        arr[gaps] = np.interp(gaps, good, arr[good])
    flag[:good[0]] = 3
    flag[good[-1] + 1:] = 3
    return SimpleNamespace(n=n, colour=COLOURS[n], yc=yc, ymin=ymin, ymax=ymax, flag=flag, filled=int((flag == 1).sum()),
                           shown=int((flag != 3).sum()), px=pixel_stats(yc, ymin, ymax, flag))


def pixel_stats(yc, ymin, ymax, flag):
    """Top and bottom of the line in pixels, and the repeat period from mid-level rising crossings."""
    seen = (flag == 0) | (flag == 2)
    top, bot = float(ymin[seen].min()), float(ymax[seen].max())
    span_cols = np.nonzero(flag != 3)[0]
    ordered = np.sort(yc[span_cols])
    hi_y, lo_y = ordered[int(len(ordered) * 0.05)], ordered[int(len(ordered) * 0.95)]  # small y is high voltage
    amp = lo_y - hi_y
    crossings = []
    if amp >= 6:
        mid, hyst, state = (lo_y + hi_y) / 2, amp * 0.15, 0
        for k in span_cols:
            y = yc[k]
            if y > mid + hyst:
                state = -1
            elif y < mid - hyst:
                if state == -1:
                    j, first = int(k), int(span_cols[0])
                    while j > first and yc[j - 1] < mid:
                        j -= 1
                    y0, y1 = (yc[j - 1], yc[j]) if j > first else (yc[j], yc[j])
                    crossings.append(j - 1 + (y0 - mid) / (y0 - y1) if j > first and y0 != y1 else j)
                state = 1
    span = crossings[-1] - crossings[0] if len(crossings) >= 2 else 0.0
    return SimpleNamespace(top=top, bot=bot, span=span, period=span / (len(crossings) - 1) if len(crossings) >= 2 else None)


def find_side_markers(loose, mx, geom, n):
    """Ground and trigger-level arrows sit in the left margin; the arrow tip marks the level."""
    H = loose.shape[0]
    gx0, gy0 = geom.gx0, geom.gy0
    ground = trig = None
    if gx0 - 6 < 4:
        return ground, trig
    bright = (loose[:, :gx0] == n) & (mx[:, :gx0] >= MARK_BRIGHT)
    y0, y1 = max(0, gy0 - 6), min(H - 1, gy0 + ROWS + 6)
    a = np.zeros(H, int)
    for y in range(y0, y1 + 1):
        c = 0
        for x in range(gx0 - 6, gx0 - 1):
            if not bright[y, x]:
                break
            c += 1
        a[y] = c
    y = y0
    while y <= y1:
        if a[y] < 4:
            y += 1
            continue
        # An arrow is a short run of wide rows with a slope on each side; the "T" above the trigger arrow is not.
        e = y
        while e + 1 <= y1 and a[e + 1] >= 4:
            e += 1
        s2, e2 = y, e
        while s2 - 1 >= y0 and a[s2 - 1] and y - s2 < 4:
            s2 -= 1
        while e2 + 1 <= y1 and a[e2 + 1] and e2 - e < 4:
            e2 += 1
        if e - y <= 3 and y - s2 >= 1 and e2 - e >= 1:
            rows = np.arange(s2, e2 + 1)
            tip = int(round(float((a[s2:e2 + 1] * rows).sum() / a[s2:e2 + 1].sum())))
            stem = int(bright[tip, max(0, gx0 - 10):gx0 - 6].sum())
            if stem >= 3:
                ground = tip if ground is None else ground
            elif trig is None:
                trig = tip
        y = e2 + 1
    return ground, trig


def find_time_markers(loose, geom):
    """Orange triangles under the top edge: solid = trigger instant (t = 0), hollow = time reference."""
    H, W = loose.shape
    gx0, gy0 = geom.gx0, geom.gy0
    ya, yb = gy0 + 2, min(H - 1, gy0 + 9)
    trig_x = ref_x = None
    if ya < 0 or gx0 < 0 or gx0 + COLS > W:
        return trig_x, ref_x
    band = loose[ya:yb + 1, gx0:gx0 + COLS] == ORANGE
    cnt = band.sum(0)
    k = 0
    while k < COLS:
        if not cnt[k]:
            k += 1
            continue
        e = k
        while e < COLS and (cnt[e] or (e + 1 < COLS and cnt[e + 1])):
            e += 1
        total = int(cnt[k:e].sum())
        if 7 <= e - k <= 14 and total >= 10:
            first = next(row for row in band[:, k:e] if row.any())
            idx = np.nonzero(first)[0]
            fill = len(idx) / (idx[-1] - idx[0] + 1)
            x = gx0 + int(round(float((cnt[k:e] * np.arange(k, e)).sum() / total)))
            if fill >= 0.7:
                trig_x = x if trig_x is None else trig_x
            elif ref_x is None:
                ref_x = x
        k = e + 1
    return trig_x, ref_x


def analyze(rgb, geom):
    cls, loose, mx = classify(rgb), classify(rgb, True), rgb.max(-1)
    covered = overlay_mask(rgb, geom)
    channels, trig_marks = [], {}
    for n in TRACES:
        trace = extract_trace(cls, mx, geom, n, covered)
        ground, trig = find_side_markers(loose, mx, geom, n)
        if trig is not None:
            trig_marks[n] = trig
        if trace:
            trace.ground, trace.ground_like = ground, None
            beside = loose[max(0, geom.gy0 - 6):geom.gy0 + ROWS + 6, :max(0, geom.gx0)]
            trace.marker_seen = bool((beside == n).any())
            channels.append(trace)
    # Two ground markers at the same level are drawn on top of each other, so one channel shows none at all.
    marked = [ch for ch in channels if ch.ground is not None and ch.n != MATH]
    for ch in channels:
        if ch.ground is None and not ch.marker_seen and ch.n != MATH and len(marked) == 1:
            ch.ground, ch.ground_like = marked[0].ground, marked[0].n
    trig_x, ref_x = find_time_markers(loose, geom)
    # Cursor lines are long orange dashes; their positions give an independent check on the scales.
    grid = cls[max(0, geom.gy0 + 2):geom.gy0 + ROWS, max(0, geom.gx0):geom.gx0 + COLS] == ORANGE
    cursor_rows = [int(r) + max(0, geom.gy0 + 2) for r in np.nonzero(grid.sum(1) >= 100)[0]]
    cursor_cols = [int(c) + max(0, geom.gx0) for c in np.nonzero(grid.sum(0) >= 100)[0]]
    return SimpleNamespace(H=rgb.shape[0], W=rgb.shape[1], geom=geom, channels=channels, trig_marks=trig_marks,
                           trig_x=trig_x, ref_x=ref_x, mx=mx, cursor_rows=cursor_rows, cursor_cols=cursor_cols,
                           covered=covered is not None)


# ---------------------------------------------------------------- reading the text

SWIFT_SOURCE = r"""
import Foundation
import Vision
import ImageIO

setvbuf(stdout, nil, _IOLBF, 0)
for path in CommandLine.arguments.dropFirst() {
    var items: [[String: Any]] = []
    if let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
       let cg = CGImageSourceCreateImageAtIndex(src, 0, nil) {
        let req = VNRecognizeTextRequest()
        req.recognitionLevel = .accurate
        req.usesLanguageCorrection = false
        req.recognitionLanguages = ["en-US"]
        try? VNImageRequestHandler(cgImage: cg, options: [:]).perform([req])
        let W = Double(cg.width), H = Double(cg.height)
        for o in req.results ?? [] {
            guard let c = o.topCandidates(1).first else { continue }
            let b = o.boundingBox
            items.append(["text": c.string, "x": b.minX * W, "y": (1 - b.maxY) * H, "w": b.width * W, "h": b.height * H])
        }
    }
    if let d = try? JSONSerialization.data(withJSONObject: ["file": path, "items": items] as [String: Any]),
       let s = String(data: d, encoding: .utf8) { print(s) }
}
"""
# Each text field is read at several enlargements, once inverted, and the readings are put to a vote.
VARIANTS = [(3, False), (4, False), (2, False), (3, True)]


def text_reader():
    """Path of the compiled helper that calls the Mac's text recogniser, or None with the reason printed."""
    if sys.platform != "darwin":
        print("  Text reading needs macOS. Give the scales with --ch1/--ch2/--time or a settings file instead.")
        return None
    for base in (Path.home() / "Library" / "Caches" / "scope_digitizer", Path(tempfile.gettempdir()) / "scope_digitizer"):
        exe = base / "vision_ocr_1"
        if exe.exists():
            return exe
        try:
            base.mkdir(parents=True, exist_ok=True)
            (base / "vision_ocr.swift").write_text(SWIFT_SOURCE)
        except OSError:
            continue
        print("  Preparing the text reader (first run only, about half a minute)...", flush=True)
        try:
            done = subprocess.run(["swiftc", "-O", str(base / "vision_ocr.swift"), "-o", str(exe)],
                                  capture_output=True, text=True, timeout=600)
        except (OSError, subprocess.TimeoutExpired) as err:
            print(f"  Could not build the text reader: {err}")
            return None
        if done.returncode == 0 and exe.exists():
            return exe
        print("  Could not build the text reader:", ((done.stderr or "").strip().splitlines() or ["unknown error"])[-1])
        return None
    return None


def sidebar_bands(mx, geom):
    """Row ranges of the text lines in the measurement panel on the right."""
    H, W = mx.shape
    xa, xb, ya, yb = geom.gx0 + 646, min(W, geom.gx0 + 780), geom.gy0 + 25, min(H - 1, geom.gy0 + ROWS - 1)
    if xa >= xb - 20 or ya < 0:
        return []
    lit = (mx[:, xa:xb] > 140).sum(1) >= 2
    bands, start = [], None
    for y in range(ya, yb + 2):
        on = y <= yb and lit[y]
        if on and start is None:
            start = y
        elif not on and start is not None:
            if 7 <= y - start <= 20:
                bands.append((start, y - 1))
            start = None
    return bands


def text_fields(shot):
    """(name, box) for every piece of text to read, skipping empty ones."""
    g, W, H = shot.geom, shot.W, shot.H
    sy = g.gy0 - 18
    boxes = [(f"ch{n}", (g.gx0 + 46 + 97 * (n - 1), sy, 86, 18)) for n in (1, 2, 3, 4)]
    boxes += [("time", (g.gx0 + 425, sy, 133, 18)), ("state", (g.gx0 + 568, sy, 60, 18)), ("trig", (g.gx0 + 659, sy, 92, 18))]
    if g.gy0 >= 40:
        boxes.append(("header", (g.gx0 + 233, g.gy0 - 39, 548, 19)))
    xa = g.gx0 + 645
    boxes += [(f"line{i:02d}", (xa, a - 3, 136, b - a + 7)) for i, (a, b) in enumerate(sidebar_bands(shot.mx, g))]
    fields = []
    for name, (x, y, w, h) in boxes:
        x, y = max(0, x), max(0, y)
        w, h = min(W - x, w), min(H - y, h)
        if w < 4 or h < 4:
            continue
        crop = shot.mx[y:y + h, x:x + w]
        background = np.bincount(crop.ravel(), minlength=256).argmax()
        ink = np.abs(crop.astype(int) - int(background)) > 70
        if ink.sum() >= 12:
            fields.append((name, (x, y, w, h)))
    return fields


def build_sheet(image, fields, scale, invert):
    """Stack the enlarged fields on one page, one per line, so a single recognition pass reads them all.
    The page is built from brightness alone, so coloured text (a selected measurement is orange) reads like white."""
    pad, gap, rows, y = 30, 44, [], 30
    crops = []
    for name, (x, y0, w, h) in fields:
        crop = image.crop((x, y0, x + w, y0 + h)).resize((w * scale, h * scale), Image.LANCZOS)
        crops.append(crop)
        rows.append((name, y, y + crop.height))
        y += crop.height + gap
    sheet = Image.new("L", (max(c.width for c in crops) + 2 * pad, y), 0)
    for crop, (_, top, _) in zip(crops, rows):
        sheet.paste(crop, (pad, top))
    return (ImageOps.invert(sheet) if invert else sheet), rows


def read_texts(shots, quiet=False):
    """Fill shot.read = {field: [reading per variant]} for every shot, in one pass of the recogniser."""
    for s in shots:
        s.read = None
    todo = [s for s in shots if s.geom.scope and (s.channels or s.geom.found)]
    exe = text_reader() if todo else None
    if not exe:
        return False
    with tempfile.TemporaryDirectory(prefix="scope_text_") as tmp:
        jobs = {}
        for i, s in enumerate(todo):
            s.read = {}
            fields = text_fields(s)
            if not fields:
                continue
            for v, (scale, invert) in enumerate(VARIANTS):
                sheet, rows = build_sheet(Image.fromarray(s.mx), fields, scale, invert)
                path = os.path.join(tmp, f"{i:04d}_{v}.png")
                sheet.save(path)
                jobs[path] = (s, rows)
        paths, done = list(jobs), 0
        for start in range(0, len(paths), 200):
            proc = subprocess.Popen([str(exe)] + paths[start:start + 200], stdout=subprocess.PIPE, text=True)
            for line in proc.stdout:
                try:
                    result = json.loads(line)
                except ValueError:
                    continue
                s, rows = jobs[result["file"]]
                per_row = {name: [] for name, _, _ in rows}
                for item in result["items"]:
                    cy = item["y"] + item["h"] / 2
                    for name, top, bottom in rows:
                        if top - 14 <= cy <= bottom + 14:
                            per_row[name].append((item["x"], item["text"]))
                            break
                for name, parts in per_row.items():
                    s.read.setdefault(name, []).append(" ".join(t for _, t in sorted(parts)))
                done += 1
                if not quiet:
                    print(f"\r  Reading text: {done}/{len(paths)} passes", end="", flush=True)
            proc.wait()
        if paths and not quiet:
            print()
    return True


def vote_quantity(texts, kind, whole=True):
    def one(t):
        q = parse_whole(t, kind) if whole else (parse_quantities(t, kind) or [None])[0]
        return (f"{q['value']:.6g}{q['unit']}", q) if q else None
    return vote([one(t) for t in texts or []])


def vote_text(texts):
    v = vote([(t, t) if t else None for t in texts or []])
    return v.item if v else ""


def vote_chars(texts):
    """Long lines rarely agree as a whole, so vote letter by letter among the readings of the most common length."""
    texts = [t for t in texts or [] if t]
    if not texts:
        return ""
    length = int(vote_text([str(len(t)) for t in texts]))
    same = [t for t in texts if len(t) == length]
    return "".join(vote_text([t[i] for t in same]) for i in range(length))


def apply_readings(shot):
    """Turn the raw readings into scales, time base, trigger and measurements. Typed values are never replaced."""
    R = shot.read or {}
    for n in (1, 2, 3, 4):
        v = vote_quantity(R.get(f"ch{n}"), "V")
        if v and shot.scale.get(n, {}).get("src") != "user":
            shot.scale[n] = dict(v.item, src="read", sure=v.sure, unanimous=v.n == len(R.get(f"ch{n}")))
    # The time field reads "<time per division>/ <delay>".
    halves = [re.fullmatch(r"(.*?s)[\\/f!#|](.*)", re.sub(r"\s+", "", tidy(t)), re.I) for t in R.get("time", [])]
    vt = vote_quantity([m.group(1) if m else "" for m in halves], "s")
    vd = vote_quantity([m.group(2) if m else "" for m in halves], "s")
    if not vt:  # no delay shown, or the slash was not read
        vt = vote_quantity(R.get("time"), "s", whole=False)
    if vt and (shot.tdiv or {}).get("src") != "user":
        shot.tdiv = dict(vt.item, src="read", sure=vt.sure, unanimous=vt.n == len(R.get("time")))
    if vd and (shot.delay or {}).get("src") != "user":
        shot.delay = dict(vd.item, src="read", sure=vd.sure)
    shot.run_state = re.sub(r"[^A-Za-z' ]", "", vote_text(R.get("state"))).strip()
    vl = vote_quantity(R.get("trig"), "V", whole=False)
    source = str(next(iter(shot.trig_marks))) if len(shot.trig_marks) == 1 else ""
    shot.trig = dict(source=source, level=vl.item, text=(f"CH{source} at " if source else "") + fmt_si(vl.item["value"], vl.item["unit"])) \
        if vl and vl.sure else None
    header = vote_chars(R.get("header")).translate(LOOKALIKES)
    header = re.sub(r"\b([DM])[S5][O0]-?X\b", r"\1SO-X", header)  # "MSO-X" comes back as "M5O-X" or "M50-X"
    header = re.sub(r"\b([DM]SO-X) ?(\d{4})7\b", r"\1 \2T", header)  # and the model's final T as a 7
    shot.header = re.sub(r"\bM[yY](\d{8})\b", r"MY\1", header)
    shot.meas, pending = [], None
    for name in sorted(k for k in R if k.startswith("line")):
        texts = R[name]
        labels = [l for l in map(parse_meas_label, texts) if l]
        label = vote([(l[0], l[0]) for l in labels])
        if label and label.n >= 2:
            src = vote([(l[1], l[1]) for l in labels if l[0] == label.item and l[1] != "?"])
            pending = (label.item, src.item if src else "?")
            continue
        if pending and not any(re.search(r"\d", t) for t in texts):
            pending = None  # the value is a row of dashes or other text, not a number
            continue
        if not pending:
            continue
        v, plain = vote_quantity(texts, None), vote_text(texts)
        if v:
            shot.meas.append(dict(name=pending[0], src=pending[1], text=v.item["shown"], q=v.item, sure=v.sure and pending[1] != "?"))
        else:
            sure = not re.search(r"\d", plain) and texts.count(plain) >= 2
            shot.meas.append(dict(name=pending[0], src=pending[1], text=plain, q=None, sure=sure))
        pending = None


# ---------------------------------------------------------------- scaling, cross-checks, status

def scale_of(shot, n):
    s = shot.scale.get(n)
    return s if s and s.get("value") and s["value"] > 0 else None


def evaluate(shot):
    """Set time zero, per-channel statistics, cross-checks against the on-screen values, notes and status."""
    notes, checks, g = list(shot.extra_notes), [], shot.geom
    remarks = []  # worth knowing, but nothing to fix
    shot.checks, shot.notes, shot.remarks = checks, notes, remarks
    shot.x0, shot.x0_from = g.gx0 + COLS / 2, "none"
    if not g.scope or (not g.found and not shot.channels):
        notes.append("This does not look like a Keysight screen capture: no grid was found.")
        shot.status = "Skipped"
        return
    if not g.found:
        notes.append("Grid frame not found, so the standard layout was assumed. Compare the check picture.")
    if g.covered or shot.covered:
        remarks.append("A menu or pop-up covers part of the grid. Trace parts hidden behind it are filled in with a straight line.")
    if not shot.channels:
        notes.append("No trace found in this image.")

    # Typed values are taken as given; only values read from the picture are questioned.
    # A fine-adjust value such as 820 mV is believed when every reading of it agreed.
    def odd(f):
        return f.get("src") != "user" and (f.get("mant", 0) >= 1000 or (f.get("unit") == "V" and f["value"] > 50)
                                           or (not is125(f["value"]) and not f.get("unanimous")))

    tdiv = shot.tdiv["value"] if shot.tdiv and (shot.tdiv.get("value") or 0) > 0 else None
    if not tdiv:
        notes.append("Time per division is missing.")
    elif odd(shot.tdiv):
        notes.append(f"Time per division reads {fmt_si(tdiv, 's')}. That is not a standard 1-2-5 step, so check it against the status line.")
    delay = shot.delay["value"] if shot.delay else None

    if shot.trig_x is not None:
        shot.x0, shot.x0_from = shot.trig_x, "trigger marker"
    elif delay is not None and tdiv:
        shot.x0 = (shot.ref_x if shot.ref_x is not None else g.gx0 + COLS / 2) - delay / tdiv * DIVX
        shot.x0_from = "delay readout"
    else:
        shot.x0_from = "screen centre (assumed)"
        notes.append("Trigger position not found, so time zero is set at the screen centre.")

    def check(what, screen, trace, unit, tol, uses):
        checks.append(SimpleNamespace(what=what, screen=float(screen), trace=float(trace), unit=unit, uses=uses,
                                      ok=bool(abs(screen - trace) <= tol)))

    if shot.trig_x is not None and shot.ref_x is not None and delay is not None and tdiv:
        check("Delay", delay, (shot.ref_x - shot.trig_x) / DIVX * tdiv, "s", max(1.5 * tdiv / DIVX, abs(delay) * 0.01), ["t"])
    by_n = {ch.n: ch for ch in shot.channels}
    for ch in shot.channels:
        sc = scale_of(shot, ch.n)
        ch.ready = bool(sc and tdiv)
        ground = ch.ground if ch.ground is not None else g.gy0 + ROWS / 2
        ch.vmax = (ground - ch.px.top) * sc["value"] / DIVY if sc else None
        ch.vmin = (ground - ch.px.bot) * sc["value"] / DIVY if sc else None
        ch.period = ch.px.period * tdiv / DIVX if tdiv and ch.px.period else None
        if ch.filled > 0.15 * ch.shown:
            notes.append(f"{trace_label(ch.n)}: {ch.filled} of {ch.shown} points were hidden (behind another trace or a pop-up) "
                         "and are filled in with a straight line.")
        if ch.n == MATH:
            if not sc:
                remarks.append("A math trace is on screen. The scope does not show its scale there, so it is written only as "
                               "pixel positions, or in volts once you type its scale.")
            continue
        if not sc:
            notes.append(f"CH{ch.n} scale per division is missing.")
        elif odd(sc):
            notes.append(f"CH{ch.n} scale reads {fmt_si(sc['value'], sc['unit'])}/div. That is not a standard 1-2-5 step, so check it against the status line.")
        if ch.ground is None:
            notes.append(f"CH{ch.n} ground marker not found, so its values are measured from the screen centre.")
        elif ch.ground_like:
            remarks.append(f"CH{ch.n} shows no ground marker of its own; it is taken to sit behind CH{ch.ground_like}'s, at the same level.")
    tr = shot.trig
    if tr and tr["source"]:
        n = int(tr["source"])
        ch, sc, mark = by_n.get(n), scale_of(shot, n), shot.trig_marks.get(n)
        if ch and sc and ch.ground is not None and mark is not None:
            level = tr["level"]["value"]
            check("Trigger level", level, (ch.ground - mark) * sc["value"] / DIVY, sc["unit"],
                  max(2.5 * sc["value"] / DIVY, abs(level) * 0.02), [f"ch{n}"])
    for m in shot.meas:
        if not m["q"] or m["src"] not in ("1", "2", "3", "4"):
            continue
        n = int(m["src"])
        ch, sc = by_n.get(n), scale_of(shot, n)
        if not ch:
            continue
        label, value = meas_label(m), m["q"]["value"]
        key = re.sub(r"[^a-z]", "", m["name"].lower())
        vtol = max(3 * sc["value"] / DIVY, abs(value) * 0.04) if sc else 0
        ttol = value * max(0.02, 2 / max(1.0, ch.px.span))
        if sc and ch.ground is not None and key == "max":
            check(label, value, ch.vmax, sc["unit"], vtol, [f"ch{n}", "m:" + label])
        elif sc and ch.ground is not None and key == "min":
            check(label, value, ch.vmin, sc["unit"], vtol, [f"ch{n}", "m:" + label])
        elif sc and key == "pkpk":
            check(label, value, ch.vmax - ch.vmin, sc["unit"], vtol * 1.5, [f"ch{n}", "m:" + label])
        elif ch.period and key == "freq":
            check(label, value, 1 / ch.period, "Hz", ttol, ["t", "m:" + label])
        elif ch.period and key == "period":
            check(label, value, ch.period, "s", ttol, ["t", "m:" + label])
        elif sc and ch.ground is not None and key == "y" and shot.cursor_rows:
            # A Y cursor readout against where its dashed line actually sits.
            at = min(((ch.ground - r) * sc["value"] / DIVY for r in shot.cursor_rows), key=lambda v: abs(v - value))
            check(label, value, at, sc["unit"], 1.6 * sc["value"] / DIVY, [f"ch{n}", "m:" + label])
        elif tdiv and key == "x" and shot.cursor_cols:
            times = [(c - shot.x0) * tdiv / DIVX for c in shot.cursor_cols]
            if (g.gx0 + 3 - shot.x0) * tdiv / DIVX <= value <= (g.gx0 + COLS - 3 - shot.x0) * tdiv / DIVX:  # on screen
                check(label, value, min(times, key=lambda v: abs(v - value)), "s", 1.6 * tdiv / DIVX, ["t", "m:" + label])
    for c in checks:
        if not c.ok:
            notes.append(f"{c.what} does not match: the screen says {fmt_si(c.screen, c.unit)}, the trace gives {fmt_si(c.trace, c.unit)}.")
    # A weak reading is fine when a passing cross-check depends on it; otherwise ask for a look.
    covered = {u for c in checks if c.ok for u in c.uses}
    for ch in shot.channels:
        sc = scale_of(shot, ch.n)
        if sc and sc.get("sure") is False and f"ch{ch.n}" not in covered:
            notes.append(f"CH{ch.n} scale was hard to read. Compare it with the screenshot.")
    if tdiv and shot.tdiv.get("sure") is False and "t" not in covered:
        notes.append("Time per division was hard to read. Compare it with the screenshot.")
    for m in shot.meas:
        label = meas_label(m)
        if m["sure"] is False and "m:" + label not in covered:
            notes.append(f"{label} was hard to read. Compare it with the screenshot.")
    shot.status = "Review" if notes else "OK"


def y_decimals(shot, ch, coords):
    """Decimal places worth writing for y: a tenth of a pixel is as fine as the line centre can be placed."""
    if coords == "pixels":
        return 1
    return max(0, math.ceil(-math.log10(scale_of(shot, ch.n)["value"] / DIVY))) + 1


def trace_points(shot, ch, coords):
    """x, y, y_min, y_max for a channel, in real units or as pixel positions on the grid."""
    g = shot.geom
    if coords == "pixels":
        base = g.gy0 + ROWS  # bottom edge of the grid
        return np.arange(COLS, dtype=float), base - ch.yc, base - ch.ymax, base - ch.ymin
    sc = scale_of(shot, ch.n)
    ground = ch.ground if ch.ground is not None else g.gy0 + ROWS / 2
    k = sc["value"] / DIVY
    x = (g.gx0 + np.arange(COLS) - shot.x0) * shot.tdiv["value"] / DIVX
    return x, (ground - ch.yc) * k, (ground - ch.ymax) * k, (ground - ch.ymin) * k


# ---------------------------------------------------------------- writing results

def _num(v, decimals):
    s = f"{v:.{decimals}f}".rstrip("0").rstrip(".") if decimals else f"{v:.0f}"
    return "0" if s in ("-0", "") else s


def info_rows(shot, ch):
    sc = scale_of(shot, ch.n)
    rows = [("source_image", shot.name), ("scope", shot.header), ("channel", f"{trace_label(ch.n)} ({ch.colour})"),
            ("vertical_scale", fmt_si(sc["value"], sc["unit"], plain=True) + "/div" if sc else ""),
            ("time_scale", fmt_si(shot.tdiv["value"], "s", plain=True) + "/div" if shot.tdiv and shot.tdiv.get("value") else ""),
            ("delay", fmt_si(shot.delay["value"], "s", plain=True) if shot.delay else ""),
            ("trigger", shot.trig["text"] if shot.trig else ""), ("run_state", shot.run_state),
            ("time_zero_from", shot.x0_from),
            ("ground_from", f"CH{ch.ground_like}'s ground marker (own marker hidden)" if ch.ground_like
             else "ground marker" if ch.ground is not None else "screen centre (assumed)")]
    rows += [(f"measurement {meas_label(m)}", m["text"]) for m in shot.meas]
    rows.append(("status", shot.status + (": " + " ".join(shot.notes) if shot.notes else "")))
    return [(k, v) for k, v in rows if v]


def trace_csv(shot, ch, opt):
    """The CSV text for one trace. opt has coords (units/pixels), columns (xy/full) and info (none/below/above)."""
    x, y, lo, hi = trace_points(shot, ch, opt.coords)
    if opt.coords == "pixels":
        ux = uy = "px"
        fx, fy = (lambda v: _num(v, 0)), (lambda v: _num(v, 1))
    else:
        sc = scale_of(shot, ch.n)
        ux, uy = "s", sc["unit"]
        decimals = y_decimals(shot, ch, opt.coords)
        places = max(0, math.ceil(-math.log10(shot.tdiv["value"] / DIVX))) + 4  # enough to write one time step exactly
        fx, fy = (lambda v: _num(v, places)), (lambda v: _num(v, decimals))
    head = [f"x ({ux})", f"y ({uy})"] + ([f"y_min ({uy})", f"y_max ({uy})", "flag"] if opt.columns == "full" else [])
    info = [["# " + k, v] for k, v in info_rows(shot, ch)] if opt.info != "none" else []
    if info and opt.columns == "full":
        info.append(["# columns", "y = centre of the line; y_min and y_max = its bottom and top edge; "
                                  "flag 0 = measured, 1 = filled in (no line visible), 2 = touches another trace"])
    f = io.StringIO()
    w = csv.writer(f)
    if opt.info == "above":
        w.writerows(info)
    w.writerow(head)
    for i in np.nonzero(ch.flag != 3)[0]:
        row = [fx(x[i]), fy(y[i])]
        if opt.columns == "full":
            row += [fy(lo[i]), fy(hi[i]), int(ch.flag[i])]
        w.writerow(row)
    if opt.info == "below":
        w.writerow([])
        w.writerows(info)
    return f.getvalue()


def _save_text(path, text):
    with open(path, "w", newline="") as f:
        f.write(text)


def _font(size):
    try:
        return ImageFont.load_default(size)
    except Exception:  # older Pillow, or a build without scalable fonts
        return ImageFont.load_default()


def _dashes(draw, a, b, colour):
    (x0, y0), (x1, y1) = a, b
    length = max(abs(x1 - x0), abs(y1 - y0))
    for s in range(0, int(length), 8):
        t0, t1 = s / length, min(1.0, (s + 4) / length)
        draw.line([(x0 + (x1 - x0) * t0, y0 + (y1 - y0) * t0), (x0 + (x1 - x0) * t1, y0 + (y1 - y0) * t1)], fill=colour)


def check_picture(shot):
    """The screenshot with the extracted line drawn over it, for comparing by eye."""
    g = shot.geom
    out = Image.new("RGB", (shot.W, shot.H + 26), (11, 14, 17))
    out.paste(shot.image)
    d = ImageDraw.Draw(out)
    if g.gx0 <= shot.x0 <= g.gx0 + COLS:
        _dashes(d, (round(shot.x0), g.gy0 + 1), (round(shot.x0), g.gy0 + ROWS), (220, 220, 220))
    for ch in shot.channels:
        if ch.ground is not None:
            _dashes(d, (g.gx0, ch.ground), (g.gx0 + COLS, ch.ground), (220, 220, 220))
    for ch in shot.channels:
        d.line([(g.gx0 + int(k), float(ch.yc[k])) for k in np.nonzero(ch.flag != 3)[0]], fill=(255, 31, 75) if ch.n <= 2 else (255, 255, 255), width=1)
    bits = ["Red line: extracted centre. Dashes: ground levels and time zero."]
    for ch in shot.channels:
        sc = scale_of(shot, ch.n)
        if sc or ch.n != MATH:
            bits.append(f"{trace_label(ch.n)} " + (fmt_si(sc["value"], sc["unit"]) + "/div" if sc else "scale missing"))
    bits.append(fmt_si(shot.tdiv["value"], "s") + "/div" if shot.tdiv and shot.tdiv.get("value") else "time scale missing")
    d.text((8, shot.H + 6), "   ".join(bits), fill=(232, 236, 240), font=_font(12))
    return out


def summary_csv(shots):
    chans = [n for n in TRACES if any(ch.n == n for s in shots for ch in s.channels)]
    keys = []
    for s in shots:
        for m in s.meas:
            if meas_label(m) not in keys:
                keys.append(meas_label(m))
    head = ["image", "status", "notes", "remarks", "scope", "run_state", "time_per_div_s", "delay_s", "time_zero_from", "trigger"]
    for n in chans:
        k = trace_key(n)
        head += [f"{k}_per_div", f"{k}_unit", f"{k}_trace_max", f"{k}_trace_min", f"{k}_trace_freq_Hz"]
    for k in keys:
        head += [k + "_screen", k + "_value"]
    head.append("checks")
    g6 = lambda v: "" if v is None else f"{v:.6g}"
    f = io.StringIO()
    w = csv.writer(f)
    w.writerow(head)
    for s in shots:
        row = [s.name, s.status, " ".join(s.notes), " ".join(s.remarks), s.header, s.run_state, g6(s.tdiv["value"]) if s.tdiv else "",
               g6(s.delay["value"]) if s.delay else "", s.x0_from, s.trig["text"] if s.trig else ""]
        for n in chans:
            ch, sc = next((c for c in s.channels if c.n == n), None), scale_of(s, n)
            row += [g6(sc["value"]) if sc else "", sc["unit"] if sc else "", g6(ch.vmax) if ch else "", g6(ch.vmin) if ch else "",
                    g6(1 / ch.period) if ch and ch.period else ""]
        for k in keys:
            m = next((m for m in s.meas if meas_label(m) == k), None)
            row += [m["text"] if m else "", g6(m["q"]["value"]) if m and m["q"] else ""]
        row.append("; ".join(f"{c.what} {'ok' if c.ok else 'MISMATCH'} (screen {fmt_si(c.screen, c.unit, plain=True)}, "
                             f"trace {fmt_si(c.trace, c.unit, plain=True)})" for c in s.checks))
        w.writerow(row)
    return f.getvalue()


SETTING_COLUMNS = ["image", "ch1", "ch2", "ch3", "ch4", "math", "time", "delay"]


def write_settings(path, shots):
    """The scales used for each image. Correct a value here and rerun with --settings to apply it."""
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(SETTING_COLUMNS)
        for s in shots:
            cell = lambda q: fmt_si(q["value"], q["unit"], 6, plain=True) if q and q.get("value") is not None else ""
            w.writerow([s.name] + [cell(scale_of(s, n)) for n in TRACES] + [cell(s.tdiv), cell(s.delay)])


def read_settings(path):
    with open(path, newline="") as f:
        return {row["image"]: row for row in csv.DictReader(f) if row.get("image")}


def apply_typed(shot, row):
    """Values from a settings file or the command line override anything read from the picture."""
    for n in TRACES:
        q = parse_typed(row.get(trace_key(n)) or "", "V")
        if q and q["value"] > 0:
            shot.scale[n] = dict(q, src="user")
    q = parse_typed(row.get("time") or "", "s")
    if q and q["value"] > 0:
        shot.tdiv = dict(q, src="user")
    q = parse_typed(row.get("delay") or "", "s")
    if q:
        shot.delay = dict(q, src="user")


# ---------------------------------------------------------------- images -> CSV

def gather(paths, types):
    files = []
    for p in map(Path, paths):
        if p.is_dir():
            files += sorted((f for f in p.iterdir() if f.suffix.lower() in types and not f.name.startswith(".")),
                            key=lambda f: [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", f.name)])
        elif p.exists():
            files.append(p)
        else:
            print(f"  Not found: {p}")
    return files


def load_shot(source, name=None):
    """Open an image (a path, or a file-like object with a name) and read its pixels."""
    name = name or Path(source).name
    shot = SimpleNamespace(name=name, stem=Path(name).stem, scale={}, tdiv=None, delay=None, trig=None, header="", run_state="",
                           meas=[], extra_notes=[], read=None, channels=[], trig_marks={}, checks=[], notes=[], remarks=[],
                           status="Failed", x0_from="", cursor_rows=[], cursor_cols=[])
    try:
        im = Image.open(source).convert("RGB")
    except Exception as err:  # a file Pillow cannot open
        shot.notes, shot.error = [f"Could not open this file ({type(err).__name__}); it may not be an image."], True
        return shot
    im, geom = normalise(im)
    shot.image = im
    if geom.scope:
        shot.__dict__.update(analyze(np.asarray(im), geom).__dict__)
    else:  # not a scope capture: nothing to extract
        shot.__dict__.update(H=im.height, W=im.width, geom=geom, mx=np.asarray(im).max(-1), trig_x=None, ref_x=None, covered=False)
    return shot


def convert(args):
    files = [f for f in gather(args.paths, IMAGE_TYPES) if not f.name.endswith("_check.png")]
    if not files:
        sys.exit("No images found. Give a folder of screenshots or image files.")
    first = Path(args.paths[0])
    out = Path(args.out) if args.out else (first if first.is_dir() else first.parent) / "scope_output"
    (out / "csv").mkdir(parents=True, exist_ok=True)
    if not args.no_check:
        (out / "check").mkdir(exist_ok=True)
    print(f"Reading {len(files)} image{'s' if len(files) != 1 else ''}...")
    shots = [load_shot(f) for f in files]
    usable = [s for s in shots if not getattr(s, "error", False)]
    if not args.no_text:
        read_texts(usable)
    settings = read_settings(args.settings) if args.settings else {}
    typed = {k: getattr(args, k) for k in SETTING_COLUMNS[1:] if getattr(args, k)}
    used, written = {}, 0
    for s in usable:
        if s.read is not None:
            apply_readings(s)
        apply_typed(s, typed)
        apply_typed(s, settings.get(s.name, {}))
        evaluate(s)
        stem = re.sub(r"[^\w.\-]+", "_", s.stem) or "image"
        used[stem] = used.get(stem, 0) + 1
        stem += f"_{used[stem]}" if used[stem] > 1 else ""
        s.stem = stem
        for ch in s.channels:
            if ch.ready or (args.coords == "pixels" and args.info == "none"):
                _save_text(out / "csv" / trace_file(stem, ch), trace_csv(s, ch, args))
                written += 1
        if not args.no_check and (s.channels or s.geom.found):
            check_picture(s).save(out / "check" / f"{stem}_check.png")
    _save_text(out / "summary.csv", summary_csv(shots))
    write_settings(out / "settings.csv", usable)
    for s in shots:
        scales = "  ".join(f"{trace_label(ch.n)} {fmt_si(scale_of(s, ch.n)['value'], scale_of(s, ch.n)['unit'])}/div" if scale_of(s, ch.n)
                           else f"{trace_label(ch.n)} ?" for ch in s.channels if ch.n != MATH or scale_of(s, ch.n))
        time = fmt_si(s.tdiv["value"], "s") + "/div" if s.tdiv and s.tdiv.get("value") else ""
        passed = sum(c.ok for c in s.checks)
        print(f"  {s.status:<7} {s.name}  {scales}  {time}  " + (f"{passed}/{len(s.checks)} checks agree" if s.checks else ""))
        for note in s.notes:
            print(f"            ! {note}")
        for remark in s.remarks:
            print(f"            - {remark}")
    review, skipped = sum(s.status == "Review" for s in shots), sum(s.status in ("Skipped", "Failed") for s in shots)
    print(f"{written} CSV file{'s' if written != 1 else ''} from {len(shots)} image{'s' if len(shots) != 1 else ''}; "
          f"{review} to review" + (f", {skipped} not scope captures" if skipped else "") + f". Results are in {out}")
    if review:
        print(f"To correct a scale: edit {out / 'settings.csv'} and rerun with --settings \"{out / 'settings.csv'}\"")
    return out


# ---------------------------------------------------------------- CSV -> graph

def read_table(path):
    """Any CSV: '#' lines are skipped, the last text row above the numbers names the columns,
    and a unit may follow a name in brackets or sit in a second header row (as in the scope's own CSV)."""
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        lines = [ln for ln in f.read().splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        return None
    sample = lines[:30]
    delim = "\t" if all("\t" in l for l in sample) else ";" if all(";" in l for l in sample) else ","
    rows = list(csv.reader(lines, delimiter=delim))

    def number(s):
        s = s.strip().replace(",", ".") if delim == ";" else s.strip()
        try:
            return float(s)
        except ValueError:
            return math.nan

    def is_data(r):
        cells = [c for c in r if c.strip()]
        return bool(cells) and all(math.isfinite(number(c)) for c in cells)

    start = next((i for i, r in enumerate(rows) if is_data(r)), None)
    if start is None:
        return None
    width = len(rows[start])
    heads = rows[:start]
    names, units = (heads[-1] if heads else []), []
    unit_word = re.compile(r"(second|volt|amp|hertz|s|v|a|hz|ms|mv|us)s?", re.I)
    if len(heads) >= 2 and all(not c.strip() or unit_word.fullmatch(c.strip()) for c in names):
        units, names = names, heads[-2]
    cols = []
    for j in range(width):
        raw = names[j].strip() if j < len(names) else ""
        unit = units[j].strip() if j < len(units) else ""
        m = re.fullmatch(r"(.*?)\s*\(([^)]+)\)", raw)
        if m:
            raw, unit = m.group(1).strip(), m.group(2)
        unit = {"second": "s", "seconds": "s", "sec": "s", "volt": "V", "volts": "V", "v": "V", "amp": "A", "amps": "A"}.get(unit.lower(), unit)
        values = np.array([number(r[j]) if j < len(r) else math.nan for r in rows[start:]])
        cols.append(SimpleNamespace(name=raw or f"column {j + 1}", unit=unit, values=values))
    return cols


def load_groups(files):
    """Group CSV files into graphs: the two channel files of one screenshot share a graph."""
    groups = {}
    for f in files:
        cols = read_table(f)
        if not cols or len(cols) < 2 or len(cols[0].values) < 2:
            print(f"  Skipped (no numeric columns): {f.name}")
            continue
        m = re.fullmatch(r"(.*)_(ch[1-4]|math)_(yellow|green|blue|pink|purple)", f.stem)
        group = groups.setdefault(m.group(1) if m else f.stem, SimpleNamespace(title=m.group(1) if m else f.stem, series=[], x_unit="", x_name="x"))
        xi = next((j for j, c in enumerate(cols) if re.fullmatch(r"(time|t|x|x-axis|second|seconds|time_s)", c.name, re.I)), 0)
        x = cols[xi]
        group.x_unit = x.unit if x.unit in ("s", "px") else "s" if re.search(r"time|second|x-axis", x.name, re.I) else ""
        group.x_name = "Time" if group.x_unit == "s" else x.name
        by_name = {c.name: c for c in cols}
        for j, c in enumerate(cols):
            if j == xi or c.name.lower() in ("flag", "filled") or re.search(r"_(min|max)$", c.name):
                continue
            lo, hi = by_name.get(c.name + "_min"), by_name.get(c.name + "_max")
            if m:
                name, colour = ("Math" if m.group(2) == "math" else m.group(2).upper()), SERIES[m.group(3)]
            elif re.fullmatch(r"[1-4]", c.name):  # the scope's own CSV names its columns 1 to 4
                name, colour = f"CH{c.name}", SERIES[COLOURS[int(c.name)]]
            else:
                name, colour = c.name, SLOTS[len(group.series) % len(SLOTS)]
            unit = c.unit or {"volts": "V", "volt": "V", "voltage": "V", "amps": "A"}.get(c.name.lower(), "")
            group.series.append(SimpleNamespace(name=name, colour=colour, unit=unit, x=x.values, y=c.values,
                                                lo=lo.values if lo and hi else None, hi=hi.values if lo and hi else None))
    return [g for g in groups.values() if g.series]


def shot_group(shot):
    """The traces of one screenshot as a graph group, without going through CSV files."""
    series = []
    for ch in shot.channels:
        if ch.ready:
            x, y, _, _ = trace_points(shot, ch, "units")
            there = ch.flag != 3
            series.append(SimpleNamespace(name=trace_label(ch.n), colour=SERIES[ch.colour], unit=scale_of(shot, ch.n)["unit"],
                                          x=x[there], y=y[there], lo=None, hi=None))
    return SimpleNamespace(title=shot.stem, series=series, x_unit="s", x_name="Time")


def unit_scale(max_abs, unit):
    if unit in ("", "px", "%") or not max_abs or not math.isfinite(max_abs):
        return 1.0, unit
    e = max(-12, min(9, math.floor(math.log10(max_abs) / 3 + 1e-9) * 3))
    return 10.0 ** e, {-12: "p", -9: "n", -6: "µ", -3: "m", 0: "", 3: "k", 6: "M", 9: "G"}[e] + unit


def draw_graph(plt, group, overlay):
    ink, ink2, grid, axis, surface = "#0b0b0b", "#52514e", "#e1e0d9", "#c3c2b7", "#fcfcfb"
    panels = [group.series] if overlay or len(group.series) == 1 else [[s] for s in group.series]
    fig, axes = plt.subplots(len(panels), 1, sharex=True, squeeze=False, facecolor=surface,
                             figsize=(10, 4.8 if len(panels) == 1 else 2.5 * len(panels) + 1.3))
    xf, xu = unit_scale(max(float(np.nanmax(np.abs(s.x))) for s in group.series), group.x_unit)
    for ax, series in zip(axes[:, 0], panels):
        unit = series[0].unit if all(s.unit == series[0].unit for s in series) else ""
        yf, yu = unit_scale(max(float(np.nanmax(np.abs(s.y))) for s in series), unit)
        for s in series:
            if s.lo is not None:
                ax.fill_between(s.x / xf, s.lo / yf, s.hi / yf, color=s.colour, alpha=0.16, linewidth=0)
            ax.plot(s.x / xf, s.y / yf, color=s.colour, linewidth=1.5, solid_joinstyle="round", label=s.name)
        ax.set_facecolor(surface)
        ax.grid(True, color=grid, linewidth=0.8)
        ax.set_axisbelow(True)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(axis)
        ax.tick_params(colors=ink2, labelsize=9, length=0)
        ax.set_ylabel(yu, color=ink2, fontsize=10, rotation=0, ha="right", va="center", labelpad=14)
        ax.margins(x=0, y=0.08)
        ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=8, frameon=False, fontsize=10, labelcolor=ink,
                  borderaxespad=0.2, handlelength=1.4)
    axes[-1, 0].set_xlabel(f"{group.x_name} ({xu})" if xu else group.x_name, color=ink2, fontsize=10)
    fig.suptitle(group.title, x=0.01, ha="left", fontsize=13, fontweight="bold", color=ink)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    return fig


def plot(args):
    files = [f for f in gather(args.paths, {".csv", ".tsv", ".txt"}) if f.name not in ("summary.csv", "settings.csv")]
    if not files:
        sys.exit("No CSV files found. Give a folder of CSV files or the files themselves.")
    import matplotlib
    if not args.show:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    groups = load_groups(files)
    first = Path(args.paths[0])
    base = first if first.is_dir() else first.parent
    # Graphs for a results folder's csv/ go beside it, not inside it.
    out = Path(args.out) if args.out else (base.parent if base.name == "csv" else base) / "graphs"
    out.mkdir(parents=True, exist_ok=True)
    shown = 0
    for g in groups:
        fig = draw_graph(plt, g, args.overlay)
        fig.savefig(out / (re.sub(r"[^\w.\-]+", "_", g.title) + ".png"), dpi=160, facecolor=fig.get_facecolor())
        if args.show and shown < 8:
            shown += 1
        else:
            plt.close(fig)
    print(f"{len(groups)} graph{'s' if len(groups) != 1 else ''} saved in {out}")
    if args.show:
        if len(groups) > shown:
            print(f"Showing the first {shown} in windows; the rest are saved as pictures.")
        plt.show()
    return out


# ---------------------------------------------------------------- command line

def main():
    argv = sys.argv[1:]
    mode = argv.pop(0) if argv and argv[0] in ("convert", "plot") else "all"
    p = argparse.ArgumentParser(prog=f"scope_digitizer.py{'' if mode == 'all' else ' ' + mode}", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("paths", nargs="+", help="a folder, or individual files")
    p.add_argument("-o", "--out", help="where to put the results (default: scope_output inside the input folder)")
    if mode != "plot":
        p.add_argument("--coords", choices=["units", "pixels"], default="units",
                       help="x,y as seconds and volts (default), or as pixel positions on the grid (x from the left, y up from the bottom)")
        p.add_argument("--columns", choices=["xy", "full"], default="xy", help="xy: just x,y (default). full: also the line's bottom and top edge and a flag")
        p.add_argument("--info", choices=["none", "below", "above"], default="none",
                       help="where each CSV carries the scope readouts: nowhere (they are all in summary.csv; default), below the points, or above them")
        p.add_argument("--settings", help="a settings.csv with corrected scales (the tool writes one on every run)")
        for name, what in (("ch1", "CH1"), ("ch2", "CH2"), ("ch3", "CH3"), ("ch4", "CH4"), ("math", "math trace")):
            p.add_argument("--" + name, help=f"{what} scale per division for every image, e.g. 500mV")
        p.add_argument("--time", help="time per division for every image, e.g. 2ms")
        p.add_argument("--delay", help="delay for every image, e.g. 1.406ms (only used when no trigger marker is visible)")
        p.add_argument("--no-text", action="store_true", help="do not read text from the images; use only the values given here")
        p.add_argument("--no-check", action="store_true", help="skip the check pictures")
    if mode != "convert":
        p.add_argument("--overlay", action="store_true", help="draw all channels in one panel instead of one panel each")
        p.add_argument("--show", action="store_true", help="also open the graphs in zoomable windows")
    args = p.parse_args(argv)
    if mode == "plot":
        plot(args)
    else:
        out = convert(args)
        if mode == "all":
            args.paths, args.out = [str(out / "csv")], str(out / "graphs")
            plot(args)


if __name__ == "__main__":
    main()
