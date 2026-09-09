"""Build the standalone CAN bus page from a monitor capture.

usage: python build_can_bus_page.py <capture.txt> [<start HH:MM:SS> <end HH:MM:SS>] [<start_clock_for_first_frame_shown>]

Reads the candump-style capture, keeps the frames inside the window, and
writes them into can-bus-traffic.template.html as compact arrays, producing
can-bus-traffic.html next to it. The page decodes the frames itself.
"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
src = Path(sys.argv[1])
w0 = sys.argv[2] if len(sys.argv) > 2 else None
w1 = sys.argv[3] if len(sys.argv) > 3 else None
focus = sys.argv[4] if len(sys.argv) > 4 else None
rx = re.compile(r"^(\S+)\t(\S+ \S+)\t\(([\d.]+)\)\s+(\S+)\s+(\S+)\s+\[(\d+)\]\s*(.*)$")
frames = []
totals = {}
crx = re.compile(r"^# (\d\d:\d\d:\d\d) total: (\d+)")
with open(src, encoding="utf-8", errors="replace") as f:
    for line in f:
        c = crx.match(line)
        if c:
            if (not w0 or c.group(1) >= w0[:8]) and (not w1 or c.group(1) <= w1[:8]):
                totals[c.group(1)] = int(c.group(2))
            continue
        m = rx.match(line.rstrip("\n"))
        if not m:
            continue
        clock = m.group(2)[11:]
        if (w0 and clock < w0) or (w1 and clock >= w1):
            continue
        frames.append((int(round(float(m.group(3)) * 1e6)), clock, m.group(5), m.group(7).replace(" ", "")))
frames.sort()
ids = sorted({x[2] for x in frames})
idx = {c: i for i, c in enumerate(ids)}
start_index = 0
if focus:
    for i, x in enumerate(frames):
        if x[1] >= focus:
            start_index = i
            break
raw = {"ids": ids, "start_clock": frames[0][1], "start_index": start_index,
       "T": [x[0] for x in frames], "I": [idx[x[2]] for x in frames], "D": [x[3] for x in frames],
       "totals": totals or None}
tpl = (HERE / "can-bus-traffic.template.html").read_text(encoding="utf-8")
page = tpl.replace("/*DATA*/", "const RAW = " + json.dumps(raw, separators=(",", ":")) + ";", 1)
out = HERE / "can-bus-traffic.html"
out.write_text(page, encoding="utf-8")
print(f"{len(frames)} frames from {frames[0][1]} to {frames[-1][1]}, {out.stat().st_size / 1e6:.2f} MB -> {out.name}")
