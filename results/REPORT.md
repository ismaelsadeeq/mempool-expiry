# Expired mempool transaction fate

Data: `expiry.log.zst`, a `reason=expiry`-only extract from b10c's 15-node fleet. Window 2024-02-16 to 2026-10-21:
12,650,774 events, 938,157 unique txids. Method: block scan of an unpruned node, txid intersection,
verified with `getrawtransaction`. Method spec: [EXPIRY_SCAN_SPEC.md](../EXPIRY_SCAN_SPEC.md).

| metric | value |
| --- | --- |
| confirmed | 863,451 (92.0%) |
| never confirmed | 74,706 (8.0%) |
| rebroadcast (lower bound) | 316,381 (33.7%), 86.9% of them confirmed |
| confirm delay (block minus first_expiry) | p50 4.8d, p90 55.4d, max 317d |
| cross-node spread (2+ nodes) | p50 27s, 54% within 60s, 24% over 14d |
| confirm rate, 1 node vs many | 98.0% vs 89.0% |

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
- The extract was built without `grep -a`, dropping 12 days (about 1%), immaterial to the headline.

Text summary: [summary.txt](summary.txt). The full per-transaction CSV is produced by `report`
locally and is not committed (about 170 MB).
