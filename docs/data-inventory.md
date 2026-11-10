# Market data inventory (23 Oct 2026)

Survey of the owner's existing data before milestone 1 imports it into the TradeDesk store.
Source folder on the Mac: `~/Trading/algotrading/data` (5.4 GB; Algo Lab's `data/` is a link to it).
Nothing here has been changed; the import writes a new store alongside it.

## Summary

- **Two years of 1-minute data from TrueData: 15 Oct 2024 → 21 Oct 2026**, for index spot, index futures, India VIX,
  every traded option contract of NIFTY, BANKNIFTY and SENSEX, 50 stock futures and 50 NIFTY stocks.
- **NIFTY spot back to 2 Jan 2023** at 5-minute resolution (from Kite), with VIX.
- **No option history before 15 Oct 2024**, and none at all for FINNIFTY or MIDCPNIFTY.
- Daily recording after 21 Oct 2026 is running on the Lightsail server (Algo Lab); that store is not inventoried yet.

## TrueData raw download — `truedata/` (2.1 GB, the primary source)

| Data set | Files | Interval / fields | Range | Notes |
|---|---|---|---|---|
| NIFTY options | 21,180 | 1 min OHLCV + OI | 111 expiries, 17 Oct 2024 → 23 Nov 2026 | 96–307 contracts per expiry |
| BANKNIFTY options | 6,940 | 1 min OHLCV + OI | 30 expiries, 16 Oct 2024 → 23 Nov 2026 | weekly until 13 Nov 2024, monthly from 27 Nov 2024 |
| SENSEX options | 27,914 | 1 min OHLCV + OI | 114 expiries, 18 Oct 2024 → 19 Nov 2026 | some expiries have only 5 contracts |
| Download log | `progress.db` | — | — | 56,032 contracts done (118 M rows), 786 empty (never traded), 1,846 tick files |
| Index spot | `underlying/` | 1 min OHLC | 15 Oct 2024 → 21 Oct 2026 | NIFTY 50, NIFTY BANK, SENSEX, INDIA VIX (~187k bars each) |
| Index futures | `underlying/` | 1 min OHLCV + OI | NIFTY-I, BANKNIFTY-I from 15 Oct 2024; SENSEX-I from 3 Aug 2026 only | continuous near-month |
| Stock futures | 50 | 1 min OHLCV + OI | 15 Oct 2024 → 21 Oct 2026 | continuous near-month (`-I`) |
| Equity | 66 | 1 min OHLCV | 15 Oct 2024 → 21 Oct 2026 | 50 NIFTY stocks + a few MCX metals (`ALUMINIUM-I`, `ZINC-I`) |
| Bhavcopy (EOD) | 996 | daily OHLC, volume, OI per contract | NSE FO and BSE FO, 15 Oct 2024 → 21 Oct 2026, 498 days each | 16 dates × 2 exchanges marked "none": all exchange holidays (e.g. 15 Nov 2024, 25 Dec, 15 Aug 2025) |
| Ticks | 1,846 | tick by tick | 5 days, 14–21 Oct 2026 | NIFTY, BANKNIFTY, SENSEX options near the money |

## Algo Lab working copies (derived; not re-imported)

| Data set | Size | Content | Use in TradeDesk |
|---|---|---|---|
| `options/` | 3.2 GB, 56,037 files | TrueData options converted to `time,last,bid,ask` | Skip — TrueData raw is the source of truth |
| `spot/TD_*_1minute.csv` | 53 MB | TrueData spot + VIX joined | Skip — use `truedata/underlying` |
| `spot/NIFTY_5minute.csv` | 70k bars | Kite NIFTY 5-min + VIX, **2 Jan 2023 → 16 Oct 2026** | **Import** — the only pre-Oct-2024 history |
| `spot/NIFTY_minute.csv` | 50k bars | Kite NIFTY 1-min, 1 Apr → 14 Oct 2026 | Import as `source=kite` for vendor-vs-Kite comparison |
| `options_1min/NIFTY` | 352 files | Kite recordings, 3 expiries (Oct–Nov 2026) | Import as `source=kite` |
| `spot/SAMPLE_*` | — | Synthetic test data | Never import |
| `review/` | — | Algo Lab data-quality reports | Reference for the new checks |

## Known quality issues (from Algo Lab's last review, 21 Oct 2026)

- **FAIL:** 3 SENSEX put contracts (1 Oct 2026 expiry) trade below intrinsic value near the money on 25 Sep 2026.
- **WARN:** 163 option spikes that reverse within a bar or two (NIFTY 46, BANKNIFTY 21, SENSEX 96); filler bars outside
  market hours in SENSEX spot.
- **INFO:** spot files include pre-open bars from 09:07 and closing-auction bars; thin at-the-money data on some SENSEX days.
- The Mac's Kite collection failed on 16 Oct 2026 (timeout) and has no entries after it; the server recording continues.

## Gaps to decide on later

1. Options before 15 Oct 2024 (2023–24): needed only for longer backtests. Buy later if M2 results justify it.
2. FINNIFTY and MIDCPNIFTY options: none. Record from M1; history only if a strategy needs it.
3. Lightsail server recordings (Algo Lab, daily 15:45): inventory and merge once TradeDesk's server (M0) exists.
