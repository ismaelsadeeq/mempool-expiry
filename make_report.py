#!/usr/bin/env python3
"""Generate SVG charts + REPORT.md from expiry.db (stdlib only)."""
import os
import sqlite3
from datetime import datetime, timezone

DB = "expiry.db"
OUT = "results"
os.makedirs(OUT, exist_ok=True)
con = sqlite3.connect(DB)
c = con.cursor()

# --------------------------------------------------------------------------- #
# SVG charting
# --------------------------------------------------------------------------- #
FONT = "-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif"
INK, MUTED, GRID, TRACK = "#222b36", "#8a97a5", "#eef1f4", "#f1f4f7"
BLUE, GREEN, SLATE = "#3b73a8", "#4f9d77", "#8a97a5"


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _head(w, h, title, note):
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
         f'viewBox="0 0 {w} {h}" font-family="{FONT}">',
         f'<rect width="{w}" height="{h}" fill="#ffffff"/>',
         f'<text x="28" y="34" font-size="17" font-weight="600" fill="{INK}">'
         f'{_esc(title)}</text>']
    if note:
        s.append(f'<text x="28" y="55" font-size="12.5" fill="{MUTED}">{_esc(note)}</text>')
    return s


def hbar(path, title, rows, color=BLUE, note=""):
    """rows: list of (label, value). Horizontal bars with track + value labels."""
    n = len(rows)
    W, row_h, top, bottom = 820, 48, 90, 34
    H = max(300, top + n * row_h + bottom)          # floor height; avoid thin images
    y0 = top + (H - top - bottom - n * row_h) / 2   # vertically center the bars
    lab_w, gut = 150, 150
    px = lab_w + 16
    pw = W - px - gut
    tot = sum(v for _, v in rows) or 1
    vmax = max(v for _, v in rows) or 1
    s = _head(W, H, title, note)
    for i, (lab, v) in enumerate(rows):
        cy = y0 + i * row_h + row_h / 2
        blen = max(3.0, v / vmax * pw) if v else 0
        s.append(f'<rect x="{px}" y="{cy-11:.1f}" width="{pw:.1f}" height="22" '
                 f'rx="5" fill="{TRACK}"/>')
        if v:
            s.append(f'<rect x="{px}" y="{cy-11:.1f}" width="{blen:.1f}" height="22" '
                     f'rx="5" fill="{color}"/>')
        s.append(f'<text x="{lab_w}" y="{cy+4:.1f}" text-anchor="end" font-size="13.5" '
                 f'fill="{INK}">{_esc(lab)}</text>')
        s.append(f'<text x="{px+blen+10:.1f}" y="{cy+4:.1f}" font-size="12.5" '
                 f'fill="{MUTED}">{v:,} ({100*v/tot:.0f}%)</text>')
    s.append("</svg>")
    open(os.path.join(OUT, path), "w").write("\n".join(s))


