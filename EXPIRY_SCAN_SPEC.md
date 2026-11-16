# Spec: Fate of expired mempool transactions

## Motivation
Time-based mempool expiry is proposed for removal from Bitcoin Core (a PR on the `persist-user-agent`
branch, commit `d40e511d9d`). The data in `expiry.log.zst` comes from monitoring nodes that ran
normal Core with expiry active, so it records real `reason=expiry` evictions. The study asks what
happened to each evicted transaction. If expired txs mostly got mined anyway, directly or after a
rebroadcast, removing expiry is low-harm. If they were mostly lost or unmineable, expiry was doing
useful garbage collection.

## Fate of a transaction
Confirmation is the fate the data settles directly:

- confirmed: the exact txid was later mined.
- not confirmed: the txid was never mined. This is some mix of replaced (a conflicting transaction
  spending the same inputs was mined instead) and lost (the inputs never moved), but the two cannot be
  separated from these logs (see Limitation).

Rebroadcast, meaning the txid re-entered a mempool after expiring, is a separate attribute reported
alongside the fate.

## Data
Input is one removal event per line, prefixed by source file and node:
```
combined/debug.log-20240216.zst:alice 2024-02-16T18:48:54Z [...] TransactionRemovedFromMempool: txid=<64hex> wtxid=<64hex> reason=expiry
```
Parsing:
- Anchor on the ISO-8601 timestamp, not on position: some lines carry a marker first
  (`...zst:bob [*] 2025-09-12T...`). txid and wtxid are lowercase 64-hex; reason is one token.
- Skip `Enqueuing TransactionRemovedFromMempool`. The old format logs each event twice, on
  `[msghand]` then `[scheduler]`; keep the `[scheduler]` line.
- Keep `reason=expiry`; count other reasons and unparseable lines.

The original extract used a grep without `-a`, so a NUL byte in 12 daily files made grep emit
`Binary file <f> matches` in place of their expiry lines, dropping those days (20241002, 20250922,
20251003, 20251028, 20251126, 20260114, 20260316, 20260405, 20260513, 20260615, 20261018, 20261019).
The parser detects the sentinel and warns. Any re-extraction must use `grep -a`.

## Node
A fully synced, unpruned Bitcoin Core node, reachable over JSON-RPC with cookie auth. Confirmation is
a read-only block scan: read each block's txid list over RPC and intersect with the expired set.
txindex is not used.

## Pipeline
All phases live in one `expiry_scan.py` (Python, stdlib only) and write SQLite `expiry.db`, so each is
resumable. Run order: `parse`, then `scan`, then `parse-added` and `rebroadcast`, then `report`.

### parse
Stream `zstd -dc expiry.log.zst`, dedup, aggregate per txid.
```sql
expiry_event(node TEXT, txid TEXT, wtxid TEXT, ts INTEGER)
tx(txid TEXT PRIMARY KEY, wtxid TEXT, first_expiry INTEGER, last_expiry INTEGER,
   n_events INTEGER, n_nodes INTEGER)
```

### scan
Range: from the first block with `time >= min(first_expiry) - 2h` (binary search over block time) to
the tip, roughly 147k blocks. Hold the expired txids in memory as 32-byte keys. For each block, read
its txid list and intersect, dropping hits. Issue concurrent `getblock` requests for throughput and
batch only `getblockhash`.
```sql
confirmation(txid TEXT PRIMARY KEY, height INTEGER, blockhash TEXT, block_time INTEGER)
progress(key TEXT PRIMARY KEY, value INTEGER)
```
Windows are processed in order, so `last_height` is a contiguous resume point.

### rebroadcast
Re-extract `TransactionAddedToMempool` into `added.log.zst`, same dedup.
```sql
added_event(node, txid, wtxid, ts)
tx_added(txid PRIMARY KEY, first_added, last_added, n_added, n_nodes_added)
rebroadcast(txid PRIMARY KEY, reentered, n_rounds, first_reentry, confirmed_after_reentry)
```
A txid is rebroadcast if an add post-dates one of its own expiries. Without the added stream, a lower
bound still follows from a single node expiring the same txid twice at increasing timestamps. Cross
with `confirmation` for rebroadcast-then-confirmed.

### report
`results.csv` has one row per expired txid: identifiers, expiry times, confirmed flag, height,
block_time, and the delays `block_time - last_expiry` and `block_time - first_expiry`. `summary.txt`
covers percent confirmed; the confirm-delay distribution; breakdowns by month, node, and n_nodes;
repeated expiry (`n_events > n_nodes`); rebroadcast and rebroadcast-then-confirmed; and cross-node
agreement, meaning for txids seen by two or more nodes the spread of per-node first expiry.

## Limitation
The not-confirmed transactions cannot be split into replaced versus lost. Once a transaction expires
it is already out of the mempool, so a later conflicting transaction is never logged as replacing it;
Core's `mempool:replaced` log only fires while the replaced transaction is still in a mempool, which
an expired one is not. Detecting replacement would mean taking the expired transaction's spent
outpoints and checking whether a later transaction spends them, which needs the prevouts or raw
transactions that the txid-only logs do not carry. The same gap blocks characterizing unmineable spam.

## CLI
```
python3 expiry_scan.py parse       --log expiry.log.zst --db expiry.db
python3 expiry_scan.py scan        --db expiry.db [--workers 6] [--window 200] [--datadir <datadir>]
python3 expiry_scan.py parse-added --log added.log.zst  --db expiry.db
python3 expiry_scan.py rebroadcast --db expiry.db
python3 expiry_scan.py report      --db expiry.db --out results/
```

## Acceptance checks
- `parse` on the first 100k lines: kept equals the non-Enqueuing `reason=expiry` lines; the full run
  reconciles total to Enqueuing plus scheduler with no field anomalies; a re-extracted input shows no
  `binary file matches`.
- `scan` killed mid-run resumes from `last_height` without rescanning.
- Spot-check confirmed txids with `getrawtransaction <txid> true <blockhash>`.
- `rebroadcast` flags re-entry only when an add, or a same-node later-dated expiry, post-dates an
  expiry.
