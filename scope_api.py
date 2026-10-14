"""Requests the page makes, answered by the digitizer.

The same handler serves both ways of running the tool: behind the small local web server
(scope_web.py), and inside the browser itself, where Python runs as WebAssembly and nothing
is sent anywhere.
"""
import io
import json
import re
from pathlib import Path
from types import SimpleNamespace

import scope_digitizer as sd

SHOTS = {}  # id -> shot, in the order they were added
# reads_text: True when Python reads the on-screen text itself (the Mac's recogniser);
# False when the page reads it and hands the readings over.
STATE = SimpleNamespace(next_id=1, text_ok=None, reads_text=True)
MAX_UPLOAD = 40 * 1024 * 1024


def is_scope(shot):
    return bool(not getattr(shot, "error", False) and shot.geom.scope and (shot.geom.found or shot.channels))


def add_shot(name, data):
    shot = sd.load_shot(io.BytesIO(data), name)
    shot.id, shot.rev, shot.awaiting_text = STATE.next_id, 1, False
    STATE.next_id += 1
    stems = {s.stem for s in SHOTS.values()}
    base = re.sub(r"[^\w.\-]+", "_", Path(name).stem) or "image"
    shot.stem, k = base, 1
    while shot.stem in stems:
        k += 1
        shot.stem = f"{base}_{k}"
    if not getattr(shot, "error", False):
        if is_scope(shot) and STATE.reads_text:
            STATE.text_ok = sd.read_texts([shot], quiet=True)
            if shot.read is not None:
                sd.apply_readings(shot)
            elif shot.channels:
                shot.extra_notes.append("The text reader is not available, so type the scales in.")
        elif is_scope(shot):
            shot.awaiting_text = True
        sd.evaluate(shot)
    SHOTS[shot.id] = shot
    return shot


def set_texts(shot, texts):
    """Readings made by the page: {field: [one reading per pass]}, or None when its reader could not start."""
    shot.awaiting_text = False
    if texts is None:
        if shot.channels:
            shot.extra_notes.append("The text reader could not be loaded, so type the scales in.")
    else:
        shot.read = {str(k): [str(t) for t in v] for k, v in texts.items()}
        sd.apply_readings(shot)
    sd.evaluate(shot)
    shot.rev += 1


def set_value(shot, key, text):
    """A value typed on the page. An unreadable entry is kept as typed and counts as missing."""
    text = str(text or "").strip()
    if re.fullmatch(r"ch[1-4]|math", key):
        q = sd.parse_typed(text, "V")
        n = sd.MATH if key == "math" else int(key[2])
        shot.scale[n] = dict(q, src="user") if q and q["value"] > 0 else {"value": None, "raw": text, "src": "user"}
    elif key == "time":
        q = sd.parse_typed(text, "s")
        shot.tdiv = dict(q, src="user") if q and q["value"] > 0 else {"value": None, "raw": text, "src": "user"}
    elif key == "delay":
        q = sd.parse_typed(text, "s")
        shot.delay = dict(q, src="user") if q else None
    elif re.fullmatch(r"meas\d+", key) and int(key[4:]) < len(shot.meas):
        m = shot.meas[int(key[4:])]
        found = sd.parse_quantities(text)
        m.update(text=text, q=found[0] if found else None, sure=True)
    sd.evaluate(shot)
    shot.rev += 1


def describe(shot):
    """What the page needs to draw a card."""
    d = {"id": shot.id, "rev": shot.rev, "name": shot.name, "stem": shot.stem, "status": shot.status, "notes": shot.notes,
         "remarks": shot.remarks, "usable": is_scope(shot)}
    if not d["usable"]:
        return d
    fmt = sd.fmt_si
    channels = []
    for ch in shot.channels:
        sc, typed = sd.scale_of(shot, ch.n), shot.scale.get(ch.n) or {}
        channels.append({
            "n": ch.n, "label": sd.trace_label(ch.n), "key": sd.trace_key(ch.n), "file": sd.trace_file(shot.stem, ch),
            "colour": ch.colour, "ready": ch.ready, "unit": sc["unit"] if sc else "V",
            "scale": fmt(sc["value"], sc["unit"]) if sc else typed.get("raw", ""), "scale_ok": bool(sc),
            "max": fmt(ch.vmax, sc["unit"], 4) if sc else "", "min": fmt(ch.vmin, sc["unit"], 4) if sc else "",
            "freq": fmt(1 / ch.period, "Hz", 4) if ch.period else "", "filled": ch.filled})
    time_ok = bool(shot.tdiv and shot.tdiv.get("value"))
    d.update(channels=channels, time_ok=time_ok, x0_from=shot.x0_from, run_state=shot.run_state, header=shot.header,
             time=fmt(shot.tdiv["value"], "s") if time_ok else (shot.tdiv or {}).get("raw", ""),
             delay=fmt(shot.delay["value"], "s") if shot.delay else "", trigger=shot.trig["text"] if shot.trig else "",
             meas=[{"label": sd.meas_label(m), "text": m["text"], "sure": m["sure"] is not False} for m in shot.meas],
             checks=[{"what": c.what, "screen": fmt(c.screen, c.unit), "trace": fmt(c.trace, c.unit), "ok": c.ok} for c in shot.checks])
    if shot.awaiting_text:  # the page reads these boxes and sends the text back
        d["fields"] = [{"name": name, "box": [int(v) for v in box]} for name, box in sd.text_fields(shot)]
    return d


