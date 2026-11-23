# mempool-expiry

Headline: 89.1% of expired transactions were later mined (610,883/685,586).

- [Report and charts](results/REPORT.md)
- [Method spec](EXPIRY_SCAN_SPEC.md)

Tooling: [`expiry_scan.py`](expiry_scan.py) (parse, scan, rebroadcast, replaced, report) and
[`make_report.py`](make_report.py) (charts). Data is a `reason=expiry`-only extract courtesy of
b10c, so rebroadcast is a lower bound and replaced-vs-lost is out of scope here.
