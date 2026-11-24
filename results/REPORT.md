# Expired mempool transaction fate

Data: `expiry.log.zst`, a `reason=expiry`-only extract from b10c's 15-node fleet. Window 2024-02-16 to 2026-10-21:
12,650,774 events, 938,157 unique txids. Method: block scan of an unpruned node, txid intersection,
verified with `getrawtransaction`. Method spec: [EXPIRY_SCAN_SPEC.md](../EXPIRY_SCAN_SPEC.md).

| metric | value |
| --- | --- |
| confirmed | 863,454 (92.0%) |
| never confirmed | 74,703 (8.0%) |
| rebroadcast (lower bound) | 316,381 (33.7%), 86.9% of them confirmed |
| mined before first expiry (lagging-node eviction) | 252,571 (29% of confirmed) |
| confirm delay, mined after expiry | p50 10.8d, p90 80.5d, max 317d |
| cross-node spread (2+ nodes) | p50 27s, 54% within 60s, 24% over 14d |
| confirm rate, 1 node vs many | 98.0% vs 89.0% |

![outcome](fig_outcome.svg)
![rebroadcast](fig_rebroadcast.svg)
![delay](fig_delay.svg)
![by month](fig_by_month.svg)
![cross-node](fig_crossnode.svg)
![n_nodes](fig_nnodes.svg)

## Surges vs constant stream
Expiries are a low baseline punctuated by large surges. The median day sees about 100 txids expire, but
the mean is ~1,500/day: the top 10 days account for ~31% of all expiries and the top 30 days for ~57%.
The largest surges (2025-09-30, 2025-01, 2026-05-24) line up with fee-market congestion and mostly
confirm later. The Aug-2026 spike is a single node (`nico`) expiring already-mined txs (see Limitations).

![surges](fig_surges.svg)

## What are the expired transactions
Sampling confirmed txids and reading the raw tx (ordinals envelope in witness, Runestone OP_RETURN,
other OP_RETURN, else regular): overall about 47% are regular payments, 40% runes, 11% ordinal
inscriptions, 2% other data. The surges are almost entirely runes: the big surge days (2025-09-30,
2026-05-24, 2025-09-10) are 99-100% Runestone transactions. Regular payments expire at a steadier,
lower rate. Only confirmed txs can be classified (raw tx needs the block hash; no txindex).

![types](fig_types.svg)

## Limitations
- Rebroadcast is a lower bound; the true rate needs the `AddedToMempool` stream.
- Replaced-vs-lost is not separable: an expired transaction is already evicted, so it is never logged
  as replaced; splitting it would need the expired transactions' inputs, which the logs do not carry.
- About 29% of confirmed txids were mined *before* their first expiry: a single lagging node expired a
  tx that was already on-chain. They are reported separately; the "mined after expiry" delay excludes
  them.
- Coverage is not continuous: ~37% of calendar days in the window have no events in the extract
  (collection gaps, including a ~130-day gap in mid-2025), which limits temporal/continuity analysis.
- The extract was also built without `grep -a`, dropping ~12 days.

Text summary: [summary.txt](summary.txt). The full per-transaction CSV is produced by `report`
locally and is not committed (about 170 MB).
