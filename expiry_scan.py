#!/usr/bin/env python3
"""expiry_scan.py — fate of mempool txs evicted with reason=expiry.

Phases (see EXPIRY_SCAN_SPEC.md):
  parse        stream expiry.log.zst  -> expiry_event, tx
  scan         RPC block scan (node stays up) -> confirmation, progress
  parse-added  stream added.log.zst   -> added_event, tx_added   (Phase 4b)
  rebroadcast  classify re-entry (4a lower bound + 4b true count) -> rebroadcast
  report       join everything        -> results.csv, summary.txt

stdlib only. Confirmation is a read-only RPC block scan against the running node
(node stays up; unpruned, no txindex needed).
"""
import argparse
import base64
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
# The timestamp is not always the token right after the node: some lines carry a
# marker like `[*]` first (e.g. `...zst:bob [*] 2025-09-12T...`). Anchor on the
# ISO-8601 timestamp itself and allow arbitrary tokens between node and ts.
_TS = r"(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)"
REMOVED_RE = re.compile(
    r"^(?P<file>[^:]+):(?P<node>\S+) .*?" + _TS + r" .*"
    r"TransactionRemovedFromMempool: txid=(?P<txid>[0-9a-f]{64}) "
    r"wtxid=(?P<wtxid>[0-9a-f]{64}) reason=(?P<reason>\S+)"
)
ADDED_RE = re.compile(
    r"^(?P<file>[^:]+):(?P<node>\S+) .*?" + _TS + r" .*"
    r"TransactionAddedToMempool: txid=(?P<txid>[0-9a-f]{64}) "
    r"wtxid=(?P<wtxid>[0-9a-f]{64})"
)
# RBF replacement (BCLog::MEMPOOL, validation.cpp:1189-1214):
#   "replacing mempool tx <old> (wtxid=..). New tx <new> (wtxid=..)"  (single-tx)
#   "replacing mempool tx <old> (wtxid=..). New package <hash> with N txs ..."  (package)
# Tolerant: capture old txid, then either a New tx txid or a New package hash.
REPLACE_RE = re.compile(
    r"^(?P<file>[^:]+):(?P<node>\S+) .*?" + _TS + r" .*?"
    r"replacing mempool tx (?P<old>[0-9a-f]{64}).*?"
    r"New (?:tx (?P<new>[0-9a-f]{64})|package (?P<pkg>[0-9a-f]{64}))"
)


def parse_ts(s):
    """'2024-02-16T18:48:54Z' or '2026-10-14T23:50:05.262411Z' -> epoch int (UTC)."""
    s = s.rstrip("Z")
    if "." in s:
        s = s.split(".", 1)[0]
    dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def zstd_lines(path):
    """Stream `zstd -dc path` line by line without buffering the whole file."""
    p = subprocess.Popen(
        ["zstd", "-dc", path], stdout=subprocess.PIPE, bufsize=1024 * 1024
    )
    try:
        for raw in p.stdout:
            yield raw.decode("utf-8", "replace").rstrip("\n")
    finally:
        p.stdout.close()
        p.wait()