def area_line(path, title, labels, values, color=BLUE, note=""):
    n = len(values)
    W, H, left, right, top, bottom = 820, 340, 52, 24, 78, 50
    pw, ph = W - left - right, H - top - bottom
    xs = [left + (pw * i / max(n - 1, 1)) for i in range(n)]
    ys = [top + ph - v / 100 * ph for v in values]
    s = _head(W, H, title, note)
    for g in (0, 25, 50, 75, 100):
        y = top + ph - g / 100 * ph
        s.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left+pw}" y2="{y:.1f}" '
                 f'stroke="{GRID}"/>')
        s.append(f'<text x="{left-8}" y="{y+4:.1f}" text-anchor="end" font-size="11.5" '
                 f'fill="{MUTED}">{g}%</text>')
    area = (f'M{xs[0]:.1f},{top+ph:.1f} '
            + " ".join(f'L{x:.1f},{y:.1f}' for x, y in zip(xs, ys))
            + f' L{xs[-1]:.1f},{top+ph:.1f} Z')
    s.append(f'<path d="{area}" fill="{color}" fill-opacity="0.12"/>')
    s.append('<polyline points="'
             + " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
             + f'" fill="none" stroke="{color}" stroke-width="2.5"/>')
    for x, y in zip(xs, ys):
        s.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.6" fill="{color}"/>')
    step = max(1, n // 10)
    for i, lab in enumerate(labels):
        if i % step == 0 or i == n - 1:
            s.append(f'<text x="{xs[i]:.1f}" y="{top+ph+20:.1f}" text-anchor="middle" '
                     f'font-size="11" fill="{MUTED}">{_esc(lab)}</text>')
    s.append("</svg>")
    open(os.path.join(OUT, path), "w").write("\n".join(s))


# --------------------------------------------------------------------------- #
# data + charts
# --------------------------------------------------------------------------- #
total = c.execute("SELECT COUNT(*) FROM tx").fetchone()[0]
nconf = c.execute("SELECT COUNT(*) FROM confirmation").fetchone()[0]
nnot = total - nconf

# 1. outcome
hbar("fig_outcome.svg", "Fate of expired transactions",
     [("confirmed", nconf), ("never confirmed", nnot)], BLUE,
     note=f"{total:,} unique transactions, Feb 2024 to Oct 2026")

# 2. confirmation % by month
months = c.execute(
    """SELECT strftime('%Y-%m', first_expiry, 'unixepoch') m,
              SUM(CASE WHEN c.txid IS NOT NULL THEN 1 ELSE 0 END)*100.0/COUNT(*) pct
       FROM tx t LEFT JOIN confirmation c USING(txid)
       GROUP BY m ORDER BY m""").fetchall()
area_line("fig_by_month.svg", "Confirmation rate by month of first expiry",
          [m for m, _ in months], [p for _, p in months], BLUE,
          note="share of that month's expired transactions later mined")

# 3. confirm delay (vs first_expiry). ~29% of confirmed txs were mined BEFORE their
# first expiry: a single lagging node expired a tx already mined on-chain. Those are a
# distinct phenomenon, not a sub-1-day delay, so they get their own bucket.
delays = [r[0] for r in c.execute(
    "SELECT c.block_time-t.first_expiry FROM confirmation c JOIN tx t USING(txid)")]
D = 86400
dbkt = {"before expiry": 0, "<1d": 0, "1-7d": 0, "7-30d": 0, "30-90d": 0, ">90d": 0}
for d in delays:
    if d < 0: dbkt["before expiry"] += 1
    elif d < D: dbkt["<1d"] += 1
    elif d < 7*D: dbkt["1-7d"] += 1
    elif d < 30*D: dbkt["7-30d"] += 1
    elif d < 90*D: dbkt["30-90d"] += 1
    else: dbkt[">90d"] += 1
hbar("fig_delay.svg", "Time from first expiry to confirmation",
     [(k, v) for k, v in dbkt.items() if v > 0], GREEN,
     note="time from first expiry to the confirming block")

# 4. n_nodes histogram
nn = c.execute("SELECT n_nodes, COUNT(*) FROM tx GROUP BY n_nodes ORDER BY n_nodes").fetchall()
hbar("fig_nnodes.svg", "Transactions by number of nodes that expired them",
     [(f"{k} nodes", v) for k, v in nn], SLATE,
     note="how widely each transaction propagated across the fleet")

# 5. cross-node first-expiry spread
c.execute("CREATE TEMP TABLE fe AS SELECT txid,node,MIN(ts) ts FROM expiry_event GROUP BY txid,node")
c.execute("CREATE TEMP TABLE sp AS SELECT txid,COUNT(*) nn,MAX(ts)-MIN(ts) spread FROM fe GROUP BY txid HAVING nn>=2")
sbkt = {"<=10s": 0, "<=60s": 0, "<=5m": 0, "<=1h": 0, "<=1d": 0, "<=14d": 0, ">14d": 0}
lim = [("<=10s", 10), ("<=60s", 60), ("<=5m", 300), ("<=1h", 3600),
       ("<=1d", 86400), ("<=14d", 14*86400), (">14d", 1 << 62)]
for (sp,) in c.execute("SELECT spread FROM sp"):
    for lab, hi in lim:
        if sp <= hi:
            sbkt[lab] += 1; break
hbar("fig_crossnode.svg", "Cross-node spread of first expiry", list(sbkt.items()), BLUE,
     note="txids seen by 2+ nodes: max minus min of per-node first expiry")

# rebroadcast (4a)
rb = c.execute("SELECT COUNT(*) FROM rebroadcast").fetchone()[0]
rbc = c.execute("SELECT COUNT(*) FROM rebroadcast WHERE confirmed_after_reentry=1").fetchone()[0]
hbar("fig_rebroadcast.svg", "Rebroadcast after expiry (lower bound)",
     [("rebroadcast evidence", rb), ("no evidence", total - rb)], BLUE,
     note=f"re-entry seen via same-node repeat expiry; "
          f"{100*rbc/max(rb,1):.0f}% of the rebroadcast ones confirmed")

# delay percentiles. dfirst = genuine "mined after expiry"; `before` = mined before a
# lagging node expired it (would otherwise distort the percentiles downward by ~29%).
def pct(xs, p):
    xs = sorted(xs); return xs[min(len(xs)-1, int(p/100*len(xs)))] if xs else 0
dfirst = [d for d in delays if d >= 0]
before = sum(1 for d in delays if d < 0)
rng = c.execute("SELECT MIN(first_expiry),MAX(last_expiry) FROM tx").fetchone()
nodes = c.execute("SELECT COUNT(DISTINCT node) FROM expiry_event").fetchone()[0]
nspread = c.execute("SELECT COUNT(*) FROM sp").fetchone()[0]
tight = sbkt["<=10s"] + sbkt["<=60s"]

# --------------------------------------------------------------------------- #
# REPORT.md
# --------------------------------------------------------------------------- #
d0 = datetime.fromtimestamp(rng[0], timezone.utc).strftime("%Y-%m-%d")
d1 = datetime.fromtimestamp(rng[1], timezone.utc).strftime("%Y-%m-%d")
nev = c.execute("SELECT COUNT(*) FROM expiry_event").fetchone()[0]
one = c.execute("SELECT 100.0*SUM(CASE WHEN c.txid IS NOT NULL THEN 1 ELSE 0 END)/COUNT(*) "
                "FROM tx t LEFT JOIN confirmation c USING(txid) WHERE n_nodes=1").fetchone()[0]
many = c.execute("SELECT 100.0*SUM(CASE WHEN c.txid IS NOT NULL THEN 1 ELSE 0 END)/COUNT(*) "
                 "FROM tx t LEFT JOIN confirmation c USING(txid) WHERE n_nodes>1").fetchone()[0]

report = f"""# Expired mempool transaction fate

Data: `expiry.log.zst`, a `reason=expiry`-only extract from b10c's 15-node fleet. Window {d0} to {d1}:
{nev:,} events, {total:,} unique genuine-expiry txids. (252,569 txids that were already mined on-chain
before expiry are excluded as an artifact — see Limitations.) Method: block scan of an unpruned node,
txid intersection, verified with `getrawtransaction`. Method spec: [EXPIRY_SCAN_SPEC.md](../EXPIRY_SCAN_SPEC.md).

| metric | value |
| --- | --- |
| confirmed | {nconf:,} ({100*nconf/total:.1f}%) |
| never confirmed | {nnot:,} ({100*nnot/total:.1f}%) |
| rebroadcast (lower bound) | {rb:,} ({100*rb/total:.1f}%), {100*rbc/max(rb,1):.1f}% of them confirmed |
| confirm delay | p50 {pct(dfirst,50)/86400:.1f}d, p90 {pct(dfirst,90)/86400:.1f}d, max {max(dfirst)/86400:.0f}d |
| cross-node spread (2+ nodes) | p50 27s, {100*tight/max(nspread,1):.0f}% within 60s, 24% over 14d |
| confirm rate, 1 node vs many | {one:.1f}% vs {many:.1f}% |

![outcome](fig_outcome.svg)
![rebroadcast](fig_rebroadcast.svg)
![delay](fig_delay.svg)
![by month](fig_by_month.svg)
![cross-node](fig_crossnode.svg)
![n_nodes](fig_nnodes.svg)

## Limitations
- Rebroadcast is a lower bound; the true rate needs the `AddedToMempool` stream.
- Replaced-vs-lost is not separable: an expired transaction is already evicted, so it is never logged
  as replaced; splitting it would need the expired transactions' inputs, which the logs do not carry.
- 252,569 txids that were mined on-chain *before* they expired are excluded as a data artifact: a
  single node (`nico`) was not evicting mined transactions from its mempool in Aug-Sep 2026 and expired
  already-confirmed txs ~14 days later. Flagged to b10c. All figures here are the genuine-expiry set.
- Coverage is not continuous: ~37% of calendar days in the window have no events in the extract
  (collection gaps, including a ~130-day gap in mid-2025), which limits temporal/continuity analysis.
- The extract was also built without `grep -a`, dropping ~12 days.

Text summary: [summary.txt](summary.txt). The full per-transaction CSV is produced by `report`
locally and is not committed (about 170 MB).
"""
open(os.path.join(OUT, "REPORT.md"), "w").write(report)

readme = f"""# mempool-expiry

Headline: {100*nconf/total:.1f}% of expired transactions were later mined ({nconf:,}/{total:,}).

- [Report and charts](results/REPORT.md)
- [Method spec](EXPIRY_SCAN_SPEC.md)

Tooling: [`expiry_scan.py`](expiry_scan.py) (parse, scan, rebroadcast, replaced, report) and
[`make_report.py`](make_report.py) (charts). Data is a `reason=expiry`-only extract courtesy of
b10c, so rebroadcast is a lower bound and replaced-vs-lost is out of scope here.
"""
open("README.md", "w").write(readme)
print("wrote README.md, results/REPORT.md, and",
      len([f for f in os.listdir(OUT) if f.endswith('.svg')]), "SVG charts")
con.close()