def points(shot, coords):
    """The extracted points for the page's graph and table."""
    series = []
    for ch in shot.channels:
        if coords == "units" and not ch.ready:
            continue
        x, y, lo, hi = sd.trace_points(shot, ch, coords)
        sc = sd.scale_of(shot, ch.n)
        places = sd.y_decimals(shot, ch, coords)  # the same rounding the CSV files get
        there = ch.flag != 3  # columns where the trace exists
        rounded = lambda a: [round(float(v), places) for v in a[there]]
        series.append({"n": ch.n, "name": sd.trace_label(ch.n), "colour": ch.colour, "unit": sc["unit"] if coords == "units" else "px",
                       "x": [round(float(v), 12) for v in x[there]], "y": rounded(y), "lo": rounded(lo), "hi": rounded(hi)})
    return {"title": shot.stem, "x_unit": "s" if coords == "units" else "px", "series": series}


def csv_options(query):
    pick = lambda key, allowed: query.get(key) if query.get(key) in allowed else allowed[0]
    return SimpleNamespace(coords=pick("coords", ["units", "pixels"]), columns=pick("columns", ["xy", "full"]),
                           info=pick("info", ["none", "below", "above"]))


def _png(image):
    out = io.BytesIO()
    image.save(out, "PNG")
    return 200, "image/png", out.getvalue(), None


def handle(method, path, query, body):
    """Answer one request. query is a plain dict of strings, body is bytes.
    Returns (status, content type, payload, download filename or None); a dict payload is sent as JSON."""
    if method == "GET" and path == "/api/shots":
        return 200, "application/json", {"shots": [describe(s) for s in SHOTS.values()], "text_reader": STATE.text_ok}, None
    if method == "POST" and path == "/api/add":
        if len(body) > MAX_UPLOAD:
            return 413, "application/json", {"error": "That file is too large."}, None
        name = Path(query.get("name") or "image").name or "image"
        return 200, "application/json", {"shot": describe(add_shot(name, body)), "text_reader": STATE.text_ok}, None
    if method == "POST" and path == "/api/clear":
        SHOTS.clear()
        return 200, "application/json", {}, None
    if method == "GET" and path == "/api/summary.csv":
        return 200, "text/csv", sd.summary_csv(list(SHOTS.values())), "summary.csv"
    m = re.fullmatch(r"/api/shot/(\d+)/([\w.]+)", path)
    shot = SHOTS.get(int(m.group(1))) if m else None
    if not shot:
        return 404, "application/json", {"error": "Not found."}, None
    what = m.group(2)
    if method == "POST" and what == "set":
        for key, text in json.loads(body or b"{}").items():
            set_value(shot, key, text)
        return 200, "application/json", {"shot": describe(shot)}, None
    if method == "POST" and what == "texts":
        set_texts(shot, json.loads(body or b"{}").get("texts"))
        return 200, "application/json", {"shot": describe(shot)}, None
    if method == "POST" and what == "remove":
        del SHOTS[shot.id]
        return 200, "application/json", {}, None
    if method == "GET" and what == "strip.png":
        left, top = max(0, shot.geom.gx0 + 27), max(0, shot.geom.gy0 - 19)
        return _png(shot.image.crop((left, top, left + 740, top + 19)))
    if method == "GET" and what == "image.png":
        return _png(shot.image)
    if method == "GET" and what == "check.png":
        return _png(sd.check_picture(shot))
    if method == "GET" and what == "points":
        return 200, "application/json", points(shot, csv_options(query).coords), None
    if method == "GET" and what == "csv":
        opt = csv_options(query)
        ch = next((c for c in shot.channels if str(c.n) == query.get("ch")), None)
        if not ch or not (ch.ready or opt.coords == "pixels"):
            return 409, "application/json", {"error": "This trace needs its scales before it can be written in volts."}, None
        return 200, "text/csv", sd.trace_csv(shot, ch, opt), sd.trace_file(shot.stem, ch)
    return 404, "application/json", {"error": "Not found."}, None


def handle_json(method, path, query_json, body):
    """The same, shaped for the in-browser caller: everything comes back in one dict."""
    try:
        data = body.to_bytes() if hasattr(body, "to_bytes") else bytes(body or b"")  # a browser buffer, or plain bytes
        status, kind, payload, filename = handle(method, path, json.loads(query_json or "{}"), data)
    except Exception as err:  # the page shows the message
        status, kind, payload, filename = 500, "application/json", {"error": f"{type(err).__name__}: {err}"}, None
    out = {"status": status, "kind": kind, "filename": filename}
    if isinstance(payload, (bytes, bytearray)):
        out["bytes"] = bytes(payload)
    else:
        out["text"] = json.dumps(payload) if kind == "application/json" else str(payload)
    return out
