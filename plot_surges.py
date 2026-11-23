#!/usr/bin/env python3
"""Daily expiry time-series with data-gap bands -> results/fig_surges.svg.

Shows txs first-expiring per calendar day (log scale). Calendar days with no
events in the extract are rendered as grey bands (collection gaps, not real
lulls) so they can't be misread as zero-expiry days.
"""
import sqlite3
import math
from datetime import date, timedelta

DB = "expiry.db"
c = sqlite3.connect(DB).cursor()
rows = {r[0]: r[1] for r in
        c.execute("SELECT date(first_expiry,'unixepoch'), COUNT(*) FROM tx GROUP BY 1")}

d0 = date.fromisoformat(min(rows))
d1 = date.fromisoformat(max(rows))
span = (d1 - d0).days + 1
days = [d0 + timedelta(i) for i in range(span)]
vals = [rows.get(d.isoformat(), 0) for d in days]

# contiguous runs of >=7 missing days -> grey "no data" bands
gaps = []
start = None
for i, v in enumerate(vals):
    if v == 0:
        if start is None:
            start = i
    else:
        if start is not None and i - start >= 7:
            gaps.append((start, i - 1))
        start = None
if start is not None and span - start >= 7:
    gaps.append((start, span - 1))

W, H, L, R, T, B = 1000, 380, 60, 20, 74, 52
pw, ph = W - L - R, H - T - B
INK, MUT, GRID, BLUE, GAP = "#222b36", "#8a97a5", "#eef1f4", "#3b73a8", "#e8ebee"
LOGMAX = 5  # y axis: 1 .. 10^5


def x(i):
    return L + pw * i / (span - 1)


def y(v):
    return T + ph - (math.log10(max(v, 1)) / LOGMAX) * ph


s = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
     f'font-family="-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">',
     f'<rect width="{W}" height="{H}" fill="#fff"/>',
     f'<text x="28" y="34" font-size="17" font-weight="600" fill="{INK}">Transactions expiring per day</text>',
     f'<text x="28" y="54" font-size="12.5" fill="{MUT}">log scale; grey = no data in the extract '
     f'(fleet/collection gaps, not real lulls)</text>']

for a, b in gaps:
    s.append(f'<rect x="{x(a):.1f}" y="{T}" width="{max(1, x(b) - x(a)):.1f}" height="{ph}" fill="{GAP}"/>')

for p in range(LOGMAX + 1):
    yy = T + ph - (p / LOGMAX) * ph
    lab = ["1", "10", "100", "1k", "10k", "100k"][p]
    s.append(f'<line x1="{L}" y1="{yy:.1f}" x2="{L+pw}" y2="{yy:.1f}" stroke="{GRID}"/>')
    s.append(f'<text x="{L-8}" y="{yy+4:.1f}" text-anchor="end" font-size="11" fill="{MUT}">{lab}</text>')

for i, v in enumerate(vals):
    if v > 0:
        s.append(f'<line x1="{x(i):.1f}" y1="{T+ph:.1f}" x2="{x(i):.1f}" y2="{y(v):.1f}" '
                 f'stroke="{BLUE}" stroke-width="1"/>')

cur = date(d0.year, d0.month, 1)
while cur <= d1:
    i = (cur - d0).days
    if 0 <= i < span:
        s.append(f'<text x="{x(i):.1f}" y="{T+ph+18:.1f}" text-anchor="middle" font-size="10" '
                 f'fill="{MUT}">{cur.strftime("%Y-%m")}</text>')
    m = cur.month + 3
    yv = cur.year + (m - 1) // 12
    cur = date(yv, (m - 1) % 12 + 1, 1)

s.append("</svg>")
open("results/fig_surges.svg", "w").write("\n".join(s))
print("wrote results/fig_surges.svg")