# --------------------------------------------------------------------------- #
# db
# --------------------------------------------------------------------------- #
def connect(db):
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def init_schema(con):
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS expiry_event(
            node TEXT, txid TEXT, wtxid TEXT, ts INTEGER);
        CREATE TABLE IF NOT EXISTS tx(
            txid TEXT PRIMARY KEY, wtxid TEXT,
            first_expiry INTEGER, last_expiry INTEGER,
            n_events INTEGER, n_nodes INTEGER);
        CREATE TABLE IF NOT EXISTS confirmation(
            txid TEXT PRIMARY KEY, height INTEGER,
            blockhash TEXT, block_time INTEGER);
        CREATE TABLE IF NOT EXISTS progress(
            key TEXT PRIMARY KEY, value INTEGER);
        CREATE TABLE IF NOT EXISTS added_event(
            node TEXT, txid TEXT, wtxid TEXT, ts INTEGER);
        CREATE TABLE IF NOT EXISTS tx_added(
            txid TEXT PRIMARY KEY,
            first_added INTEGER, last_added INTEGER,
            n_added INTEGER, n_nodes_added INTEGER);
        CREATE TABLE IF NOT EXISTS rebroadcast(
            txid TEXT PRIMARY KEY, reentered INTEGER, n_rounds INTEGER,
            first_reentry INTEGER, confirmed_after_reentry INTEGER);
        CREATE TABLE IF NOT EXISTS replace_event(
            node TEXT, old_txid TEXT, new_txid TEXT, ts INTEGER, is_package INTEGER);
        CREATE TABLE IF NOT EXISTS replace_edge(
            old_txid TEXT, new_txid TEXT, first_ts INTEGER, last_ts INTEGER,
            n_nodes INTEGER, is_package INTEGER,
            PRIMARY KEY(old_txid, new_txid));
        CREATE TABLE IF NOT EXISTS replaced_fate(
            txid TEXT PRIMARY KEY, class TEXT,
            settle_txid TEXT, settle_height INTEGER, settle_block_time INTEGER,
            cascade_depth INTEGER, n_replacements INTEGER);
        """
    )
    con.commit()


# --------------------------------------------------------------------------- #
# phase 1: parse
# --------------------------------------------------------------------------- #
def cmd_parse(args):
    con = connect(args.db)
    init_schema(con)
    con.execute("DELETE FROM expiry_event")
    con.execute("DELETE FROM tx")
    con.commit()

    total = kept = dup = unparsed = 0
    other_reasons = {}
    unparsed_samples = []
    binmatch_files = []  # source files grep dropped as "binary file matches"
    batch = []
    BATCH = 100_000
    t0 = time.time()

    def flush():
        con.executemany(
            "INSERT INTO expiry_event(node,txid,wtxid,ts) VALUES (?,?,?,?)", batch
        )
        con.commit()
        batch.clear()

    for line in zstd_lines(args.log):
        total += 1
        if "TransactionRemovedFromMempool" not in line:
            # grep's "Binary file <f> matches" sentinel: a NUL byte made the
            # original extraction suppress this file's lines -> DATA LOSS upstream.
            if "binary file matches" in line.lower():
                binmatch_files.append(line.split(":", 1)[0])
            unparsed += 1
            if len(unparsed_samples) < 5:
                unparsed_samples.append(line)
            continue
        if "Enqueuing TransactionRemovedFromMempool" in line:  # old-format dup
            dup += 1
            continue
        m = REMOVED_RE.match(line)
        if not m:
            unparsed += 1
            if len(unparsed_samples) < 5:
                unparsed_samples.append(line)
            continue
        if m["reason"] != "expiry":
            other_reasons[m["reason"]] = other_reasons.get(m["reason"], 0) + 1
            continue
        batch.append((m["node"], m["txid"], m["wtxid"], parse_ts(m["ts"])))
        kept += 1
        if len(batch) >= BATCH:
            flush()
        if total % 2_000_000 == 0:
            print(f"  ...{total:,} lines ({time.time()-t0:.0f}s)", file=sys.stderr)
    if batch:
        flush()

    print("Aggregating tx ...", file=sys.stderr)
    con.execute("DELETE FROM tx")
    con.execute(
        """INSERT INTO tx
           SELECT txid, MIN(wtxid), MIN(ts), MAX(ts),
                  COUNT(*), COUNT(DISTINCT node)
           FROM expiry_event GROUP BY txid"""
    )
    con.commit()

    uniq = con.execute("SELECT COUNT(*) FROM tx").fetchone()[0]
    rng = con.execute("SELECT MIN(first_expiry), MAX(last_expiry) FROM tx").fetchone()
    per_node = con.execute(
        "SELECT node, COUNT(*) FROM expiry_event GROUP BY node ORDER BY 2 DESC"
    ).fetchall()

    print(f"\ntotal lines          {total:,}")
    print(f"skipped Enqueuing    {dup:,}")
    print(f"kept expiry events   {kept:,}")
    print(f"unique txids         {uniq:,}")
    print(f"unparseable lines    {unparsed:,}")
    if other_reasons:
        print("other reasons (not expiry):")
        for r, c in sorted(other_reasons.items(), key=lambda x: -x[1]):
            print(f"  {r:20} {c:,}")
    print("per-node expiry events:")
    for n, c in per_node:
        print(f"  {n:12} {c:,}")
    if rng[0]:
        lo = datetime.fromtimestamp(rng[0], timezone.utc)
        hi = datetime.fromtimestamp(rng[1], timezone.utc)
        print(f"date range           {lo:%Y-%m-%d} .. {hi:%Y-%m-%d}")
    if unparsed_samples:
        print("first unparseable samples:")
        for s in unparsed_samples:
            print(f"  {s[:120]}")
    if binmatch_files:
        print("\n" + "!" * 70)
        print(f"WARNING: {len(binmatch_files)} source files were DROPPED by grep as "
              f"'binary file matches' during the original extraction —")
        print("their expiry events are MISSING from expiry.log.zst. Re-extract these "
              "with `grep -a` (text mode) from the combined logs:")
        for f in binmatch_files:
            print(f"  {f}")
        print("!" * 70)
    con.close()


# --------------------------------------------------------------------------- #
# phase 4b: parse-added (consumes combined-log-derived added.log.zst)
# --------------------------------------------------------------------------- #
def cmd_parse_added(args):
    con = connect(args.db)
    init_schema(con)
    con.execute("DELETE FROM added_event")
    con.execute("DELETE FROM tx_added")
    con.commit()

    total = kept = dup = unparsed = 0
    batch = []
    BATCH = 100_000

    def flush():
        con.executemany(
            "INSERT INTO added_event(node,txid,wtxid,ts) VALUES (?,?,?,?)", batch
        )
        con.commit()
        batch.clear()

    for line in zstd_lines(args.log):
        total += 1
        if "TransactionAddedToMempool" not in line:
            continue
        if "Enqueuing TransactionAddedToMempool" in line:
            dup += 1
            continue
        m = ADDED_RE.match(line)
        if not m:
            unparsed += 1
            continue
        batch.append((m["node"], m["txid"], m["wtxid"], parse_ts(m["ts"])))
        kept += 1
        if len(batch) >= BATCH:
            flush()
    if batch:
        flush()

    con.execute(
        """INSERT INTO tx_added
           SELECT txid, MIN(ts), MAX(ts), COUNT(*), COUNT(DISTINCT node)
           FROM added_event GROUP BY txid"""
    )
    con.commit()
    uniq = con.execute("SELECT COUNT(*) FROM tx_added").fetchone()[0]
    print(f"total lines {total:,}  kept adds {kept:,}  "
          f"skipped Enqueuing {dup:,}  unparsed {unparsed:,}  unique txids {uniq:,}")
    con.close()


# --------------------------------------------------------------------------- #
# phase 5: parse-replaced (RBF replacement edges, old->new)
# --------------------------------------------------------------------------- #
def cmd_parse_replaced(args):
    con = connect(args.db)
    init_schema(con)
    con.execute("DELETE FROM replace_event")
    con.execute("DELETE FROM replace_edge")
    con.commit()

    total = kept = pkg = unparsed = 0
    binmatch_files = []
    batch = []
    BATCH = 100_000

    def flush():
        con.executemany(
            "INSERT INTO replace_event(node,old_txid,new_txid,ts,is_package) "
            "VALUES (?,?,?,?,?)", batch)
        con.commit()
        batch.clear()

    for line in zstd_lines(args.log):
        total += 1
        if "replacing mempool tx" not in line:
            if "binary file matches" in line.lower():
                binmatch_files.append(line.split(":", 1)[0])
            unparsed += 1
            continue
        m = REPLACE_RE.match(line)
        if not m:
            unparsed += 1
            continue
        if m["new"]:
            batch.append((m["node"], m["old"], m["new"], parse_ts(m["ts"]), 0))
        else:                                   # New package <hash>: unresolved
            pkg += 1
            batch.append((m["node"], m["old"], m["pkg"], parse_ts(m["ts"]), 1))
        kept += 1
        if len(batch) >= BATCH:
            flush()
    if batch:
        flush()

    # dedup to edges across nodes, keeping node count + ts span
    con.execute(
        """INSERT INTO replace_edge
           SELECT old_txid, new_txid, MIN(ts), MAX(ts),
                  COUNT(DISTINCT node), MAX(is_package)
           FROM replace_event GROUP BY old_txid, new_txid""")
    con.commit()
    n_edges = con.execute("SELECT COUNT(*) FROM replace_edge").fetchone()[0]
    n_old = con.execute("SELECT COUNT(DISTINCT old_txid) FROM replace_edge").fetchone()[0]
    multi = con.execute(
        "SELECT COUNT(*) FROM (SELECT old_txid FROM replace_edge "
        "GROUP BY old_txid HAVING COUNT(*)>1)").fetchone()[0]
    print(f"total lines      {total:,}")
    print(f"kept events      {kept:,}  (package/unresolved {pkg:,})")
    print(f"unparsed         {unparsed:,}")
    print(f"unique edges     {n_edges:,}  (old->new pairs)")
    print(f"distinct old txs {n_old:,}  (of which {multi:,} replaced by >1 new tx — cross-node divergence)")
    if binmatch_files:
        print("\n" + "!"*70)
        print(f"WARNING: {len(binmatch_files)} source files dropped as 'binary file matches' "
              "— re-extract with grep -a. Missing:")
        for f in binmatch_files:
            print(f"  {f}")
        print("!"*70)
    con.close()


# --------------------------------------------------------------------------- #
# RPC
# --------------------------------------------------------------------------- #
class RPC:
    def __init__(self, datadir, host="127.0.0.1", port=8332):
        cookie = open(os.path.join(datadir, ".cookie")).read().strip()
        self.auth = base64.b64encode(cookie.encode()).decode()
        self.url = f"http://{host}:{port}"

    def _post(self, payload):
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode(),
            headers={"Authorization": "Basic " + self.auth,
                     "Content-Type": "text/plain"},
        )
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return json.load(r)
            except Exception as e:
                if attempt == 4:
                    raise
                time.sleep(0.5 * (attempt + 1))

    def call(self, method, params=None):
        r = self._post({"jsonrpc": "1.0", "id": "x",
                        "method": method, "params": params or []})
        if r.get("error"):
            raise RuntimeError(f"{method} {params}: {r['error']}")
        return r["result"]

    def batch(self, calls):
        """calls = [(method, params), ...] -> list of results (order preserved)."""
        payload = [{"jsonrpc": "1.0", "id": i, "method": m, "params": p}
                   for i, (m, p) in enumerate(calls)]
        out = self._post(payload)
        out.sort(key=lambda x: x["id"])
        res = []
        for o in out:
            if o.get("error"):
                raise RuntimeError(o["error"])
            res.append(o["result"])
        return res


# --------------------------------------------------------------------------- #
# phase 2: scan (RPC, node stays up)
# --------------------------------------------------------------------------- #
def find_start_height(rpc, target_time, tip):
    """First height whose block time >= target_time (binary search)."""
    lo, hi = 0, tip
    while lo < hi:
        mid = (lo + hi) // 2
        h = rpc.call("getblockhash", [mid])
        t = rpc.call("getblockheader", [h])["time"]
        if t < target_time:
            lo = mid + 1
        else:
            hi = mid
    return lo


def cmd_scan(args):
    con = connect(args.db)
    init_schema(con)
    rpc = RPC(args.datadir)

    info = rpc.call("getblockchaininfo")
    if info["pruned"]:
        sys.exit("node is pruned; RPC scan needs full blocks (see spec fallback notes)")
    tip = info["blocks"] if args.end_height is None else args.end_height

    # resume checkpoint (contiguous watermark)
    row = con.execute("SELECT value FROM progress WHERE key='last_height'").fetchone()
    if args.start_height is not None:
        start = args.start_height
    elif row:
        start = row[0] + 1
    else:
        tmin = con.execute("SELECT MIN(first_expiry) FROM tx").fetchone()[0]
        if tmin is None:
            sys.exit("tx table empty — run `parse` first")
        start = find_start_height(rpc, tmin - 7200, tip)
    if row and args.start_height is None:
        start = max(start, row[0] + 1)

    print(f"loading txid set (expired + replacement endpoints) ...", file=sys.stderr)
    # union: expired txids + every replacement-edge endpoint (old & new, non-package)
    txset = {bytes.fromhex(t) for (t,) in con.execute(
        """SELECT txid FROM tx
           UNION SELECT old_txid FROM replace_edge
           UNION SELECT new_txid FROM replace_edge WHERE is_package=0""")}
    # don't re-find already-confirmed txids on resume
    for (t,) in con.execute("SELECT txid FROM confirmation"):
        txset.discard(bytes.fromhex(t))
    print(f"scanning heights {start:,}..{tip:,} "
          f"({tip-start+1:,} blocks), {len(txset):,} txids, "
          f"{args.workers} workers", file=sys.stderr)

    W = args.window
    pool = ThreadPoolExecutor(max_workers=args.workers)
    t0 = time.time()
    done = 0
    for lo in range(start, tip + 1, W):
        hi = min(lo + W - 1, tip)
        heights = list(range(lo, hi + 1))
        hashes = rpc.batch([("getblockhash", [h]) for h in heights])
        blocks = list(pool.map(lambda bh: rpc.call("getblock", [bh, 1]), hashes))
        hits = []
        for h, blk in zip(heights, blocks):
            bt, bh = blk["time"], blk["hash"]
            for txid in blk["tx"]:
                b = bytes.fromhex(txid)
                if b in txset:
                    hits.append((txid, h, bh, bt))
                    txset.discard(b)
        if hits:
            con.executemany(
                "INSERT OR IGNORE INTO confirmation VALUES (?,?,?,?)", hits)
        con.execute(
            "INSERT OR REPLACE INTO progress VALUES ('last_height', ?)", [hi])
        con.commit()
        done += len(heights)
        if (lo // W) % 10 == 0:
            rate = done / max(time.time() - t0, 1e-9)
            eta = (tip - hi) / max(rate, 1e-9) / 60
            print(f"  h={hi:,}  {rate:.0f} blk/s  "
                  f"found={con.execute('SELECT COUNT(*) FROM confirmation').fetchone()[0]:,}  "
                  f"ETA {eta:.1f}m", file=sys.stderr)
    pool.shutdown()
    nconf = con.execute("SELECT COUNT(*) FROM confirmation").fetchone()[0]
    print(f"\ndone. confirmed {nconf:,} of "
          f"{con.execute('SELECT COUNT(*) FROM tx').fetchone()[0]:,} txids")
    con.close()


# --------------------------------------------------------------------------- #
# phase 4: rebroadcast
# --------------------------------------------------------------------------- #
def cmd_rebroadcast(args):
    con = connect(args.db)
    init_schema(con)
    con.execute("DELETE FROM rebroadcast")

    # 4a lower bound: same node expires the same txid at >=2 distinct timestamps
    reexpiry = dict(con.execute(
        """SELECT txid, COUNT(*) FROM
             (SELECT txid, node, COUNT(DISTINCT ts) c
                FROM expiry_event GROUP BY txid, node HAVING c >= 2)
           GROUP BY txid"""))

    has_added = con.execute("SELECT COUNT(*) FROM added_event").fetchone()[0] > 0
    confirmed = {t for (t,) in con.execute("SELECT txid FROM confirmation")}

    rows = []
    for (txid, first_exp) in con.execute("SELECT txid, first_expiry FROM tx"):
        reentered = txid in reexpiry
        n_rounds = reexpiry.get(txid, 0)
        first_reentry = None
        if has_added:
            # true re-entry: an add strictly after one of this tx's expiries
            r = con.execute(
                """SELECT MIN(a.ts) FROM added_event a
                   WHERE a.txid=? AND a.ts > ?""", (txid, first_exp)).fetchone()
            if r and r[0] is not None:
                reentered = True
                first_reentry = r[0]
                n_rounds = max(n_rounds, con.execute(
                    "SELECT COUNT(*) FROM added_event WHERE txid=? AND ts>?",
                    (txid, first_exp)).fetchone()[0])
        if reentered:
            rows.append((txid, 1, n_rounds, first_reentry,
                         1 if txid in confirmed else 0))
    con.executemany(
        "INSERT OR REPLACE INTO rebroadcast VALUES (?,?,?,?,?)", rows)
    con.commit()

    n = len(rows)
    nconf = sum(r[4] for r in rows)
    total = con.execute("SELECT COUNT(*) FROM tx").fetchone()[0]
    print(f"rebroadcast method: {'AddedToMempool true-count (4b)' if has_added else 're-expiry lower bound (4a)'}")
    print(f"rebroadcast txids   {n:,} / {total:,} ({100*n/max(total,1):.1f}%)")
    print(f"  of those, confirmed {nconf:,} ({100*nconf/max(n,1):.1f}%)")
    con.close()


# --------------------------------------------------------------------------- #
# phase 5: replaced — cascade classification by precedence
# --------------------------------------------------------------------------- #
def cmd_replaced(args):
    con = connect(args.db)
    init_schema(con)
    if con.execute("SELECT COUNT(*) FROM replace_edge").fetchone()[0] == 0:
        sys.exit("replace_edge empty — run `parse-replaced` first")
    con.execute("DELETE FROM replaced_fate")

    adj = {}
    for old, new in con.execute("SELECT old_txid, new_txid FROM replace_edge"):
        adj.setdefault(old, set()).add(new)
    confirmed = {}
    for t, h, bt in con.execute("SELECT txid, height, block_time FROM confirmation"):
        confirmed[t] = (h, bt)

    CAP = 500_000                       # per-cascade visit cap (anomaly guard)
    counts = {"confirmed-self": 0, "replaced-settled": 0,
              "replaced-unsettled": 0, "residual-unknown": 0}
    multi_confirmed = capped = 0
    rows = []
    for (E,) in con.execute("SELECT txid FROM tx"):
        if E in confirmed:
            h, bt = confirmed[E]
            counts["confirmed-self"] += 1
            rows.append((E, "confirmed-self", E, h, bt, 0, 0))
            continue
        children = adj.get(E)
        if not children:
            counts["residual-unknown"] += 1
            rows.append((E, "residual-unknown", None, None, None, None, 0))
            continue
        # BFS over the cascade (descendants) to find confirmed members
        seen = {E}
        frontier = [(c, 1) for c in children]
        hits = []                       # (height, block_time, txid, depth)
        nvisit = 0
        while frontier:
            node, depth = frontier.pop()
            if node in seen:
                continue
            seen.add(node)
            nvisit += 1
            if nvisit > CAP:
                capped += 1
                break
            if node in confirmed:
                h, bt = confirmed[node]
                hits.append((h, bt, node, depth))
            for nn in adj.get(node, ()):
                if nn not in seen:
                    frontier.append((nn, depth + 1))
        n_repl = len(children)
        if hits:
            hits.sort()                 # by height; terminal winner = earliest-mined
            h, bt, stx, depth = hits[0]
            if len({x[2] for x in hits}) > 1:
                multi_confirmed += 1
            counts["replaced-settled"] += 1
            rows.append((E, "replaced-settled", stx, h, bt, depth, n_repl))
        else:
            counts["replaced-unsettled"] += 1
            rows.append((E, "replaced-unsettled", None, None, None, None, n_repl))

    con.executemany(
        "INSERT OR REPLACE INTO replaced_fate VALUES (?,?,?,?,?,?,?)", rows)
    con.commit()

    total = sum(counts.values())
    ntx = con.execute("SELECT COUNT(*) FROM tx").fetchone()[0]
    print("fate partition (precedence):")
    for k in ("confirmed-self", "replaced-settled", "replaced-unsettled", "residual-unknown"):
        print(f"  {k:20} {counts[k]:>9,}  ({100*counts[k]/max(ntx,1):5.1f}%)")
    print(f"  {'TOTAL':20} {total:>9,}   reconciles=={ntx:,}: {total==ntx}")
    if multi_confirmed:
        print(f"  ⚠ {multi_confirmed:,} cascades reached >1 confirmed member "
              "(UTXO-invariant violation — inspect: package/non-conflict or reorg)")
    if capped:
        print(f"  ⚠ {capped:,} cascades hit the {CAP:,}-node visit cap (inspect for cycles)")
    con.close()


# --------------------------------------------------------------------------- #
# phase 3: report
# --------------------------------------------------------------------------- #
def pct(xs, p):
    if not xs:
        return 0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p / 100 * len(xs)))]


def cmd_report(args):
    con = connect(args.db)
    os.makedirs(args.out, exist_ok=True)
    rows = con.execute(
        """SELECT t.txid, t.wtxid, t.n_nodes, t.n_events,
                  t.first_expiry, t.last_expiry,
                  c.height, c.block_time
           FROM tx t LEFT JOIN confirmation c ON t.txid=c.txid""").fetchall()

    import csv
    with open(os.path.join(args.out, "results.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["txid", "wtxid", "n_nodes", "n_events", "first_expiry",
                    "last_expiry", "confirmed", "height", "block_time",
                    "delay_s", "delay_first"])
        for (txid, wtxid, nn, ne, fe, le, h, bt) in rows:
            conf = 1 if h is not None else 0
            delay = (bt - le) if bt is not None else ""
            delay_first = (bt - fe) if bt is not None else ""
            w.writerow([txid, wtxid, nn, ne, fe, le, conf,
                        h if h is not None else "", bt if bt is not None else "",
                        delay, delay_first])

    total = len(rows)
    conf = [r for r in rows if r[6] is not None]
    delays = [r[7] - r[5] for r in conf]            # block_time - last_expiry
    delays_first = [r[7] - r[4] for r in conf]      # block_time - first_expiry
    df_after = [d for d in delays_first if d >= 0]  # genuinely mined after first expiry
    before = sum(1 for d in delays_first if d < 0)  # lagging-node eviction of already-mined tx
    buckets = {"<1d": 0, "1-7d": 0, "7-30d": 0, ">30d": 0, "neg": 0}
    for d in delays:
        if d < 0:
            buckets["neg"] += 1
        elif d < 86400:
            buckets["<1d"] += 1
        elif d < 7 * 86400:
            buckets["1-7d"] += 1
        elif d < 30 * 86400:
            buckets["7-30d"] += 1
        else:
            buckets[">30d"] += 1

    with open(os.path.join(args.out, "summary.txt"), "w") as f:
        def pr(*a):
            print(*a); print(*a, file=f)
        pr(f"unique expired txids   {total:,}")
        pr(f"later confirmed        {len(conf):,} ({100*len(conf)/max(total,1):.1f}%)")
        if delays:
            pr(f"delay(vs last_expiry) p10/p50/p90/max  {pct(delays,10)/86400:.2f} / "
               f"{pct(delays,50)/86400:.2f} / {pct(delays,90)/86400:.2f} / "
               f"{max(delays)/86400:.2f} days")
            pr(f"mined before first_expiry (lagging node): {before:,} "
               f"({100*before/max(len(conf),1):.0f}% of confirmed)")
            if df_after:
                pr(f"delay after first_expiry (neg excluded) p50/p90/max  "
                   f"{pct(df_after,50)/86400:.2f} / {pct(df_after,90)/86400:.2f} / "
                   f"{max(df_after)/86400:.2f} days")
            pr("delay buckets:         " +
               "  ".join(f"{k}={v:,}" for k, v in buckets.items()))
        # per month of first expiry
        pr("\nby month of first expiry (confirmed / total):")
        bymonth = {}
        for r in rows:
            mk = datetime.fromtimestamp(r[4], timezone.utc).strftime("%Y-%m")
            c, t = bymonth.get(mk, (0, 0))
            bymonth[mk] = (c + (1 if r[6] is not None else 0), t + 1)
        for mk in sorted(bymonth):
            c, t = bymonth[mk]
            pr(f"  {mk}  {c:,}/{t:,} ({100*c/max(t,1):.0f}%)")
        # by n_nodes (1 vs many)
        one = [r for r in rows if r[2] == 1]
        many = [r for r in rows if r[2] > 1]
        for label, grp in (("1 node", one), ("many nodes", many)):
            c = sum(1 for r in grp if r[6] is not None)
            pr(f"\n{label}: {len(grp):,} txids, {c:,} confirmed "
               f"({100*c/max(len(grp),1):.1f}%)")
        # repeated expiry
        rep = con.execute(
            "SELECT COUNT(*) FROM tx WHERE n_events > n_nodes").fetchone()[0]
        pr(f"\nexpired repeatedly (n_events>n_nodes): {rep:,}")
        # rebroadcast
        rb = con.execute("SELECT COUNT(*) FROM rebroadcast").fetchone()[0]
        if rb:
            rbc = con.execute(
                "SELECT COUNT(*) FROM rebroadcast WHERE confirmed_after_reentry=1"
            ).fetchone()[0]
            pr(f"rebroadcast txids: {rb:,} ({100*rb/max(total,1):.1f}%), "
               f"of which confirmed {rbc:,} ({100*rbc/max(rb,1):.1f}%)")
        # fate partition (Phase 5)
        nf = con.execute("SELECT COUNT(*) FROM replaced_fate").fetchone()[0]
        if nf:
            pr("\nfate partition (Phase 5, precedence):")
            for cls, c in con.execute(
                    "SELECT class, COUNT(*) FROM replaced_fate GROUP BY class "
                    "ORDER BY 2 DESC"):
                pr(f"  {cls:20} {c:,} ({100*c/max(total,1):.1f}%)")
            sd = [r[0] for r in con.execute(
                "SELECT settle_block_time-first_expiry FROM replaced_fate rf "
                "JOIN tx USING(txid) WHERE class='replaced-settled' "
                "AND settle_block_time IS NOT NULL")]
            if sd:
                pr(f"  replaced-settled delay p50/p90 (days): "
                   f"{pct(sd,50)/86400:.2f} / {pct(sd,90)/86400:.2f}")
    con.close()


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("parse"); p.add_argument("--log", required=True)
    p.add_argument("--db", default="expiry.db"); p.set_defaults(fn=cmd_parse)

    p = sub.add_parser("scan"); p.add_argument("--db", default="expiry.db")
    p.add_argument("--datadir", default="/data/bitcoin")
    p.add_argument("--start-height", type=int)
    p.add_argument("--end-height", type=int)
    p.add_argument("--window", type=int, default=200)
    p.add_argument("--workers", type=int, default=6); p.set_defaults(fn=cmd_scan)

    p = sub.add_parser("parse-added"); p.add_argument("--log", required=True)
    p.add_argument("--db", default="expiry.db"); p.set_defaults(fn=cmd_parse_added)

    p = sub.add_parser("parse-replaced"); p.add_argument("--log", required=True)
    p.add_argument("--db", default="expiry.db"); p.set_defaults(fn=cmd_parse_replaced)

    p = sub.add_parser("rebroadcast"); p.add_argument("--db", default="expiry.db")
    p.set_defaults(fn=cmd_rebroadcast)

    p = sub.add_parser("replaced"); p.add_argument("--db", default="expiry.db")
    p.set_defaults(fn=cmd_replaced)

    p = sub.add_parser("report"); p.add_argument("--db", default="expiry.db")
    p.add_argument("--out", default="results/"); p.set_defaults(fn=cmd_report)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
