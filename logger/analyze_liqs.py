r"""
Liquidation analysis: did fading a Kraken liquidation burst pay off?

Reads the logger's daily CSVs, groups fills into liquidation *bursts*, downloads
Kraken 1-minute candles, and measures what price did after each burst, from the
point of view of FADING it:
    longs liquidated (side = sell)  -> fade = go LONG
    shorts liquidated (side = buy)  -> fade = go SHORT

Sections in the summary:
  - descriptive tables (by pair, side, VWMA stretch, size, open interest)
  - VWMA band table, overall and per pair ("only fade bursts >= X% from VWMA")
  - special events (e.g. the Binance hack) vs everything else
  - take-profit / stop-loss simulation with fees and a one-position-at-a-time rule
  - Kraken vs Coinbase spot: would the same fades have done better on Coinbase?

Run:   python analyze_liqs.py                  (uses ./logs)
       python analyze_liqs.py path\to\logs
       python analyze_liqs.py --no-coinbase    (skip the Coinbase comparison)

Writes logs/analysis/bursts.csv and logs/analysis/summary.txt.
Candles are cached in logs/analysis/candles/ and candles_coinbase/.
Read-only public market data: no API keys, no orders.
"""

import csv
import glob
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ------------------------------------------------------------------ settings

KRAKEN_URL = "https://futures.kraken.com/api/charts/v1/trade/{symbol}/1m"
COINBASE_URL = "https://api.exchange.coinbase.com/products/{product}/candles"

# Kraken perp -> Coinbase spot product used for the venue comparison
COINBASE_PRODUCTS = {
    "PF_XBTUSD": "BTC-USD", "PF_ETHUSD": "ETH-USD", "PF_SOLUSD": "SOL-USD",
    "PF_XRPUSD": "XRP-USD", "PF_DOGEUSD": "DOGE-USD", "PF_LINKUSD": "LINK-USD",
    "PF_ZECUSD": "ZEC-USD", "PF_HYPEUSD": "HYPE-USD",
}

# Assumed Kraken Futures fee per side (base-tier taker). Change if yours differs.
FEE_PER_SIDE_PCT = 0.05
ROUND_TRIP_FEE_PCT = 2 * FEE_PER_SIDE_PCT

# Market events to report separately (UTC). Add more as they happen.
EVENT_WINDOWS = [
    ("Binance hack", "2026-10-29T19:00:00+00:00", "2026-10-30T04:00:00+00:00"),
]

BURST_GAP_SECONDS = 60       # fills of the same pair+side closer than this = one burst
CLUSTER_GAP_SECONDS = 120    # bursts on any pair within this = one market-wide event
HORIZONS_MIN = [5, 15, 60]
EXCURSION_MIN = 60           # window for best/worst move and the TP/SL simulation

BAND_THRESHOLDS = [0, 0.5, 1, 1.5, 2, 2.5, 3, 4, 5]
PAIR_BAND_THRESHOLDS = [0, 1, 1.5, 2, 3]
TP_LEVELS = [0.3, 0.5, 0.75, 1.0, 1.5]
SL_LEVELS = [0.5, 0.75, 1.0, 1.5, 2.0]

STRETCH_BUCKETS = [(-99, 0, "wrong side of VWMA"), (0, 1, "0-1%"), (1, 2, "1-2%"),
                   (2, 3, "2-3%"), (3, 99, "3%+")]
SIZE_BUCKETS = [(0, 5_000, "< $5k"), (5_000, 25_000, "$5k-25k"),
                (25_000, 100_000, "$25k-100k"), (100_000, 1e18, "$100k+")]


# ------------------------------------------------------------------ load + group

def parse_ts(s):
    return datetime.fromisoformat(s).timestamp()


def load_rows(log_dir):
    rows = []
    for path in sorted(glob.glob(str(log_dir / "liqs_*.csv"))):
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                if not r.get("trade_time_utc") or not r.get("price"):
                    continue                      # skip a half-written last line
                r["ts"] = parse_ts(r["trade_time_utc"])
                for k in ("qty", "price", "notional_usd", "vwma", "dist_from_vwma_pct",
                          "open_interest", "oi_change_60s_pct"):
                    r[k] = float(r[k]) if r.get(k) not in ("", None) else None
                rows.append(r)
    seen, unique = set(), []
    for r in rows:
        if r["uid"] and r["uid"] in seen:
            continue
        seen.add(r["uid"])
        unique.append(r)
    return sorted(unique, key=lambda r: r["ts"])


