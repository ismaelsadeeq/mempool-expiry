#!/usr/bin/env python3
"""Classify expired (and later-confirmed) txs as inscription / rune / data / regular.

Only confirmed txids are classifiable: getrawtransaction needs the block hash
(no txindex). Fetches a sample per surge day plus an overall baseline, inspects
raw tx structure, and writes results/fig_types.svg.

Heuristics (lowercase hex):
  inscription : an input witness contains the ordinals envelope 0063036f7264
                (OP_FALSE OP_IF push"ord")
  rune        : an output scriptPubKey starts 6a5d (OP_RETURN OP_PUSHNUM_13)
  data        : any other OP_RETURN output (scriptPubKey starts 6a)
  regular     : everything else
Priority: inscription > rune > data > regular.
"""
import sqlite3, os, base64, json, urllib.request, time
from concurrent.futures import ThreadPoolExecutor

DATADIR = "/data/bitcoin"
DB = "expiry.db"
PER_DAY = 500
BASELINE = 4000
DAYS = ["2025-09-30", "2026-05-24", "2025-09-10", "2025-01-23",
        "2025-01-12", "2026-08-23"]  # last = nico artifact day


class RPC:
    def __init__(self, datadir):
        cookie = open(os.path.join(datadir, ".cookie")).read().strip()
        self.auth = base64.b64encode(cookie.encode()).decode()
        self.url = "http://127.0.0.1:8332"

    def call(self, method, params):
        req = urllib.request.Request(
            self.url, data=json.dumps({"jsonrpc": "1.0", "id": "x",
                                       "method": method, "params": params}).encode(),
            headers={"Authorization": "Basic " + self.auth, "Content-Type": "text/plain"})
        for a in range(4):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    j = json.load(r)
                if j.get("error"):
                    return None
                return j["result"]
            except Exception:
                if a == 3:
                    return None
                time.sleep(0.3 * (a + 1))


rpc = RPC(DATADIR)
c = sqlite3.connect(DB).cursor()


def classify(tx):
    if tx is None:
        return None
    for vin in tx.get("vin", []):
        for w in vin.get("txinwitness", []) or []:
            if "0063036f7264" in w:
                return "inscription"
    rune = False
    data = False
    for vout in tx.get("vout", []):
        h = vout.get("scriptPubKey", {}).get("hex", "")
        if h.startswith("6a5d"):
            rune = True
        elif h.startswith("6a"):
            data = True
    if rune:
        return "rune"
    if data:
        return "data"
    return "regular"


def fetch(row):
    txid, bh = row
    return classify(rpc.call("getrawtransaction", [txid, True, bh]))


def tally(rows, label):
    cats = {"inscription": 0, "rune": 0, "data": 0, "regular": 0, "err": 0}
    with ThreadPoolExecutor(max_workers=12) as ex:
        for r in ex.map(fetch, rows):
            cats[r if r else "err"] += 1
    n = sum(cats[k] for k in cats if k != "err")
    print(f"{label:14} n={n:5}  "
          + "  ".join(f"{k}={cats[k]} ({100*cats[k]/max(n,1):.0f}%)"
                      for k in ("inscription", "rune", "data", "regular")))
    return label, n, cats


CACHE = "results/types_cache.json"
if os.path.exists(CACHE):
    results = [(l, n, cats) for l, n, cats in json.load(open(CACHE))]
    for l, n, cats in results:
        print(f"{l:14} n={n:5}  (cached)")
else:
    results = []
    for d in DAYS:
        rows = c.execute(
            "SELECT c.txid,c.blockhash FROM tx t JOIN confirmation c ON c.txid=t.txid "
            "WHERE date(t.first_expiry,'unixepoch')=? LIMIT ?", (d, PER_DAY)).fetchall()
        results.append(tally(rows, d))
    base = c.execute(
        "SELECT txid,blockhash FROM confirmation ORDER BY RANDOM() LIMIT ?", (BASELINE,)).fetchall()
    results.append(tally(base, "baseline(all)"))
    json.dump(results, open(CACHE, "w"))

# ---- SVG: horizontal stacked bars, one per day ----
COL = {"inscription": "#c44e52", "rune": "#dd8452", "data": "#8172b3", "regular": "#55a868"}
L, R, T = 150, 24, 70
W = 920
H = T + 46 * len(results) + 44
bw = W - L - R
s = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
     f'font-family="-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">',
     f'<rect width="{W}" height="{H}" fill="#fff"/>',
     f'<text x="28" y="34" font-size="17" font-weight="600" fill="#222b36">What are the expired transactions?</text>',
     f'<text x="28" y="54" font-size="12.5" fill="#8a97a5">share by type, confirmed txs only (ordinals / runes / other OP_RETURN / regular)</text>']
y = T
for label, n, cats in results:
    x = L
    s.append(f'<text x="{L-10}" y="{y+22:.0f}" text-anchor="end" font-size="12.5" fill="#222b36">{label}</text>')
    for k in ("inscription", "rune", "data", "regular"):
        w = bw * cats[k] / max(n, 1)
        if w > 0:
            s.append(f'<rect x="{x:.1f}" y="{y}" width="{w:.1f}" height="30" fill="{COL[k]}"/>')
            if w > 34:
                s.append(f'<text x="{x+w/2:.1f}" y="{y+20}" text-anchor="middle" font-size="11" '
                         f'fill="#fff">{100*cats[k]/max(n,1):.0f}%</text>')
            x += w
    y += 46
lx = L
for k in ("inscription", "rune", "data", "regular"):
    s.append(f'<rect x="{lx}" y="{H-26}" width="13" height="13" fill="{COL[k]}"/>')
    s.append(f'<text x="{lx+18}" y="{H-15}" font-size="11.5" fill="#222b36">{k}</text>')
    lx += 150
s.append("</svg>")
open("results/fig_types.svg", "w").write("\n".join(s))
print("wrote results/fig_types.svg")
