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

# ---- SVG: rounded stacked bars, one per day ----
ORDER = ["regular", "rune", "inscription", "data"]
COL = {"regular": "#5aab82", "rune": "#e0a44e", "inscription": "#d06a6a", "data": "#8192ad"}
NICE = {"regular": "regular payment", "rune": "rune", "inscription": "inscription", "data": "other data"}
FONT = "-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif"
INK, MUT = "#222b36", "#8a97a5"
L, R, T, ROW, BH = 170, 90, 90, 46, 28
W = 960
bw = W - L - R
H = T + ROW * len(results) + 60

s = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" font-family="{FONT}">',
     f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
     f'<defs>']
for i in range(len(results)):
    yy = T + i * ROW + (ROW - BH) / 2
    s.append(f'<clipPath id="b{i}"><rect x="{L}" y="{yy:.1f}" width="{bw}" height="{BH}" rx="7"/></clipPath>')
s.append('</defs>')
s.append(f'<text x="30" y="36" font-size="18" font-weight="600" fill="{INK}">What kind of transactions expire?</text>')
s.append(f'<text x="30" y="58" font-size="13" fill="{MUT}">share by type, for the expired txs that were later mined</text>')

for i, (label, n, cats) in enumerate(results):
    yy = T + i * ROW + (ROW - BH) / 2
    name = "all expired" if label.startswith("baseline") else label
    weight = "600" if name == "all expired" else "400"
    s.append(f'<text x="{L-14}" y="{yy+BH/2+4.5:.1f}" text-anchor="end" font-size="13" '
             f'font-weight="{weight}" fill="{INK}">{name}</text>')
    # track + segments, clipped to a rounded rect
    s.append(f'<g clip-path="url(#b{i})">')
    s.append(f'<rect x="{L}" y="{yy:.1f}" width="{bw}" height="{BH}" fill="#eef1f4"/>')
    x = L
    for k in ORDER:
        w = bw * cats[k] / max(n, 1)
        if w <= 0:
            continue
        s.append(f'<rect x="{x:.2f}" y="{yy:.1f}" width="{w:.2f}" height="{BH}" fill="{COL[k]}"/>')
        pct = 100 * cats[k] / max(n, 1)
        if w > 30:
            s.append(f'<text x="{x+w/2:.1f}" y="{yy+BH/2+4.5:.1f}" text-anchor="middle" '
                     f'font-size="11.5" fill="#ffffff">{pct:.0f}%</text>')
        x += w
    # thin white separators between segments
    x = L
    for k in ORDER[:-1]:
        x += bw * cats[k] / max(n, 1)
        if 0 < x - L < bw:
            s.append(f'<rect x="{x-0.9:.2f}" y="{yy:.1f}" width="1.8" height="{BH}" fill="#ffffff"/>')
    s.append('</g>')

# legend
lx = L
for k in ORDER:
    s.append(f'<rect x="{lx}" y="{H-30}" width="13" height="13" rx="3" fill="{COL[k]}"/>')
    s.append(f'<text x="{lx+19}" y="{H-19}" font-size="12.5" fill="{INK}">{NICE[k]}</text>')
    lx += 160
s.append("</svg>")
open("results/fig_types.svg", "w").write("\n".join(s))
print("wrote results/fig_types.svg")