def group_bursts(rows):
    """Consecutive fills on the same symbol+side, each within BURST_GAP_SECONDS
    of the previous one, become one burst."""
    open_bursts, bursts = {}, []
    for r in rows:
        key = (r["symbol"], r["side"])
        b = open_bursts.get(key)
        if b and r["ts"] - b["fills"][-1]["ts"] <= BURST_GAP_SECONDS:
            b["fills"].append(r)
        else:
            b = {"symbol": r["symbol"], "side": r["side"], "fills": [r]}
            open_bursts[key] = b
            bursts.append(b)

    out = []
    for b in bursts:
        f = b["fills"]
        fade = "long" if b["side"] == "sell" else "short"
        dists = [x["dist_from_vwma_pct"] for x in f if x["dist_from_vwma_pct"] is not None]
        ois = [x["oi_change_60s_pct"] for x in f if x["oi_change_60s_pct"] is not None]
        if dists:
            extreme = min(dists) if fade == "long" else max(dists)
            stretch = -extreme if fade == "long" else extreme
        else:
            stretch = None
        out.append({
            "symbol": b["symbol"],
            "side": b["side"],
            "fade": fade,
            "start_ts": f[0]["ts"],
            "end_ts": f[-1]["ts"],
            "fills": len(f),
            "notional_usd": sum(x["notional_usd"] for x in f),
            "first_price": f[0]["price"],
            "last_price": f[-1]["price"],
            "vwma": f[0]["vwma"],
            "stretch_pct": stretch,
            "oi_change_min_pct": min(ois) if ois else None,
        })
    return out


def assign_clusters(bursts):
    """Bursts on any pair that start close together = one market-wide event."""
    cluster, last_end = 0, None
    for b in sorted(bursts, key=lambda b: b["start_ts"]):
        if last_end is None or b["start_ts"] - last_end > CLUSTER_GAP_SECONDS:
            cluster += 1
        b["cluster"] = cluster
        last_end = max(last_end or 0, b["end_ts"])
    return cluster


def event_label(ts, windows=EVENT_WINDOWS):
    for name, start, end in windows:
        if parse_ts(start) <= ts < parse_ts(end):
            return name
    return ""


# ------------------------------------------------------------------ candles

def fetch_candles(symbol, start_ts, end_ts, cache_dir):
    """Kraken Futures 1m candles between start and end (open-time ms -> candle)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    candles = {}
    chunk = 12 * 3600
    t = int(start_ts // 60 * 60)
    while t < end_ts:
        t_end = min(t + chunk, int(end_ts) + 60)
        cache_file = cache_dir / f"{symbol}_{t}_{t_end}.json"
        complete = t_end < time.time() - 120
        if cache_file.exists():
            data = json.loads(cache_file.read_text())
        else:
            data = download_kraken_range(symbol, t, t_end)
            if complete:
                cache_file.write_text(json.dumps(data))
            time.sleep(0.3)
        for c in data:
            candles[int(c["time"])] = c
        t = t_end
    return candles


def download_kraken_range(symbol, start, end):
    url = KRAKEN_URL.format(symbol=symbol)
    out, cursor = [], start
    for _ in range(50):
        r = requests.get(url, params={"from": cursor, "to": end}, timeout=20)
        r.raise_for_status()
        got = r.json().get("candles", [])
        if not got:
            break
        out.extend(got)
        last = int(got[-1]["time"]) // 1000
        if last + 60 >= end or last < cursor:
            break
        cursor = last + 60
    return out


def coinbase_to_candles(raw):
    """Coinbase returns [time_s, low, high, open, close, volume] rows (newest first).
    Convert to the same dict format as Kraken candles, keyed by open time in ms."""
    out = {}
    for t, low, high, open_, close, vol in raw:
        out[int(t) * 1000] = {"time": int(t) * 1000, "open": open_, "high": high,
                              "low": low, "close": close, "volume": vol}
    return out


def needed_windows(bursts, chunk_seconds):
    """Start times of fixed-size chunks that overlap any burst's analysis window."""
    starts = set()
    for b in bursts:
        lo = b["start_ts"] - 120
        hi = b["end_ts"] + (EXCURSION_MIN + 2) * 60
        k = int(lo // chunk_seconds)
        while k * chunk_seconds < hi:
            starts.add(k * chunk_seconds)
            k += 1
    return sorted(starts)


def fetch_coinbase_candles(product, bursts, cache_dir):
    """Coinbase spot 1m candles, only around the bursts (max 300 candles per call)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    chunk = 300 * 60
    candles = {}
    for start in needed_windows(bursts, chunk):
        end = start + chunk
        cache_file = cache_dir / f"{product}_{start}.json"
        if cache_file.exists():
            raw = json.loads(cache_file.read_text())
        else:
            r = requests.get(COINBASE_URL.format(product=product), timeout=20, params={
                "granularity": 60,
                "start": datetime.fromtimestamp(start, tz=timezone.utc).isoformat(),
                "end": datetime.fromtimestamp(end - 60, tz=timezone.utc).isoformat(),
            })
            if r.status_code == 404:
                raise LookupError(f"{product} not listed on Coinbase")
            r.raise_for_status()
            raw = r.json()
            if end < time.time() - 120:
                cache_file.write_text(json.dumps(raw))
            time.sleep(0.2)
        candles.update(coinbase_to_candles(raw))
    return candles


def candle_at(candles, ts):
    return candles.get(int(ts // 60 * 60) * 1000)


# ------------------------------------------------------------------ outcomes

def fade_return(fade, entry, price):
    move = (price - entry) / entry * 100
    return move if fade == "long" else -move


def measure(b, candles, entry=None, start_ts=None, prefix=""):
    """Returns at 5/15/60 min plus best/worst within 60 min.
    Default entry = last liquidation price, measured from the burst's end."""
    entry = b["last_price"] if entry is None else entry
    start_ts = b["end_ts"] if start_ts is None else start_ts
    now = time.time()
    for h in HORIZONS_MIN:
        target = start_ts + h * 60
        c = candle_at(candles, target) if target < now - 60 else None
        b[f"{prefix}ret_{h}m_pct"] = fade_return(b["fade"], entry, float(c["close"])) if c else None

    window = [candle_at(candles, start_ts + m * 60) for m in range(1, EXCURSION_MIN + 1)]
    window = [c for c in window if c]
    if len(window) >= EXCURSION_MIN * 0.8:
        highs = [float(c["high"]) for c in window]
        lows = [float(c["low"]) for c in window]
        up, down = (max(highs), min(lows)) if b["fade"] == "long" else (min(lows), max(highs))
        b[f"{prefix}best_60m_pct"] = fade_return(b["fade"], entry, up)
        b[f"{prefix}worst_60m_pct"] = fade_return(b["fade"], entry, down)
    else:
        b[f"{prefix}best_60m_pct"] = b[f"{prefix}worst_60m_pct"] = None


def next_minute_open(candles, ts):
    """Entry price if you got in at the start of the minute after the burst."""
    c = candle_at(candles, ts + 60)
    return float(c["open"]) if c else None


# ------------------------------------------------------------------ TP / SL simulation

def simulate_exit(fade, entry, candles, start_ts, tp_pct, sl_pct, max_min=EXCURSION_MIN):
    """Walk 1m candles after entry. Returns (gross % return, minutes held, reason)
    or None if too many candles are missing. If target and stop are both inside
    the same candle we can't tell which came first, so we assume the STOP (worst case)."""
    if fade == "long":
        stop, target = entry * (1 - sl_pct / 100), entry * (1 + tp_pct / 100)
    else:
        stop, target = entry * (1 + sl_pct / 100), entry * (1 - tp_pct / 100)

    missing, last_close, last_m = 0, None, 0
    for m in range(1, max_min + 1):
        c = candle_at(candles, start_ts + m * 60)
        if not c:
            missing += 1
            continue
        high, low = float(c["high"]), float(c["low"])
        hit_stop = low <= stop if fade == "long" else high >= stop
        hit_target = high >= target if fade == "long" else low <= target
        if hit_stop:
            return -sl_pct, m, "stop"
        if hit_target:
            return tp_pct, m, "target"
        last_close, last_m = float(c["close"]), m
    if missing > max_min * 0.2 or last_close is None:
        return None
    return fade_return(fade, entry, last_close), last_m, "time"


def run_strategy(bursts, candles_by_symbol, tp_pct, sl_pct, min_stretch=None,
                 min_size=0, one_position=True, fee_pct=ROUND_TRIP_FEE_PCT):
    """Trade the bursts in time order. With one_position=True, a new burst is
    skipped while a trade is still open (on any pair)."""
    trades, busy_until = [], float("-inf")
    for b in sorted(bursts, key=lambda b: b["end_ts"]):
        if min_stretch is not None and (b["stretch_pct"] is None or b["stretch_pct"] < min_stretch):
            continue
        if b["notional_usd"] < min_size:
            continue
        candles = candles_by_symbol.get(b["symbol"])
        if not candles:
            continue
        if one_position and b["end_ts"] < busy_until:
            continue
        res = simulate_exit(b["fade"], b["last_price"], candles, b["end_ts"], tp_pct, sl_pct)
        if res is None:
            continue
        gross, minutes, reason = res
        trades.append({"burst": b, "gross": gross, "net": gross - fee_pct,
                       "minutes": minutes, "reason": reason})
        busy_until = b["end_ts"] + minutes * 60
    return trades


# ------------------------------------------------------------------ summary helpers

def fmt(x, nd=2):
    return f"{x:+.{nd}f}" if x is not None else "  n/a"


def values(group, key):
    return [b[key] for b in group if b.get(key) is not None]


def med(group, key):
    v = values(group, key)
    return statistics.median(v) if v else None


def avg(group, key):
    v = values(group, key)
    return statistics.mean(v) if v else None


def win_rate(group, key):
    v = values(group, key)
    return sum(1 for x in v if x > 0) / len(v) * 100 if v else None


HEADER = (f"{'':<22}{'bursts':>6}{'events':>8}{'win15':>7}"
          f"{'med 5m':>8}{'med 15m':>9}{'med 60m':>9}{'best60':>8}{'worst60':>9}{'avg15 net':>11}")


def stats_line(label, group):
    w = win_rate(group, "ret_15m_net_pct")
    return (f"{label:<22}{len(group):>6}{len({b['cluster'] for b in group}):>8}"
            f"{(f'{w:.0f}%' if w is not None else 'n/a'):>7}"
            f"{fmt(med(group, 'ret_5m_pct')):>8}{fmt(med(group, 'ret_15m_pct')):>9}"
            f"{fmt(med(group, 'ret_60m_pct')):>9}{fmt(med(group, 'best_60m_pct')):>8}"
            f"{fmt(med(group, 'worst_60m_pct')):>9}{fmt(avg(group, 'ret_15m_net_pct')):>11}")


def bucket(value, buckets):
    if value is None:
        return None
    for lo, hi, name in buckets:
        if lo <= value < hi:
            return name
    return None


def band_filter(bursts, threshold):
    return [b for b in bursts if b["stretch_pct"] is not None and b["stretch_pct"] >= threshold]


# ------------------------------------------------------------------ summary

def build_summary(bursts, n_clusters, rows, candles_by_symbol, coinbase_ok):
    L = []
    first = datetime.fromtimestamp(rows[0]["ts"], tz=timezone.utc)
    last = datetime.fromtimestamp(rows[-1]["ts"], tz=timezone.utc)
    L += [
        "LIQUIDATION FADE ANALYSIS",
        f"Data: {first:%Y-%m-%d %H:%M} to {last:%Y-%m-%d %H:%M} UTC",
        f"{len(rows)} fills (${sum(r['notional_usd'] for r in rows):,.0f}) -> "
        f"{len(bursts)} bursts -> {n_clusters} market-wide events",
        "",
        "Returns are % in the FADE direction, entering at the last liquidation price.",
        f"'net' subtracts {ROUND_TRIP_FEE_PCT:.2f}% round-trip fees "
        f"({FEE_PER_SIDE_PCT:.2f}% per side, Kraken taker assumption).",
        "win15 = share of bursts with a positive NET return after 15 min.",
        "best60/worst60 = median best and worst point within the next 60 min.",
        "events = independent moments (bursts on several pairs at once count once).",
        "",
    ]

    def section(title, key_fn, order):
        L.append(title)
        L.append(HEADER)
        for name in order:
            g = [b for b in bursts if key_fn(b) == name]
            if g:
                L.append(stats_line(name, g))
        L.append("")

    L.append(HEADER.replace(" " * 22, "ALL".ljust(22), 1))
    L.append(stats_line("all bursts", bursts))
    L.append("")
    section("BY PAIR", lambda b: b["symbol"], sorted({b["symbol"] for b in bursts}))
    section("BY WHO GOT LIQUIDATED", lambda b: "longs (fade long)" if b["fade"] == "long"
            else "shorts (fade short)", ["longs (fade long)", "shorts (fade short)"])
    section("BY STRETCH FROM VWMA (in fade direction)",
            lambda b: bucket(b["stretch_pct"], STRETCH_BUCKETS), [n for _, _, n in STRETCH_BUCKETS])
    section("BY BURST SIZE", lambda b: bucket(b["notional_usd"], SIZE_BUCKETS),
            [n for _, _, n in SIZE_BUCKETS])
    section("BY OPEN INTEREST", lambda b: None if b["oi_change_min_pct"] is None else
            ("OI fell > 0.2%" if b["oi_change_min_pct"] < -0.2 else "OI flat / up"),
            ["OI fell > 0.2%", "OI flat / up"])

    # --- special events
    L.append("SPECIAL EVENTS vs EVERYTHING ELSE")
    L.append(HEADER)
    outside = [b for b in bursts if not b["event"]]
    L.append(stats_line("excluding events", outside))
    for name, _, _ in EVENT_WINDOWS:
        g = [b for b in bursts if b["event"] == name]
        if g:
            L.append(stats_line(name, g))
    L.append("")

    # --- biggest days
    by_day = {}
    for b in bursts:
        d = datetime.fromtimestamp(b["start_ts"], tz=timezone.utc).strftime("%Y-%m-%d")
        by_day.setdefault(d, []).append(b)
    top_days = sorted(by_day, key=lambda d: -sum(b["notional_usd"] for b in by_day[d]))[:5]
    L.append("5 BIGGEST DAYS (by liquidated $)")
    L.append(HEADER)
    for d in top_days:
        L.append(stats_line(f"{d} ${sum(b['notional_usd'] for b in by_day[d]) / 1e6:.1f}M", by_day[d]))
    L.append("")

    # --- band table, overall
    L.append("VWMA BAND TABLE: only fade bursts at least X% from VWMA (net of fees)")
    L.append(f"{'band':<8}{'bursts':>7}{'events':>8}{'win15':>7}{'avg5':>8}{'avg15':>8}"
             f"{'avg60':>8}{'med worst60':>13}")
    for x in BAND_THRESHOLDS:
        g = band_filter(bursts, x)
        if not g:
            continue
        w = win_rate(g, "ret_15m_net_pct")
        L.append(f"{f'{x:g}%+':<8}{len(g):>7}{len({b['cluster'] for b in g}):>8}"
                 f"{(f'{w:.0f}%' if w is not None else 'n/a'):>7}"
                 f"{fmt(avg(g, 'ret_5m_net_pct')):>8}{fmt(avg(g, 'ret_15m_net_pct')):>8}"
                 f"{fmt(avg(g, 'ret_60m_net_pct')):>8}{fmt(med(g, 'worst_60m_pct')):>13}")
    L.append("")

    # --- band table, per pair
    L.append("VWMA BAND TABLE PER PAIR: bursts / avg 15m net %")
    L.append(f"{'pair':<12}" + "".join(f"{f'{x:g}%+':>15}" for x in PAIR_BAND_THRESHOLDS))
    for sym in sorted({b["symbol"] for b in bursts}):
        cells = []
        for x in PAIR_BAND_THRESHOLDS:
            g = band_filter([b for b in bursts if b["symbol"] == sym], x)
            a = avg(g, "ret_15m_net_pct")
            cells.append(f"{len(g):>4} / {fmt(a)}" if g else "-")
        L.append(f"{sym:<12}" + "".join(f"{c:>15}" for c in cells))
    L.append("")

    # --- TP / SL simulation
    L.append("TAKE-PROFIT / STOP-LOSS SIMULATION (net of fees, one position at a time)")
    L.append("Exit at target, stop, or after 60 min. If target and stop fall in the same")
    L.append("1-minute candle, the stop is assumed (worst case). Cells = avg net % per trade.")
    results = []
    for min_stretch, label in ((None, "any stretch"), (1.0, "stretch >= 1%"),
                               (2.0, "stretch >= 2%")):
        L.append("")
        n_ref = None
        grid_lines = []
        for sl in SL_LEVELS:
            cells = []
            for tp in TP_LEVELS:
                t = run_strategy(bursts, candles_by_symbol, tp, sl, min_stretch=min_stretch)
                if t:
                    a = statistics.mean(x["net"] for x in t)
                    results.append((a, label, tp, sl, t))
                    n_ref = len(t)
                    cells.append(f"{a:+7.2f}")
                else:
                    cells.append(f"{'-':>7}")
            grid_lines.append(f"  SL {sl:<5g}" + "".join(f"{c:>9}" for c in cells))
        L.append(f"{label} (about {n_ref or 0} trades per cell)")
        L.append(f"{'':<10}" + "".join(f"{f'TP {tp:g}':>9}" for tp in TP_LEVELS))
        L += grid_lines
    L.append("")
    if results:
        L.append("TOP 5 TP/SL SETTINGS")
        L.append(f"{'filter':<15}{'TP':>5}{'SL':>6}{'trades':>8}{'win':>6}{'avg net':>9}"
                 f"{'total net':>11}{'worst':>8}{'targets/stops/time':>21}")
        for a, label, tp, sl, t in sorted(results, key=lambda r: -r[0])[:5]:
            wins = sum(1 for x in t if x["net"] > 0) / len(t) * 100
            reasons = [sum(1 for x in t if x["reason"] == r) for r in ("target", "stop", "time")]
            L.append(f"{label:<15}{tp:>5g}{sl:>6g}{len(t):>8}{wins:>5.0f}%{a:>+9.2f}"
                     f"{sum(x['net'] for x in t):>+11.2f}{min(x['net'] for x in t):>+8.2f}"
                     f"{'/'.join(map(str, reasons)):>21}")
        L.append("(Picking the best cell after the fact flatters it. Treat as a starting point.)")
    L.append("")

    # --- Kraken vs Coinbase
    if coinbase_ok:
        L.append("KRAKEN vs COINBASE SPOT (same bursts, entry at the open of the next minute,")
        L.append("gross returns: fee schedules differ by venue)")
        L.append(f"{'pair':<12}{'bursts':>7}{'K avg15':>9}{'CB avg15':>10}{'K best60':>10}"
                 f"{'CB best60':>11}{'K worst60':>11}{'CB worst60':>12}")
        for sym in sorted({b["symbol"] for b in bursts}):
            g = [b for b in bursts if b["symbol"] == sym
                 and b.get("k_ret_15m_pct") is not None and b.get("cb_ret_15m_pct") is not None]
            if not g:
                continue
            L.append(f"{sym:<12}{len(g):>7}{fmt(avg(g, 'k_ret_15m_pct')):>9}"
                     f"{fmt(avg(g, 'cb_ret_15m_pct')):>10}{fmt(med(g, 'k_best_60m_pct')):>10}"
                     f"{fmt(med(g, 'cb_best_60m_pct')):>11}{fmt(med(g, 'k_worst_60m_pct')):>11}"
                     f"{fmt(med(g, 'cb_worst_60m_pct')):>12}")
        L.append("")

    # --- largest bursts
    big = sorted(bursts, key=lambda b: -b["notional_usd"])[:15]
    L.append("15 LARGEST BURSTS")
    L.append(f"{'time (UTC)':<17}{'pair':<11}{'fade':<6}{'size $':>11}{'stretch':>9}"
             f"{'5m':>8}{'15m':>8}{'60m':>8}{'worst60':>9}  event")
    for b in big:
        t = datetime.fromtimestamp(b["start_ts"], tz=timezone.utc)
        L.append(f"{t:%m-%d %H:%M:%S}   {b['symbol']:<11}{b['fade']:<6}"
                 f"{b['notional_usd']:>11,.0f}{fmt(b['stretch_pct']):>9}"
                 f"{fmt(b['ret_5m_pct']):>8}{fmt(b['ret_15m_pct']):>8}"
                 f"{fmt(b['ret_60m_pct']):>8}{fmt(b['worst_60m_pct']):>9}  {b['event']}")
    L += ["", "Small samples: treat every number here as a first look, not a trading rule."]
    return "\n".join(L)


# ------------------------------------------------------------------ main

BURST_COLUMNS = ["cluster", "event", "symbol", "side", "fade", "start_utc", "end_utc", "fills",
                 "notional_usd", "first_price", "last_price", "vwma", "stretch_pct",
                 "oi_change_min_pct", "ret_5m_pct", "ret_15m_pct", "ret_60m_pct",
                 "ret_15m_net_pct", "best_60m_pct", "worst_60m_pct",
                 "k_ret_15m_pct", "cb_ret_15m_pct", "cb_best_60m_pct", "cb_worst_60m_pct"]


def add_net(b):
    for h in HORIZONS_MIN:
        g = b.get(f"ret_{h}m_pct")
        b[f"ret_{h}m_net_pct"] = g - ROUND_TRIP_FEE_PCT if g is not None else None


def main(log_dir, fetch=fetch_candles, fetch_cb=fetch_coinbase_candles, use_coinbase=True):
    out_dir = log_dir / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = load_rows(log_dir)
    if not rows:
        print(f"No liqs_*.csv rows found in {log_dir}")
        return None
    bursts = group_bursts(rows)
    n_clusters = assign_clusters(bursts)
    for b in bursts:
        b["event"] = event_label(b["start_ts"])
    print(f"{len(rows)} fills -> {len(bursts)} bursts -> {n_clusters} events")

    candles_by_symbol, coinbase_ok = {}, False
    for symbol in sorted({b["symbol"] for b in bursts}):
        mine = [b for b in bursts if b["symbol"] == symbol]
        start = min(b["start_ts"] for b in mine) - 300
        end = min(max(b["end_ts"] for b in mine) + (EXCURSION_MIN + 2) * 60, time.time())
        print(f"Kraken candles for {symbol} ({len(mine)} bursts)...")
        try:
            candles = fetch(symbol, start, end, out_dir / "candles")
        except Exception as e:
            print(f"  failed: {e} (outcomes for {symbol} left blank)")
            candles = {}
        candles_by_symbol[symbol] = candles
        for b in mine:
            measure(b, candles)
            add_net(b)

        product = COINBASE_PRODUCTS.get(symbol)
        if not (use_coinbase and product and candles):
            continue
        print(f"Coinbase candles for {product}...")
        try:
            cb = fetch_cb(product, mine, out_dir / "candles_coinbase")
        except Exception as e:
            print(f"  skipped: {e}")
            continue
        for b in mine:
            k_entry, cb_entry = next_minute_open(candles, b["end_ts"]), next_minute_open(cb, b["end_ts"])
            if k_entry and cb_entry:
                measure(b, candles, entry=k_entry, start_ts=b["end_ts"] + 60, prefix="k_")
                measure(b, cb, entry=cb_entry, start_ts=b["end_ts"] + 60, prefix="cb_")
                coinbase_ok = True

    with open(out_dir / "bursts.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=BURST_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for b in sorted(bursts, key=lambda b: b["start_ts"]):
            row = dict(b)
            row["start_utc"] = datetime.fromtimestamp(b["start_ts"], tz=timezone.utc).isoformat()
            row["end_utc"] = datetime.fromtimestamp(b["end_ts"], tz=timezone.utc).isoformat()
            for k, v in row.items():
                if isinstance(v, float):
                    row[k] = round(v, 4)
            w.writerow(row)

    summary = build_summary(bursts, n_clusters, rows, candles_by_symbol, coinbase_ok)
    (out_dir / "summary.txt").write_text(summary)
    print()
    print(summary)
    print(f"\nSaved {out_dir / 'bursts.csv'} and {out_dir / 'summary.txt'}")
    return summary


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    folder = Path(args[0]) if args else Path(__file__).resolve().parent / "logs"
    main(folder, use_coinbase="--no-coinbase" not in sys.argv)
